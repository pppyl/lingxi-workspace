from copy import deepcopy
from threading import Event, RLock
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from flask import Flask

from feature_control import FeatureControl
from feature_runtime import FeatureLease, ThreadPoolExecutor, feature_running, feature_scope
from http_transport import FeatureSession, RequestException


class Store:
    def __init__(self):
        self.data = {}

    def load_activity_data(self, key):
        return deepcopy(self.data.get(key))

    def save_activity_data(self, key, value):
        self.data[key] = deepcopy(value)


class FeatureControlTests(unittest.TestCase):
    def setUp(self):
        from pathlib import Path
        self.app = Flask('feature-test', template_folder=str(Path(__file__).resolve().parents[1]/'templates'))
        self.store = Store()
        self.control = FeatureControl(self.app, self.store)
        self.business = Mock(return_value={'ok': True})
        self.app.add_url_rule('/api/star-day/probe', endpoint='business', view_func=lambda:self.business())

    def test_defaults_and_persistent_independent_switches(self):
        self.assertTrue(all(row['enabled'] for row in self.control.public()))
        self.control.set_enabled('star-day', False)
        self.assertTrue(self.control.enabled('coupons'))
        restored = FeatureControl(Flask('restart'), self.store)
        self.assertFalse(restored.enabled('star-day'))
        self.assertTrue(restored.enabled('lingxi'))

    def test_custom_order_survives_restart_without_touching_switches_or_leases(self):
        self.control.set_enabled('star-day',False)
        lease=FeatureLease(self.control,'lingxi')
        ids=self.control.default_order();ordered=[ids[-1],*ids[:-1]]
        response=self.app.test_client().post('/api/features/order',json={'feature_ids':ordered})
        self.assertEqual(response.status_code,200)
        self.assertEqual([row['id'] for row in response.json['features']],ordered)
        self.assertEqual(response.json['default_order'],ids)
        self.assertTrue(lease.allowed());self.business.assert_not_called()
        restored=FeatureControl(Flask('reordered'),self.store)
        self.assertEqual([row['id'] for row in restored.public()],ordered)
        self.assertFalse(restored.enabled('star-day'))

    def test_order_validation_never_discards_features_and_defaults_can_be_restored(self):
        client=self.app.test_client();ids=self.control.default_order()
        for payload in (None,{}, {'feature_ids':'wrong'},{'feature_ids':[ids[0],ids[0]]},{'feature_ids':[None]}):
            self.assertEqual(client.post('/api/features/order',json=payload).status_code,400)
        for values in (ids[:-1],[*ids,'unknown']):
            self.assertEqual(client.post('/api/features/order',json={'feature_ids':values}).status_code,409)
        self.assertNotIn('feature-order',self.store.data)
        client.post('/api/features/order',json={'feature_ids':list(reversed(ids))})
        result=client.post('/api/features/order',json={'feature_ids':ids})
        self.assertEqual([row['id'] for row in result.json['features']],ids)

    def test_new_modules_append_and_removed_modules_do_not_block_future_ordering(self):
        client=self.app.test_client();ids=self.control.default_order()
        client.post('/api/features/order',json={'feature_ids':list(reversed(ids))})
        platform={'state':'running','metadata':{},'navigation':'main'}
        self.control.register('extra','活动','/extra',('/extra',),platform=platform)
        self.assertEqual(self.control.public()[-1]['id'],'extra')
        new_order=['extra',*reversed(ids)]
        self.assertEqual(client.post('/api/features/order',json={'feature_ids':new_order}).status_code,200)
        platform['state']='deleted'
        self.assertNotIn('extra',self.control.default_order())
        self.assertEqual(client.post('/api/features/order',json={'feature_ids':ids}).status_code,200)

    def test_corrupt_order_is_not_silently_overwritten(self):
        self.store.data['feature-order']=['lingxi','lingxi']
        from login_storage import LoginStorageError
        with self.assertRaises(LoginStorageError):FeatureControl(Flask('bad-order'),self.store)
        self.assertEqual(self.store.data['feature-order'],['lingxi','lingxi'])

    def test_disabled_api_never_enters_business_and_returns_specific_reason(self):
        self.control.set_enabled('star-day', False)
        response = self.app.test_client().get('/api/star-day/probe')
        self.assertEqual(response.status_code, 423)
        self.assertEqual(response.json['code'], 'FEATURE_DISABLED')
        self.assertEqual(response.headers['X-Workspace-Feature-Disabled'], 'star-day')
        self.business.assert_not_called()
        self.control.set_enabled('star-day', True)
        self.assertEqual(self.app.test_client().get('/api/star-day/probe').status_code, 200)
        self.business.assert_called_once()

    def test_switch_route_validates_payload_and_keeps_administration_available(self):
        client = self.app.test_client()
        for value in ({}, {'enabled':'false'}, {'enabled':0}, None):
            self.assertEqual(client.post('/api/features/star-day', json=value).status_code, 400)
        self.assertEqual(client.post('/api/features/no-such-module', json={'enabled':False}).status_code, 400)
        self.assertEqual(client.post('/api/features/star-day', json={'enabled':False}).status_code, 200)
        self.assertEqual(client.get('/api/features').status_code, 200)

    def test_independent_channel_uses_its_own_switch(self):
        self.control.register('wechat-checkin', '微信渠道签到', '/checkin/wechat', ('/checkin/wechat','/api/checkin-wechat'))
        self.control.set_enabled('checkin', False)
        self.assertEqual(self.control.identify('/checkin/wechat'), 'wechat-checkin')
        self.assertEqual(self.control.identify('/api/checkin-wechat/status'), 'wechat-checkin')
        self.assertTrue(self.control.enabled('wechat-checkin'))

    def test_pause_cancels_waiting_rounds_but_does_not_delete_saved_plans(self):
        schedule = {'enabled':True, 'at':'2031-01-01T10:00:00+08:00', 'product':'original'}
        scheduler = SimpleNamespace(lock=RLock(), jobs={'job':schedule}, cancellations={'job':Event()},
                                    tick=Mock(), workers={}, running=set())
        self.control.bind_scheduler('star-day', scheduler, background=('tick',))
        scheduler.tick()
        self.control.set_enabled('star-day', False)
        scheduler.tick()
        self.assertEqual(scheduler.tick.__wrapped__.call_count, 1)
        self.assertTrue(scheduler.cancellations['job'].is_set())
        self.assertEqual(scheduler.jobs['job'], schedule)
        self.control.set_enabled('star-day', True)
        scheduler.tick()
        self.assertEqual(scheduler.tick.__wrapped__.call_count, 2)
        self.assertEqual(scheduler.cancellations, {})

    def test_paused_scope_cannot_send_even_after_a_quick_reenable(self):
        lease = FeatureLease(self.control, 'star-day')
        with feature_scope(lease):
            session = FeatureSession()
            self.addCleanup(session.close)
            self.control.set_enabled('star-day', False)
            self.control.set_enabled('star-day', True)
            with patch('http_transport._transport_requests.Session.request') as send:
                with self.assertRaises(RequestException):
                    session.get('https://unused.invalid/')
                send.assert_not_called()
            # A new operation can reuse the same session with a fresh lease.
            with feature_scope(FeatureLease(self.control, 'star-day')):
                with patch('http_transport._transport_requests.Session.request', return_value='ok') as send:
                    self.assertEqual(session.get('https://unused.invalid/'), 'ok')
                    send.assert_called_once()

    def test_nested_pool_and_sessions_retain_feature_permission(self):
        lease = FeatureLease(self.control, 'star-day')
        with feature_scope(lease), ThreadPoolExecutor(max_workers=1) as pool:
            self.assertTrue(pool.submit(feature_running).result())
            session = pool.submit(FeatureSession).result()
            self.addCleanup(session.close)
            self.control.set_enabled('star-day', False)
            with patch('http_transport._transport_requests.Session.request') as send:
                with self.assertRaises(RequestException):
                    pool.submit(session.get, 'https://unused.invalid/').result()
                send.assert_not_called()

    def test_unscoped_primary_login_remains_independent(self):
        self.control.set_enabled('star-day', False)
        with FeatureSession() as session, patch('http_transport._transport_requests.Session.request', return_value='ok'):
            self.assertEqual(session.get('https://unused.invalid/'), 'ok')

    def test_disabling_a_live_burst_stops_all_following_rounds(self):
        from checkin_flash import run_flash_burst
        from tests.test_checkin_flash import Prepared
        def reply(_index):
            self.control.set_enabled('star-day', False)
            return {'state':'retryable', 'message':'繁忙'}
        prepared = Prepared(reply, delay=0, wait=0)
        with feature_scope(FeatureLease(self.control, 'star-day')):
            result = run_flash_burst(prepared, {'burst_count':5,'burst_workers':1,'burst_interval':.05},
                lambda:None, lambda:False, followup_seconds=None)
        self.assertEqual(len(prepared.starts), 1)
        self.assertEqual(result['attempt_count'], 1)


