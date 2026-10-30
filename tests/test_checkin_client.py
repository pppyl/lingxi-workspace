import copy
from datetime import datetime, timedelta
import time
import unittest
from unittest.mock import Mock
from curl_cffi import requests

from checkin_client import CheckinClient, CheckinError, ENTRY, FLASH_PRODUCT, PreparedFlash, _official_url
from recharge.cmcc import SHANGHAI


SNAPSHOT = {'phone': '13800000000', 'cookie': 'JSESSIONID=fake', 'profile': {}, 'credential_revision': 1}
NOW = 1793246400000  # 2026-10-29 12:00 Asia/Shanghai


class Response:
    def __init__(self, data=None, *, text='', status=200, headers=None):
        self.status_code, self.text, self.headers = status, text, headers or {}
        self.data = data

    def json(self):
        return self.data


class ProtocolSession:
    def __init__(self, *, signed=True, now=NOW):
        self.headers, self.cookies, self.calls, self.signed, self.now = {}, {}, [], signed, now
        self.stock, self.claimed = 1, False
        self.identity = {'mobile': SNAPSHOT['phone'], 'channel': 'app'}
        self.today_status = None
        self.end_time = NOW+3600000

    def get(self, url, **kwargs):
        self.calls.append(('GET', url, kwargs))
        if self.headers.get('login-check') or self.headers.get('x-requested-with'):
            return Response(text='{"code":"GOTO_LOGIN","data":null}')
        return Response(text='var loginPath="/appTokenLogin?sid=fake";')

    def post(self, url, *, json, **kwargs):
        self.calls.append(('POST', url, json))
        if '/qwhdsso/appTokenLogin' in url:
            return Response({'code': 'SUCCESS', 'data': {'url': ENTRY}})
        if kwargs.get('headers', {}).get('login-check') != '1':
            return Response(text='<!doctype html>login redirect')
        path = url.split('/api/mark/')[-1]
        if path == 'user/info':
            data = self.identity
        elif path == 'info/commonInfo':
            data = {'sysTime': self.now, 'isMark31': True}
        elif path == 'mark31/markstatus':
            data = {'markstatus': [{'date': '20261029', 'status': self.today_status or ('1' if self.signed else '0')}],
                    'userinfo': {'accumulateTimes': 1 if self.signed else 0}}
        elif path == 'markSeckill/secConfig':
            data = {'secKillData': {'secKillZones': [{'id': 123, 'startTime': NOW, 'endTime': self.end_time,
                    'prize': {'id': 456, 'name': '2元话费券', 'leaveStock': self.stock, 'status': '1' if self.claimed else '0'}}]}}
        elif path == 'mark31/domark':
            self.signed = True
            data = {'markPrize': {'name': '100MB流量日包券'}}
        elif path == 'markSeckill/redeem':
            self.claimed = True
            data = {}
        else:
            raise AssertionError(path)
        return Response({'code': 'SUCCESS', 'data': data})

    def close(self):
        pass


