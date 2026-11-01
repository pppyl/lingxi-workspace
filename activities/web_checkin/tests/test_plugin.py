from unittest.mock import Mock, patch
import unittest

from flask import Flask, render_template_string

from activities.web_checkin.client import WebCheckinError
from activities.web_checkin.plugin import Controller, SCOPE, register


class PluginTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask('web-checkin-plugin')
        self.app.config['TESTING'] = True
        self.scheduler = Mock()
        self.scheduler.status.return_value = {'settings': {'enabled': True, 'time': '08:00'}, 'accounts': []}
        self.login = Mock()
        self.login.sms.return_value = {'message': '已发送', 'retry_after': 60}
        self.login.login.return_value = {'message': '网页登录完成'}
        self.scheduler_patch = patch('activities.web_checkin.plugin.WebCheckinScheduler', return_value=self.scheduler)
        self.login_patch = patch('activities.web_checkin.plugin.WebLoginManager', return_value=self.login)
        self.scheduler_patch.start()
        self.login_patch.start()
        self.addCleanup(self.scheduler_patch.stop)
        self.addCleanup(self.login_patch.stop)
        self.controller = register(self.app, Mock(), lambda: [], None)
        self.client = self.app.test_client()

    def test_get_status_is_local_and_does_not_send_sms_or_login(self):
        result = self.client.get('/api/checkin-web/status')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json['accounts'], [])
        self.login.sms.assert_not_called()
        self.login.login.assert_not_called()

    def test_sms_is_only_sent_on_explicit_post(self):
        result = self.client.post('/api/checkin-web/sms', json={'id': 'a'})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json['retry_after'], 60)
        self.login.sms.assert_called_once_with('a')
        self.login.login.assert_not_called()
        self.assertFalse(self.controller.auth_busy)

    def test_login_success_notifies_pending_daily_work(self):
        result = self.client.post('/api/checkin-web/login', json={'id': 'a', 'code': '123456'})
        self.assertEqual(result.status_code, 200)
        self.login.login.assert_called_once_with('a', '123456')
        self.scheduler.logged_in.assert_called_once_with('a')

    def test_failed_login_does_not_mark_success_or_schedule_and_releases_busy(self):
        self.login.login.side_effect = WebCheckinError('验证码错误', 'SMS_INVALID', 409)
        result = self.client.post('/api/checkin-web/login', json={'id': 'a', 'code': '123456'})
        self.assertEqual(result.status_code, 409)
        self.assertEqual(result.json['code'], 'SMS_INVALID')
        self.scheduler.logged_in.assert_not_called()
        self.assertFalse(self.controller.auth_busy)

    def test_cooldown_response_is_exposed_to_frontend(self):
        error = WebCheckinError('稍后再试', 'SMS_COOLDOWN', 429)
        error.retry_after = 32
        self.login.sms.side_effect = error
        result = self.client.post('/api/checkin-web/sms', json={'id': 'a'})
        self.assertEqual(result.status_code, 429)
        self.assertEqual(result.json['retry_after'], 32)

    def test_deleted_platform_blocks_backend_operations_and_hides_entry(self):
        self.controller.deleted = True
        self.assertEqual(self.client.get('/api/checkin-web/status').status_code, 410)
        self.assertEqual(self.client.post('/api/checkin-web/sms', json={'id': 'a'}).status_code, 410)
        with self.app.test_request_context():
            self.assertEqual(render_template_string('{{ checkin_web_available }}'), 'False')
        self.login.sms.assert_not_called()

    def test_forget_cleans_activity_and_login_manager_scope(self):
        self.scheduler.busy_login.return_value = False
        self.controller.forget('a')
        self.scheduler.forget.assert_called_once_with('a')
        self.login.forget.assert_called_once_with('a')

    def test_active_authentication_prevents_account_deletion(self):
        self.controller.auth_busy.add('a')
        with self.assertRaises(WebCheckinError):
            self.controller.forget('a')
        self.scheduler.forget.assert_not_called()

    def test_shutdown_retains_valid_cached_credentials_but_delete_clears_correct_scope(self):
        self.controller.cache = Mock()
        self.controller.stop()
        self.controller.cache.forget.assert_not_called()
        self.login.close.assert_called_once_with()
        self.controller.deleted = True
        self.controller.stop()
        self.controller.cache.forget.assert_called_once_with(scope=SCOPE)

    def test_invalid_json_rejected_before_login_side_effect(self):
        for path in ('run', 'sms', 'login'):
            with self.subTest(path=path):
                result = self.client.post('/api/checkin-web/'+path, json=[])
                self.assertEqual(result.status_code, 400)
        self.login.sms.assert_not_called()
        self.login.login.assert_not_called()


if __name__ == '__main__':
    unittest.main()
