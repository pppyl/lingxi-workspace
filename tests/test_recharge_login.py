import unittest
from unittest.mock import Mock, patch

from app import create_app
from recharge.accounts import AccountManager
from recharge.cmcc import ActivityClient, ActivityError, APP_UA
from recharge.scheduler import ClaimScheduler


class AppActivityLoginTests(unittest.TestCase):
    def setUp(self):
        self.client = ActivityClient()
        self.http = Mock()
        page = Mock(status_code=200, url='https://wx.10086.cn/qwhdsso/login',
                    text="var loginPath = '/appTokenLogin?sid=fresh-session';")
        finish = Mock(status_code=200)
        identity = Mock(status_code=200)
        identity.json.return_value = {'code': '0', 'data': {'phone': '19900000000', 'provinceCode': '100'}}
        self.http.get.side_effect = [page, finish, identity]
        self.http.post.return_value.status_code = 200
        self.http.post.return_value.json.return_value = {'code': 'SUCCESS', 'data': {'url': '/qwhdsso/redirect?sid=fresh-session'}}

    def test_real_app_cookie_goes_through_app_sso_and_only_masked_identity_returns(self):
        with patch('recharge.cmcc.requests.Session', return_value=self.http):
            result = self.client.connect_app('JSESSIONID=private-app-cookie; UID=u; ticketID=t',
                                             '19900000000', {'provinceCode': '100', 'cityCode': '101'})
        sent = self.http.post.call_args
        self.assertIn('/qwhdsso/appTokenLogin?sid=fresh-session', sent.args[0])
        self.assertEqual(sent.kwargs['json']['token'], 'JSESSIONID=private-app-cookie; UID=u; ticketID=t')
        self.assertEqual(sent.kwargs['json']['userCheckId'], format(19900000000, 'x'))
        self.assertEqual(sent.kwargs['json']['carrierOperator'], '002')
        self.http.headers.update.assert_called_once_with({'User-Agent': APP_UA, 'Origin': 'https://wx.10086.cn'})
        self.assertEqual(result['mode'], 'app')
        self.assertEqual(result['phone'], '199****0000')
        self.assertNotIn('private-app-cookie', str(result))
        self.assertFalse(any('drawPrize' in str(call) for call in self.http.method_calls))

    def test_sso_redirect_cannot_send_session_to_another_origin(self):
        self.http.post.return_value.json.return_value['data']['url'] = 'https://unrelated.example/steal'
        with patch('recharge.cmcc.requests.Session', return_value=self.http):
            with self.assertRaises(ActivityError):
                self.client.connect_app('JSESSIONID=private', '19900000000')
        self.assertEqual(self.client.mode, 'none')
        self.assertEqual(self.http.get.call_count, 1)
        self.http.close.assert_called_once()

    def test_business_sso_failure_is_not_proof_of_expired_app_cookie(self):
        self.http.post.return_value.json.return_value = {'code': 'ERROR', 'msg': 'private upstream detail'}
        with patch('recharge.cmcc.requests.Session', return_value=self.http):
            with self.assertRaises(ActivityError) as failure:
                self.client.connect_app('JSESSIONID=private', '19900000000')
        self.assertEqual(failure.exception.code, 'APP_SSO_FAILED')
        self.assertEqual(failure.exception.status, 502)
        self.assertTrue(failure.exception.retryable)
        self.assertNotIn('private upstream detail', str(failure.exception))
        self.assertEqual(self.client.mode, 'none')