class CheckinProtocolTests(unittest.TestCase):
    def client(self, session):
        return CheckinClient(lambda: copy.deepcopy(SNAPSHOT), session_factory=lambda: session)

    def mutations(self, session):
        return [call for call in session.calls if call[0] == 'POST' and
                ('/domark' in call[1] or '/redeem' in call[1])]

    def test_inspect_uses_real_calendar_and_never_mutates(self):
        session = ProtocolSession()
        result = self.client(session).inspect()
        self.assertEqual((result['today_state'], result['month_count']), ('signed', 1))
        self.assertEqual(result['date'], '20261029')
        self.assertEqual(self.mutations(session), [])
        self.assertEqual(result['products'][0]['id'], FLASH_PRODUCT)
        self.assertNotIn('cookie', result)

    def test_page_navigation_is_not_treated_as_api_login_check(self):
        session = ProtocolSession()
        self.assertEqual(self.client(session).inspect()['today_state'], 'signed')
        self.assertNotIn('login-check', session.headers)
        self.assertEqual(len([call for call in session.calls if call[0] == 'GET']), 2)

    def test_opaque_mobile_is_bound_to_expected_masked_display_phone(self):
        session = ProtocolSession()
        session.identity = {'mobile': 'opaque-not-a-phone', 'nickName': '138****0000', 'channel': 'app'}
        self.assertEqual(self.client(session).inspect()['today_state'], 'signed')

    def test_wrong_masked_phone_cannot_use_opaque_identity(self):
        session = ProtocolSession()
        session.identity = {'mobile': 'opaque-not-a-phone', 'nickName': '139****1111', 'channel': 'app'}
        with self.assertRaises(CheckinError) as raised:
            self.client(session).signin()
        self.assertEqual(raised.exception.code, 'ACCOUNT_MISMATCH')
        self.assertEqual(self.mutations(session), [])

    def test_opaque_identity_without_verifiable_phone_is_rejected(self):
        session = ProtocolSession()
        session.identity = {'mobile': 'opaque-not-a-phone', 'channel': 'app'}
        with self.assertRaises(CheckinError) as raised:
            self.client(session).signin()
        self.assertEqual(raised.exception.code, 'ACCOUNT_MISMATCH')
        self.assertEqual(self.mutations(session), [])

    def test_already_signed_never_posts_again(self):
        session = ProtocolSession()
        result = self.client(session).signin()
        self.assertEqual(result['state'], 'already_signed')
        self.assertFalse(result['submitted'])
        self.assertEqual(self.mutations(session), [])

    def test_signin_once_then_verifies_official_calendar(self):
        session = ProtocolSession(signed=False)
        result = self.client(session).signin()
        self.assertEqual(result['state'], 'signed')
        self.assertEqual(result['month_count'], 1)
        self.assertEqual(len(self.mutations(session)), 1)
        self.assertEqual(self.mutations(session)[0][2], {'date': '20261029'})
        self.assertNotIn('/do/mark', self.mutations(session)[0][1])

    def test_submission_callback_runs_immediately_before_real_write(self):
        session = ProtocolSession(signed=False)
        before = Mock(side_effect=lambda: self.assertEqual(self.mutations(session), []))
        self.client(session).signin(before_submit=before)
        before.assert_called_once_with()
        before.reset_mock()
        self.client(session).signin(before_submit=before)
        before.assert_not_called()

    def test_flash_will_not_send_early(self):
        session = ProtocolSession(now=NOW-1000)
        result = self.client(session).flash(FLASH_PRODUCT)
        self.assertFalse(result['submitted'])
        self.assertEqual(self.mutations(session), [])

    def test_re_sign_status_is_not_claimed_as_today_signed(self):
        session = ProtocolSession()
        session.today_status = '2'
        with self.assertRaises(CheckinError) as raised:
            self.client(session).signin()
        self.assertEqual(raised.exception.code, 'TODAY_STATE_MISSING')
        self.assertEqual(self.mutations(session), [])

    def test_unknown_end_time_never_authorizes_flash_submission(self):
        session = ProtocolSession()
        session.end_time = 'not-a-time'
        result = self.client(session).flash(FLASH_PRODUCT)
        self.assertEqual(result['state'], 'skipped')
        self.assertEqual(self.mutations(session), [])

    def test_flash_requires_today_signin(self):
        session = ProtocolSession(signed=False)
        result = self.client(session).flash(FLASH_PRODUCT)
        self.assertFalse(result['submitted'])
        self.assertEqual(self.mutations(session), [])

    def test_flash_fetches_current_zone_and_claims_once(self):
        session = ProtocolSession()
        result = self.client(session).flash(FLASH_PRODUCT)
        self.assertEqual(result['state'], 'success')
        self.assertEqual(self.mutations(session)[0][2], {'secId': '123', 'prizeId': 456})
        result = self.client(session).flash(FLASH_PRODUCT)
        self.assertFalse(result['submitted'])
        self.assertEqual(len(self.mutations(session)), 1)

    def test_sold_out_does_not_post(self):
        session = ProtocolSession()
        session.stock = 0
        self.assertFalse(self.client(session).flash(FLASH_PRODUCT)['submitted'])
        self.assertEqual(self.mutations(session), [])

    def test_credential_rotation_before_submit_aborts(self):
        session = ProtocolSession(signed=False)
        provider = Mock(return_value=copy.deepcopy(SNAPSHOT))
        client = CheckinClient(provider, session_factory=lambda: session)
        original = session.post
        def post(url, **kwargs):
            response = original(url, **kwargs)
            if url.endswith('/markSeckill/secConfig'):
                provider.return_value = {**SNAPSHOT, 'credential_revision': 2, 'cookie': 'JSESSIONID=new'}
            return response
        session.post = post
        with self.assertRaises(CheckinError) as raised:
            client.signin()
        self.assertEqual(raised.exception.code, 'APP_CREDENTIALS_CHANGED')
        self.assertEqual(self.mutations(session), [])

    def test_timeout_after_submit_is_uncertain_and_never_retried(self):
        session = ProtocolSession(signed=False)
        original = session.post
        attempted = []
        def post(url, **kwargs):
            if url.endswith('/mark31/domark'):
                attempted.append(url)
                raise requests.RequestsError('timeout')
            return original(url, **kwargs)
        session.post = post
        with self.assertRaises(CheckinError) as raised:
            self.client(session).signin()
        self.assertTrue(raised.exception.uncertain)
        self.assertEqual(len(attempted), 1)

    def test_mismatched_sso_account_is_never_used(self):
        session = ProtocolSession(signed=False)
        original = session.post
        def post(url, **kwargs):
            if url.endswith('/user/info'):
                return Response({'code': 'SUCCESS', 'data': {'mobile': '13900000000', 'channel': 'app'}})
            return original(url, **kwargs)
        session.post = post
        with self.assertRaises(CheckinError) as raised:
            self.client(session).signin()
        self.assertEqual(raised.exception.code, 'ACCOUNT_MISMATCH')
        self.assertEqual(self.mutations(session), [])

    def test_refuses_cross_origin_sso_redirect(self):
        with self.assertRaises(CheckinError):
            _official_url('https://other.example/collect')

    def test_unknown_product_is_rejected_before_network(self):
        session = ProtocolSession()
        with self.assertRaises(CheckinError):
            self.client(session).flash('a-purchase')
        self.assertEqual(session.calls, [])