class AppFeatureTests(unittest.TestCase):
    def test_real_app_blocks_every_business_route_and_mounted_recharge(self):
        from app import create_app
        app = create_app(enable_recharge=True)
        features = app.extensions['feature_control']
        client = app.test_client()
        cases = [('lingxi','/api/lingxi/tasks'),('star-day','/api/star-day/status'),
                 ('checkin','/api/checkin/status'),('monthly-coupon','/api/monthly-coupon/status'),
                 ('coupons','/api/coupons/accounts'),('seven-day','/api/seven-day/status'),
                 ('wechat-checkin','/api/checkin-wechat/status'),('recharge','/recharge/api/accounts')]
        with patch('http_transport._transport_requests.Session.request', side_effect=AssertionError('unexpected HTTP')):
            for feature_id, url in cases:
                features.set_enabled(feature_id, False)
                self.assertEqual(client.get(url).status_code, 423, url)
            self.assertEqual(client.get('/accounts').status_code, 200)
            self.assertEqual(client.get('/platforms').status_code, 200)
            self.assertEqual(client.get('/api/features').status_code, 200)
            self.assertIn('已关闭', client.get('/star-day').text)
            self.assertIn('已关闭', client.get('/recharge/').text)
            self.assertEqual(client.post('/api/star-day/login/sms', json={'login_id':'unused'}).status_code, 423)


if __name__ == '__main__':
    unittest.main()
