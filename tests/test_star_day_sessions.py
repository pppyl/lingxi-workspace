from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from datetime import datetime
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from http_transport import requests
from login_storage import DatabaseLoginStorage
from recharge.cmcc import SHANGHAI
from star_day_auth import StarDaySessionCache, star_day_session, StarDayAuthError
from star_day_client import StarDayClient, StarDayError


class CampaignSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.storage = DatabaseLoginStorage(Path(self.temp.name)/'accounts.sqlite3',
            protect=lambda b:b'protected:'+b, unprotect=lambda b:b.removeprefix(b'protected:'))
        self.stamp = datetime(2026,11,1,8,0,tzinfo=SHANGHAI).timestamp()
        self.cache = StarDaySessionCache(self.storage, now=lambda:self.stamp)
        self.snapshot = {'id':'a','phone':'13800000001','cookie':'UID=original; JSESSIONID=session','credential_revision':1}
        self.flow = Mock()
        self.flow.call.return_value = (200, {'retCode':'000000'}, {})
        self.denied, self.auth_calls = False, 0
        self.clients = []
        self.patch = patch('star_day_auth.AppLoginSession.from_credentials', return_value=self.flow)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def factory(self):
        client = Mock(headers={}, cookies=requests.Cookies())
        def post(*args, **kwargs):
            self.auth_calls += 1
            if not self.denied:
                client.cookies.set('access_token', 'campaign-secret-'+str(self.auth_calls), domain='qqt.cmicrwx.cn')
            return Mock(status_code=200, json=lambda:{'status':'0' if self.denied else '1'})
        client.post.side_effect = post
        self.clients.append(client)
        return client

    def session(self, **kwargs):
        return star_day_session(lambda:deepcopy(self.snapshot), session_factory=self.factory, cache=self.cache, **kwargs)

    def test_reuses_campaign_for_two_hours_despite_primary_cookie_and_revision_updates(self):
        with self.session():pass
        created = self.cache.load('a', self.snapshot['phone'])['expires_at']
        for offset in (1,300,3600,7199):
            self.stamp = created-7200+offset
            self.snapshot.update(cookie='UID=changed; JSESSIONID=new', credential_revision=offset)
            with self.session() as (client, _):
                self.assertEqual(client.cookies.get('access_token'), 'campaign-secret-1')
        self.assertEqual(self.auth_calls, 1)
        self.assertEqual(self.flow.call.call_count, 1)
        self.assertEqual(self.cache.load('a', self.snapshot['phone'])['expires_at'], created)
        self.stamp = created
        with self.session():pass
        self.assertEqual(self.auth_calls, 2)

    def test_restart_loads_encrypted_campaign_cookie_without_new_login(self):
        with self.session():pass
        with closing(sqlite3.connect(self.storage.path)) as db:
            blob = db.execute('SELECT payload FROM activity_state WHERE platform_id=?', ('star-day-session:a',)).fetchone()[0]
        self.assertTrue(blob.startswith(b'protected:'))
        self.cache = StarDaySessionCache(self.storage, now=lambda:self.stamp)
        self.stamp += 600
        with self.session() as (client, _):self.assertEqual(client.cookies.get('access_token'), 'campaign-secret-1')
        self.assertEqual(self.auth_calls, 1)

    def test_concurrent_queries_share_one_campaign_login(self):
        def read(_):
            with self.session() as (client, _):return client.cookies.get('access_token')
        with ThreadPoolExecutor(max_workers=8) as pool:
            tokens = list(pool.map(read, range(16)))
        self.assertEqual(set(tokens), {'campaign-secret-1'})
        self.assertEqual(self.auth_calls, 1)

    def test_force_refresh_is_explicit_and_failure_does_not_replace_valid_cookie(self):
        with self.session():pass
        self.denied = True
        with self.assertRaises(StarDayAuthError):
            with self.session(force=True):pass
        self.assertEqual(self.cache.load('a', self.snapshot['phone'])['cookies'][0]['value'], 'campaign-secret-1')
        with self.session():pass
        self.assertEqual(self.auth_calls, 2)

    def test_accounts_never_share_tokens_and_logout_removes_persisted_cookie(self):
        with self.session():pass
        self.snapshot.update(id='b', phone='13800000002')
        with self.session() as (client, _):self.assertEqual(client.cookies.get('access_token'), 'campaign-secret-2')
        self.cache.forget('a')
        self.assertIsNone(self.storage.load_activity_data('star-day-session:a'))
        self.assertIsNotNone(self.cache.load('b', self.snapshot['phone']))

    def test_official_logout_clears_cookie_but_business_rejection_keeps_it(self):
        client = StarDayClient(lambda:deepcopy(self.snapshot), session_factory=self.factory, session_cache=self.cache)
        for code in ('-800','-10'):
            with self.assertRaises(StarDayError):
                with client._session():raise StarDayError('upstream rejected', code)
            self.assertEqual(self.cache.load('a', self.snapshot['phone']) is None, code == '-10')
        self.assertEqual(self.auth_calls, 1)

    def test_0930_replaces_older_cookie_once_but_late_normal_login_is_reused(self):
        with self.session():pass
        self.stamp = datetime(2026,11,1,9,30,tzinfo=SHANGHAI).timestamp()
        client = StarDayClient(lambda:deepcopy(self.snapshot), session_factory=self.factory, session_cache=self.cache)
        client.prepare()
        self.assertEqual(self.auth_calls, 2)
        self.stamp += 15*60
        client.prepare()
        self.assertEqual(self.auth_calls, 2)
        self.cache.forget('a')
        with self.session():pass  # A normal 09:45 startup login.
        client.prepare()
        self.assertEqual(self.auth_calls, 3)

    def test_sms_fallback_covering_rush_is_not_replaced_by_rejected_app_conversion(self):
        self.stamp=datetime(2026,11,1,9,0,tzinfo=SHANGHAI).timestamp()
        with self.session() as (client,_):self.cache.save('a',self.snapshot['phone'],client,source='sms')
        self.stamp+=30*60
        self.denied=True
        client=StarDayClient(lambda:deepcopy(self.snapshot),session_factory=self.factory,session_cache=self.cache)
        client.prepare()
        self.assertEqual(self.auth_calls,1)


if __name__ == '__main__':unittest.main()