class MountedRechargeTests(unittest.TestCase):
    def setUp(self):
        self.activity = ActivityClient()
        self.manager = AccountManager(default_client=self.activity)
        self.scheduler = ClaimScheduler(self.manager)
        self.app = create_app(enable_recharge=True, recharge_manager=self.manager, recharge_scheduler=self.scheduler)
        self.app.testing = True
        self.http = self.app.test_client()
        self.http.get('/')
        self.page = self.http.get('/recharge/')
        cookie = self.http.get_cookie('recharge_session', path='/recharge')
        child = self.app.extensions['recharge_app']
        decoded = child.session_interface.get_signing_serializer(child).loads(cookie.value)
        self.headers = {'X-CSRF-Token': decoded['csrf']}

    def test_module_assets_are_mounted_on_same_port(self):
        self.assertEqual(self.page.status_code, 200)
        self.assertIn(b'data-api-base="/recharge"', self.page.data)
        self.assertIn(b'/recharge/static/workspace.js', self.page.data)
        with self.http.get('/recharge/static/workspace.js') as asset:
            self.assertEqual(asset.status_code, 200)
        self.assertIn('有效账号自动参与'.encode(), self.page.data)
        self.assertIn('wed-tab-tasks'.encode(), self.page.data)

    def test_shared_login_only_uses_this_browsers_app_credentials(self):
        with self.http.session_transaction() as state:
            sid = state['sid']
        logged = self.app.extensions['lingxi_accounts'][sid]
        logged.app_cookie, logged.app_phone = 'JSESSIONID=private', '19900000000'
        logged.app_profile = {'provinceCode': '100'}
        self.app.extensions['unified_logins'].remember(logged.app_phone,logged.app_cookie,logged.app_profile,'token')
        self.activity.connect_app = Mock(return_value={'authenticated': True, 'mode': 'app', 'phone': '199****0000'})
        result = self.http.post('/recharge/api/login/current', json={}, headers=self.headers)
        self.assertEqual(result.status_code, 200)
        self.activity.connect_app.assert_called_once_with(cookie='JSESSIONID=private', phone='19900000000', profile={'provinceCode': '100'})
        self.assertNotIn(b'JSESSIONID', result.data)
        pool=self.app.extensions['unified_logins'];saved=pool.get(logged.app_phone)
        pool.rotate_credentials(saved.phone,expected_revision=saved.credential_revision,cookie='JSESSIONID=updated')
        self.assertEqual(self.activity.app_credentials_provider()['cookie'],'JSESSIONID=updated')
        self.assertEqual(self.manager.get().pool_login_id,saved.id)
        other = self.app.test_client()
        other.get('/recharge/')
        child = self.app.extensions['recharge_app']
        decoded = child.session_interface.get_signing_serializer(child).loads(other.get_cookie('recharge_session', path='/recharge').value)
        result = other.post('/recharge/api/login/current', json={}, headers={'X-CSRF-Token': decoded['csrf']})
        self.assertEqual(result.status_code, 401)

    def test_shared_login_still_requires_csrf_and_never_starts_jobs(self):
        self.assertEqual(self.http.post('/recharge/api/login/current', json={}).status_code, 403)
        self.assertFalse(self.scheduler.snapshots())
        self.assertIsNone(self.scheduler.thread)

    def test_activity_has_no_second_sms_login_when_mounted(self):
        self.activity.send_sms = Mock()
        result = self.http.post('/recharge/api/login/sms', json={'phone': '19900000000'}, headers=self.headers)
        self.assertEqual(result.status_code, 409)
        self.assertEqual(result.json['code'], 'UNIFIED_LOGIN_REQUIRED')
        self.activity.send_sms.assert_not_called()
        self.assertNotIn(b'id="app-sms-form"', self.page.data)

    def test_same_phone_cannot_create_two_activity_workers(self):
        with self.http.session_transaction() as state:
            logged = self.app.extensions['lingxi_accounts'][state['sid']]
        logged.app_cookie, logged.app_phone = 'JSESSIONID=private', '19900000000'
        self.app.extensions['unified_logins'].remember(logged.app_phone,logged.app_cookie,{},'token')
        self.activity.app_material = {'phone': '19900000000'}
        other = self.manager.add('another')
        other.client.connect_app = Mock()
        result = self.http.post('/recharge/api/login/current', json={'account_id': other.id}, headers=self.headers)
        self.assertEqual(result.status_code, 409)
        self.assertEqual(result.json['code'], 'DUPLICATE_PHONE')
        other.client.connect_app.assert_not_called()

    def test_unified_logout_clears_both_features(self):
        with self.http.session_transaction() as state:
            logged = self.app.extensions['lingxi_accounts'][state['sid']]
        logged.app_cookie, logged.app_phone, logged.auth_code = 'JSESSIONID=private', '19900000000', 'token'
        logged.verified = True
        self.app.extensions['unified_logins'].remember(logged.app_phone, logged.app_cookie, {}, logged.auth_code)
        self.activity.app_material = {'phone': logged.app_phone}
        self.activity.mode = 'app'
        self.manager.update(self.manager.get(), {'authenticated': True})
        self.assertEqual(self.http.post('/api/logout', json={}).status_code, 200)
        self.assertFalse(self.http.get('/api/status').json['has_credentials'])
        self.assertFalse(self.http.get('/api/logins').json['accounts'])
        self.assertEqual(self.activity.mode, 'none')
        self.assertFalse(self.manager.snapshots()[0]['authenticated'])


class UnifiedLoginTests(unittest.TestCase):
    @patch('app.verify_token')
    @patch('app.exchange_cookie', return_value='lingxi-private')
    @patch('app.AppLoginSession')
    def test_reusing_phone_never_sends_sms_or_logs_in_again(self, factory, exchange, verify):
        application = create_app()
        http = application.test_client()
        flow = factory.return_value
        flow.send_sms.return_value = {'sent': True, 'phone_masked': '199****0000', 'retry_after': 60}
        flow.login.return_value = 'JSESSIONID=private-app'
        flow.user_info = {}
        http.post('/api/login/sms', json={'phone': '19900000000'})
        http.post('/api/login', json={'phone': '19900000000', 'code': '123456'})
        result = http.post('/api/login/sms', json={'phone': '19900000000'})
        self.assertTrue(result.json['reused'])
        flow.send_sms.assert_called_once()
        flow.login.assert_called_once()
        exchange.assert_called_once()
        records = http.get('/api/logins')
        self.assertNotIn(b'private-app', records.data)
        self.assertNotIn(b'19900000000', records.data)
        other = application.test_client()
        result = other.post('/api/login/select', json={'id': records.json['accounts'][0]['id']})
        self.assertTrue(result.json['verified'])
        flow.login.assert_called_once()

    @patch('app.verify_token')
    @patch('app.exchange_cookie', return_value='token')
    @patch('app.AppLoginSession')
    def test_pending_sms_is_shared_instead_of_resending_for_same_phone(self, factory, exchange, verify):
        import time
        application = create_app()
        one, two = application.test_client(), application.test_client()
        flow = factory.return_value
        def send(phone):
            flow.phone, flow.sms_sent_at = phone, time.monotonic()
            return {'sent': True, 'phone_masked': '199****0000', 'retry_after': 60}
        flow.send_sms.side_effect = send
        flow.login.return_value = 'JSESSIONID=private'
        flow.user_info = {}
        one.post('/api/login/sms', json={'phone': '19900000000'})
        two.post('/api/login/sms', json={'phone': '19900000000'})
        flow.send_sms.assert_called_once()
        two.post('/api/login', json={'phone': '19900000000', 'code': '123456'})
        one.post('/api/login', json={'phone': '19900000000', 'code': '123456'})
        flow.login.assert_called_once()


if __name__ == '__main__':
    unittest.main()
