import unittest
import time
from copy import deepcopy
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

from app import create_app
from app_login import AppLoginError
from lingxi_scheduler import SHANGHAI
from recharge.accounts import AccountManager
from recharge.cmcc import ActivityClient
from recharge.scheduler import ClaimScheduler


class AccountCenterTests(unittest.TestCase):
    def setUp(self):
        self.clients = []
        def factory(profile_dir=None):
            client = ActivityClient(profile_dir)
            client.identity = Mock()
            def connect(cookie, phone, profile):
                client.app_material = {'cookie': cookie, 'phone': phone, 'profile': profile}
                client.mode = 'app'
                state = {'authenticated': True, 'mode': 'app', 'phone': phone[:3] + '****' + phone[-4:]}
                client.identity.return_value = state
                return state
            client.connect_app = Mock(side_effect=connect)
            self.clients.append(client)
            return client
        self.manager = AccountManager(default_client=factory(), client_factory=factory)
        self.scheduler = ClaimScheduler(self.manager)
        self.app = create_app(enable_recharge=True, recharge_manager=self.manager, recharge_scheduler=self.scheduler)
        self.app.testing = True
        self.http = self.app.test_client()
        self.http.get('/')
        store = self.app.extensions['unified_logins']
        self.a = store.remember('19900000001', 'JSESSIONID=private-a', {}, 'token-a', '主号')
        self.b = store.remember('19900000002', 'JSESSIONID=private-b', {}, 'token-b', '备用号')
        self.http.get('/recharge/')
        child = self.app.extensions['recharge_app']
        value = self.http.get_cookie('recharge_session', path='/recharge').value
        csrf = child.session_interface.get_signing_serializer(child).loads(value)['csrf']
        self.headers = {'X-CSRF-Token': csrf}

    def states(self):
        with self.http.session_transaction() as state:
            sid = state['sid']
        return {key[1]: value for key, value in self.app.extensions['lingxi_chat_states'].items() if key[0] == sid}

    @patch('app.verify_token')
    def test_lingxi_switch_preserves_each_account_conversation(self, verify):
        self.http.post('/api/login/select', json={'id': self.a.id})
        a_session = self.states()[self.a.id].session_id
        self.http.post('/api/login/select', json={'id': self.b.id})
        b_session = self.states()[self.b.id].session_id
        self.http.post('/api/login/select', json={'id': self.a.id})
        self.assertEqual(self.states()[self.a.id].session_id, a_session)
        self.assertNotEqual(a_session, b_session)
        self.http.post('/api/conversation/new', json={'login_id': self.b.id})
        self.assertEqual(self.states()[self.a.id].session_id, a_session)
        self.assertNotEqual(self.states()[self.b.id].session_id, b_session)

    @patch('app.verify_token')
    def test_chat_uses_explicit_account_even_if_another_tab_selected_one(self, verify):
        self.http.post('/api/login/select', json={'id': self.a.id})
        self.http.post('/api/login/select', json={'id': self.b.id})
        seen = []
        def reply(account, message):
            seen.append(account.app_phone)
            yield {'type': 'delta', 'text': 'reply'}
            yield {'type': 'done'}
        with patch('app.chat', side_effect=reply):
            response = self.http.post('/api/chat', json={'login_id': self.a.id, 'message': 'hello'})
            self.assertIn(b'reply', response.data)
        self.assertEqual(seen, ['19900000001'])

    @patch('app.verify_token')
    def test_activity_can_choose_b_while_lingxi_uses_a_and_deduplicates(self, verify):
        self.http.post('/api/login/select', json={'id': self.a.id})
        result = self.http.post('/recharge/api/login/attach', json={'login_id': self.b.id}, headers=self.headers)
        self.assertEqual(result.status_code, 200)
        target_id = result.json['account_id']
        self.clients[0].connect_app.assert_called_once_with(cookie='JSESSIONID=private-b', phone='19900000002', profile={})
        self.assertEqual(self.http.get('/api/status').json['phone_masked'], '199****0001')
        again = self.http.post('/recharge/api/login/attach', json={'login_id': self.b.id}, headers=self.headers)
        self.assertEqual(again.json['account_id'], target_id)
        self.clients[0].connect_app.assert_called_once()
        self.assertFalse(self.scheduler.snapshots())
        other = self.http.post('/recharge/api/login/attach', json={'login_id': self.a.id}, headers=self.headers)
        self.assertEqual(other.status_code, 200)
        self.assertNotEqual(other.json['account_id'], target_id)
        self.assertEqual(len(self.manager.accounts), 2)

    @patch('app.verify_token')
    def test_center_logout_only_clears_target_account_in_both_features(self, verify):
        for saved in (self.a, self.b):
            self.http.post('/api/login/select', json={'id': saved.id})
            self.http.post('/recharge/api/login/attach', json={'login_id': saved.id}, headers=self.headers)
        checkin = self.app.extensions['checkin_scheduler']
        for saved in (self.a, self.b):
            checkin.save({'login_ids': [saved.id], 'signin_enabled': True, 'flash_enabled': False})
        response = self.http.post('/api/logins/logout', json={'id': self.a.id})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.states()[self.a.id].auth_code)
        self.assertTrue(self.states()[self.b.id].auth_code)
        self.assertEqual(self.http.get('/api/status').json['phone_masked'], '199****0002')
        self.assertEqual(len(self.http.get('/api/logins').json['accounts']), 1)
        self.assertEqual(self.clients[0].mode, 'none')
        self.assertEqual(self.clients[1].mode, 'app')
        self.assertTrue(checkin.data['tasks'])
        self.assertTrue(all(task['login_id'] == self.b.id for task in checkin.data['tasks'].values()))

    @patch('app.verify_token')
    def test_center_cannot_logout_an_account_with_a_live_reply(self, verify):
        self.http.post('/api/login/select', json={'id': self.a.id})
        account = self.states()[self.a.id]
        self.http.post('/api/login/select', json={'id': self.b.id})
        account.lock.acquire()
        try:
            response = self.http.post('/api/logins/logout', json={'id': self.a.id})
            self.assertEqual(response.status_code, 409)
            self.assertIsNotNone(self.app.extensions['unified_logins'].get_id(self.a.id))
        finally:
            account.lock.release()

    def test_center_renames_without_exposing_credentials(self):
        response = self.http.post('/api/logins/rename', json={'id': self.b.id, 'name': '工作号码'})
        self.assertEqual(response.json['account']['name'], '工作号码')
        records = self.http.get('/api/logins')
        for private in (b'private-a', b'private-b', b'token-a', b'token-b', b'19900000001'):
            self.assertNotIn(private, records.data)

    @patch('app.verify_token')
    @patch('app.exchange_cookie', return_value='new-token')
    @patch('app.AppLoginSession')
    def test_manager_login_does_not_change_lingxi_selection(self, factory, exchange, verify):
        self.http.post('/api/login/select', json={'id': self.a.id})
        flow = factory.return_value
        flow.login.return_value = 'JSESSIONID=third'
        flow.user_info = {}
        response = self.http.post('/api/logins/login', json={'phone': '19900000003', 'code': '123456', 'name': '第三个'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['account']['name'], '第三个')
        self.assertEqual(len(self.http.get('/api/logins').json['accounts']), 3)
        self.assertEqual(self.http.get('/api/status').json['phone_masked'], '199****0001')

    @patch('app.AppLoginSession')
    def test_different_phones_keep_their_own_pending_sms_flow(self, factory):
        flows = []
        def make_flow():
            flow = Mock()
            def send(phone):
                import time
                flow.phone, flow.sms_sent_at = phone, time.monotonic()
                return {'sent': True, 'phone_masked': phone[:3] + '****' + phone[-4:], 'retry_after': 60}
            flow.send_sms.side_effect = send
            flows.append(flow)
            return flow
        factory.side_effect = make_flow
        for phone in ('19900000003', '19900000004'):
            self.assertEqual(self.http.post('/api/logins/sms', json={'phone': phone}).status_code, 200)
        store = self.app.extensions['unified_logins']
        self.assertIsNot(store.pending['19900000003'], store.pending['19900000004'])
        self.assertEqual(store.pending['19900000003'].phone, '19900000003')
        self.assertEqual(store.pending['19900000004'].phone, '19900000004')

    def sms_flow(self):
        flow = Mock(user_info={})
        def send(phone):
            flow.phone, flow.sms_sent_at = phone, time.monotonic()
            return {'sent': True, 'phone_masked': phone[:3] + '****' + phone[-4:], 'retry_after': 60}
        flow.send_sms.side_effect = send
        flow.login.return_value = 'JSESSIONID=renewed'
        flow.export_context.return_value = {'refreshToken': 'new-refresh'}
        return flow

    @patch('app.verify_token')
    @patch('app.exchange_cookie', return_value='renewed-token')
    @patch('app.AppLoginSession')
    def test_relogin_uses_saved_phone_and_preserves_account_and_feature_bindings(self, factory, exchange, verify):
        flow = factory.return_value = self.sms_flow()
        store = self.app.extensions['unified_logins']
        self.http.post('/api/login/select', json={'id': self.b.id})
        attached = self.http.post('/recharge/api/login/attach', json={'login_id': self.a.id}, headers=self.headers).json
        scheduler = self.app.extensions['lingxi_scheduler']
        task_id = scheduler.save({'login_ids': [self.a.id], 'content': 'scheduled message',
                                  'run_at': (datetime.now(SHANGHAI) + timedelta(days=1)).isoformat()})[0]
        original_task = deepcopy(scheduler.jobs[task_id])
        original_id, original_created, revision = self.a.id, self.a.created_at, self.a.credential_revision
        store.mark(self.a, 'login_required', '需要短信验证')
        response = self.http.post('/api/logins/relogin/sms', json={'id': self.a.id, 'phone': self.b.phone})
        self.assertEqual(response.status_code, 200)
        flow.send_sms.assert_called_once_with(self.a.phone)
        response = self.http.post('/api/logins/relogin', json={'id': self.a.id, 'code': '123456', 'phone': self.b.phone, 'name': '不应改名'})
        self.assertEqual(response.status_code, 200)
        flow.login.assert_called_once_with(self.a.phone, '123456')
        self.assertIs(store.get_id(original_id), self.a)
        self.assertEqual((self.a.name, self.a.created_at, self.a.status), ('主号', original_created, 'active'))
        self.assertEqual(self.a.credential_revision, revision + 1)
        self.assertEqual(self.a.token, 'renewed-token')
        self.assertEqual(self.a.app_context['refreshToken'], 'new-refresh')
        self.assertEqual(len(store.records), 2)
        self.assertEqual(scheduler.jobs[task_id], original_task)
        self.assertNotIn(self.a.phone, store.pending)
        self.assertEqual(self.http.get('/api/status').json['phone_masked'], '199****0002')
        again = self.http.post('/recharge/api/login/attach', json={'login_id': self.a.id}, headers=self.headers)
        self.assertEqual(again.json['account_id'], attached['account_id'])
        self.clients[0].connect_app.assert_called_with(cookie='JSESSIONID=renewed', phone=self.a.phone, profile={})
        for private in (b'19900000001', b'JSESSIONID=renewed', b'renewed-token', b'new-refresh'):
            self.assertNotIn(private, response.data)

    @patch('app.AppLoginSession')
    def test_relogin_reuses_pending_code_and_explicit_resend_uses_same_flow(self, factory):
        flow = factory.return_value = self.sms_flow()
        self.app.extensions['unified_logins'].mark(self.a, 'login_required')
        for _ in range(2):
            self.assertEqual(self.http.post('/api/logins/relogin/sms', json={'id': self.a.id}).status_code, 200)
        flow.send_sms.assert_called_once_with(self.a.phone)
        self.assertEqual(self.http.post('/api/logins/relogin/sms', json={'id': self.a.id, 'resend': True}).status_code, 200)
        self.assertEqual(flow.send_sms.call_count, 2)
        factory.assert_called_once()

    @patch('app.verify_token')
    @patch('app.AppLoginSession')
    def test_relogin_reuses_account_if_login_has_already_recovered(self, factory, verify):
        response = self.http.post('/api/logins/relogin/sms', json={'id': self.a.id})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json['reused'])
        factory.assert_not_called()

    @patch('app.verify_token')
    @patch('app.exchange_cookie', return_value='renewed-token')
    @patch('app.AppLoginSession')
    def test_explicit_relogin_refreshes_an_active_account_without_changing_its_identity(self, factory, exchange, verify):
        flow = factory.return_value = self.sms_flow()
        original = self.a.id, self.a.name, self.a.created_at, self.a.credential_revision
        before_cookie = self.a.cookie
        response = self.http.post('/api/logins/relogin/sms', json={
            'id': self.a.id, 'force': True, 'phone': self.b.phone})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json['sent'])
        flow.send_sms.assert_called_once_with(self.a.phone)
        self.assertEqual(self.a.cookie, before_cookie)
        response = self.http.post('/api/logins/relogin', json={
            'id': self.a.id, 'force': True, 'code': '123456', 'name': '不应改名'})
        self.assertEqual(response.status_code, 200)
        flow.login.assert_called_once_with(self.a.phone, '123456')
        self.assertEqual((self.a.id, self.a.name, self.a.created_at), original[:3])
        self.assertEqual(self.a.credential_revision, original[3]+1)
        self.assertEqual(self.a.token, 'renewed-token')
        self.assertEqual(len(self.app.extensions['unified_logins'].records), 2)

    @patch('app.AppLoginSession')
    def test_relogin_rejects_missing_or_logged_out_account_without_sending(self, factory):
        self.app.extensions['unified_logins'].forget(self.a.phone)
        for target in (self.a.id, 'missing'):
            for route in ('/api/logins/relogin/sms', '/api/logins/relogin'):
                self.assertEqual(self.http.post(route, json={'id': target, 'code': '123456'}).status_code, 404)
        factory.assert_not_called()
        self.assertEqual(len(self.app.extensions['unified_logins'].records), 1)

    @patch('app.verify_token')
    @patch('app.exchange_cookie', return_value='renewed-token')
    @patch('app.AppLoginSession')
    def test_relogin_wrong_code_can_retry_without_replacing_the_account(self, factory, exchange, verify):
        flow = factory.return_value = self.sms_flow()
        flow.login.side_effect = [AppLoginError('验证码错误'), 'JSESSIONID=renewed']
        store = self.app.extensions['unified_logins']
        store.mark(self.a, 'login_required')
        before = self.a.cookie, self.a.token, self.a.credential_revision
        self.http.post('/api/logins/relogin/sms', json={'id': self.a.id})
        failed = self.http.post('/api/logins/relogin', json={'id': self.a.id, 'code': '000000'})
        self.assertEqual(failed.status_code, 400)
        self.assertEqual((self.a.cookie, self.a.token, self.a.credential_revision), before)
        self.assertEqual(self.a.status, 'login_required')
        exchange.assert_not_called()
        self.assertIs(store.pending[self.a.phone], flow)
        retry = self.http.post('/api/logins/relogin', json={'id': self.a.id, 'code': '123456'})
        self.assertEqual(retry.status_code, 200)
        self.assertIs(store.get_id(self.a.id), self.a)
        self.assertEqual(self.a.status, 'active')
        flow.send_sms.assert_called_once()


if __name__ == '__main__':
    unittest.main()