class PreparedFlashTests(unittest.TestCase):
    def setUp(self):
        self.primary, self.workers = ProtocolSession(), []
        self.primary.cookies['HLWHD_SESSION_TOKEN'] = 'fake-activity-session'
        self.provider = Mock(return_value=copy.deepcopy(SNAPSHOT))
        def worker():
            session = ProtocolSession()
            session.close = Mock()
            self.workers.append(session)
            return session
        self.client = CheckinClient(self.provider, session_factory=lambda: self.primary,
                                   worker_session_factory=worker)
        self.settings = {'burst_count': 40, 'burst_interval': 0.05, 'burst_workers': 3}

    def test_burst_reuses_one_sso_but_leases_independent_warmed_sessions(self):
        result = self.client.flash(FLASH_PRODUCT, burst=self.settings)
        self.assertEqual((result['state'], result['attempt_count']), ('success', 1))
        sso_calls = [c for c in self.primary.calls if '/qwhdsso/appTokenLogin' in c[1]]
        self.assertEqual(len(sso_calls), 1)
        self.assertEqual(len(self.workers), 3)
        mutations = []
        for worker in self.workers:
            self.assertEqual(worker.cookies['HLWHD_SESSION_TOKEN'], 'fake-activity-session')
            self.assertEqual(worker.headers['Referer'], ENTRY)
            self.assertTrue(worker.calls[0][1].endswith('/info/commonInfo'))
            mutations.extend(c for c in worker.calls if c[1].endswith('/redeem'))
            worker.close.assert_called_once_with()
        self.assertEqual([c[2] for c in mutations], [{'secId': '123', 'prizeId': 456}])
        self.assertNotIn('cookie', str(result))
        self.assertNotIn('fake-activity-session', str(result))

    def test_preparation_waits_for_noon_even_if_official_zone_started_earlier(self):
        target = datetime.fromtimestamp(NOW/1000, SHANGHAI)
        zone = {'id': 123, 'startTime': NOW-3600000, 'endTime': NOW+3600000,
                'prize': {'id': 456, 'status': '0', 'leaveStock': 1}}
        self.client._inspect = Mock(return_value=({'server_time': (target-timedelta(seconds=30)).isoformat(),
                'today_state': 'signed', 'flash_state': 'available'}, [zone]))
        prepared = PreparedFlash(self.client, self.primary, copy.deepcopy(SNAPSHOT))
        self.addCleanup(prepared.close)
        original = self.client._post
        def post(worker, path, *args, **kwargs):
            worker.now = NOW-30000
            return original(worker, path, *args, **kwargs)
        self.client._post = post
        began = time.monotonic()
        prepared.prepare(2, target)
        self.assertTrue(prepared.ready)
        self.assertGreaterEqual(prepared.start_monotonic, began+30)
        self.assertTrue(all(len(worker.calls) == 1 for worker in self.workers))

    def test_changed_credentials_after_warmup_prevent_all_redeems(self):
        from checkin_flash import run_flash_burst
        prepared = PreparedFlash(self.client, self.primary, copy.deepcopy(SNAPSHOT))
        self.addCleanup(prepared.close)
        prepared.prepare(3, None)
        self.provider.return_value = {**SNAPSHOT, 'credential_revision': 2, 'cookie': 'JSESSIONID=new'}
        result = run_flash_burst(prepared, self.settings, Mock(), lambda: False)
        self.assertEqual((result['state'], result['attempt_count']), ('failed', 0))
        self.assertEqual(result['attempts'][0]['code'], 'APP_CREDENTIALS_CHANGED')
        self.assertFalse(any(c[1].endswith('/redeem') for w in self.workers for c in w.calls))

    def test_only_busy_or_not_started_replies_allow_following_requests(self):
        prepared = PreparedFlash(self.client, self.primary, copy.deepcopy(SNAPSHOT))
        self.addCleanup(prepared.close)
        prepared.prepare(1, None)
        worker = self.workers[0]
        for code, state in [('NET_ERROR', 'retryable'), ('NOT_TIME_IN', 'retryable'),
                            ('PRIZE_NO_STOCK', 'terminal'), ('PRIZE_LIMIT_MONTH', 'terminal'),
                            ('UNKNOWN', 'terminal')]:
            worker.post = Mock(return_value=Response({'code': 'FAIL', 'status': code}))
            self.assertEqual(prepared.send(before_submit=Mock())['state'], state)
        worker.post = Mock(return_value=Response({'code': 'FAIL', 'status': 'RISK_VERIFY'}))
        with self.assertRaises(CheckinError) as raised:
            prepared.send(before_submit=Mock())
        self.assertEqual(raised.exception.code, 'VERIFICATION_REQUIRED')


if __name__ == '__main__':
    unittest.main()
