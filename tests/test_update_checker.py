from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from io import BytesIO
from pathlib import Path
import tempfile
from threading import Event
import time
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from app import create_app
from login_storage import DatabaseLoginStorage
from update_checker import CHECK_INTERVAL, REPOSITORY, UpdateChecker, fetch_release, version_numbers


def release(version='1.4.0'):
    return {'tag_name': 'v'+version, 'name': '版本 '+version, 'body': '修复与改进',
            'published_at': '2026-11-01T08:00:00Z', 'draft': False, 'prerelease': False,
            'assets': [{'name': f'LingxiWorkspace-{version}-{suffix}', 'size': 100, 'browser_download_url': 'https://evil.test/fake'}
                       for suffix in ('Windows-x64.exe', 'Windows-x64.zip', 'Android-arm64.apk')]}


def wait(checker):
    deadline = time.monotonic()+3
    while checker.checking and time.monotonic()<deadline:
        time.sleep(.005)
    if checker.checking:
        raise AssertionError('Update worker did not finish')
    return checker.status()


class UpdateCheckerTests(unittest.TestCase):
    def checker(self, payload=None, **kwargs):
        self.clock = [1000.0]
        self.fetch = Mock(return_value=(release() if payload is None else payload, 'test-etag'))
        return UpdateChecker(current_version='1.3.0', fetcher=self.fetch, now=lambda: self.clock[0], **kwargs)

    def test_numeric_versions_and_platform_specific_downloads(self):
        self.assertGreater(version_numbers('v1.10.0'), version_numbers('1.9.0'))
        for platform, suffix in (('windows', '.exe'), ('android', '.apk'), ('source', 'v1.10.0.zip')):
            checker = self.checker(release('1.10.0'), platform=platform)
            checker.request_check()
            status = wait(checker)
            self.assertTrue(status['available'])
            self.assertTrue(status['recommended']['url'].endswith(suffix))
            self.assertTrue(status['recommended']['url'].startswith(REPOSITORY+'/'))
            self.assertNotIn('evil.test', str(status))
            self.assertEqual(status['release']['notes'], '修复与改进')

    def test_same_and_older_versions_never_prompt_to_update(self):
        for version in ('1.3.0', '1.2.9'):
            checker = self.checker(release(version))
            checker.request_check()
            self.assertFalse(wait(checker)['available'])

    def test_prereleases_drafts_and_malformed_metadata_are_not_offered(self):
        for payload in ({**release(), 'prerelease':True}, {**release(), 'draft':True},
                        release('1.4.0-rc1'), {**release(), 'assets':[None]}, ['not-a-release']):
            checker = self.checker(payload)
            checker.request_check()
            status = wait(checker)
            self.assertFalse(status['available'])
            self.assertTrue(status['error'])

    def test_parallel_requests_merge_and_local_status_reads_do_not_fetch(self):
        started, finish = Event(), Event()
        checker = self.checker()
        def fetch(_):
            started.set()
            finish.wait(2)
            return release(), 'etag'
        self.fetch.side_effect = fetch
        self.assertTrue(checker.request_check()['checking'])
        self.assertTrue(started.wait(1))
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: checker.request_check(force=True), range(8)))
        for _ in range(20):
            self.assertTrue(checker.status()['checking'])
        self.fetch.assert_called_once()
        finish.set()
        wait(checker)

    def test_restart_reuses_cache_until_due_and_revalidates_etag(self):
        with tempfile.TemporaryDirectory() as folder:
            storage = DatabaseLoginStorage(Path(folder)/'accounts.sqlite3')
            checker = self.checker(storage=storage)
            checker.request_check()
            original = wait(checker)['release']
            self.clock[0] += 10
            restarted = UpdateChecker(storage, current_version='1.3.0', now=lambda:self.clock[0], fetcher=self.fetch)
            restarted.request_check()
            self.assertEqual(self.fetch.call_count, 1)
            self.assertEqual(restarted.status()['release'], original)
            self.clock[0] += CHECK_INTERVAL
            self.fetch.return_value = (False, 'test-etag')
            restarted.request_check()
            self.assertEqual(wait(restarted)['release'], original)
            self.fetch.assert_called_with('test-etag')

    def test_manual_check_has_a_shared_cooldown_and_failure_preserves_last_known_release(self):
        checker = self.checker()
        checker.request_check()
        wait(checker)
        checker.request_check(force=True)
        self.fetch.assert_called_once()
        self.clock[0] += 60
        self.fetch.side_effect = URLError('offline')
        checker.request_check(force=True)
        status = wait(checker)
        self.assertTrue(status['available'])
        self.assertIn('检查网络', status['error'])
        self.assertEqual(self.fetch.call_count, 2)
        checker.request_check()
        self.assertEqual(self.fetch.call_count, 2)

    def test_github_rate_limit_honors_server_retry_after(self):
        checker = self.checker()
        self.fetch.side_effect = HTTPError('https://api.github.com', 429, 'limited', {'Retry-After':'1800'}, None)
        checker.request_check()
        status = wait(checker)
        self.assertEqual(status['next_check_at'], 2800)
        self.assertIn('次数已达限制', status['error'])

    def test_missing_release_is_distinct_from_network_failure(self):
        checker = self.checker()
        self.fetch.return_value = (None, '')
        checker.request_check()
        status = wait(checker)
        self.assertIsNone(status['release'])
        self.assertFalse(status['error'])
        self.assertEqual(status['checked_at'], 1000)

    def test_fetch_only_uses_public_github_metadata_and_conditional_headers(self):
        with patch('update_checker.urlopen', side_effect=HTTPError('url', 304, 'same', {}, None)) as opener:
            self.assertEqual(fetch_release('known-etag'), (False, 'known-etag'))
        request = opener.call_args.args[0]
        self.assertEqual(request.get_header('If-none-match'), 'known-etag')
        self.assertIsNone(request.get_header('Authorization'))
        self.assertIsNone(request.get_header('Cookie'))
        self.assertEqual(opener.call_args.kwargs['timeout'], 10)

    def test_background_start_uses_the_same_cache_and_stops(self):
        checker = self.checker()
        checker.start()
        try:
            deadline = time.monotonic()+2
            while not self.fetch.called and time.monotonic()<deadline:
                time.sleep(.005)
            wait(checker)
            checker.start()
            self.fetch.assert_called_once()
        finally:
            checker.shutdown()
        self.assertFalse(checker.thread.is_alive())


class UpdateApiTests(unittest.TestCase):
    def test_routes_are_password_protected_and_reads_do_not_check_github(self):
        app = create_app()
        checker = app.extensions['update_checker']
        checker.fetcher = Mock(return_value=(release('99.0.0'), 'etag'))
        client = app.test_client()
        for _ in range(3):
            self.assertEqual(client.get('/api/updates').status_code, 200)
        checker.fetcher.assert_not_called()
        for path in ('/api/updates', '/api/updates/check'):
            response = client.get(path, base_url='http://public.example:5500', environ_overrides={'REMOTE_ADDR':'2001:db8::2'})
            self.assertEqual(response.status_code, 428)
        response = client.post('/api/updates/check', json={})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(wait(checker)['available'])
        checker.fetcher.assert_called_once()


if __name__ == '__main__':
    unittest.main()
