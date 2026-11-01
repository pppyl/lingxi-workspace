from copy import deepcopy
from datetime import datetime
import sqlite3
import time
from unittest import TestCase, main
from unittest.mock import Mock, patch

from flask import Flask
from credential_cache import CredentialCache
from activities.checkin_common.client import WebCheckinError, _identity, SCOPE as WEB_SCOPE
from activities.checkin_common.scheduler import WebCheckinScheduler
from activities.wechat_checkin.auth import SCOPE, WechatLoginManager, client_for
from activities.wechat_checkin.plugin import register
from recharge.cmcc import SHANGHAI

SNAPSHOT = {'id':'a','phone':'13800000001','cookie':'APP-ONLY'}
ACCOUNT = {'id':'a','phone_masked':'138****0001','name':'测试','status':'active'}
OBSERVATION = {'today_signed':True,'sign_date':'2026-11-01','cycle_complete':False,
    'marked_times':1,'total_times':3,'channel':'wechat','prizes':[{'name':'0.5元话费券','status':'UNUSED'}]}


class WechatTests(TestCase):
    def test_exact_channel_required_even_for_same_number(self):
        for channel in ('app','dlwmh','wxmini'):
            with self.subTest(channel=channel), self.assertRaises(WebCheckinError):
                _identity({'mobile':SNAPSHOT['phone'],'channel':channel},SNAPSHOT,'wechat')
        self.assertEqual(_identity({'mobile':SNAPSHOT['phone'],'channel':'wechat'},SNAPSHOT,'wechat')['channel'],'wechat')
        with self.assertRaises(WebCheckinError):
            _identity({'mobile':SNAPSHOT['phone'],'channel':'wechat'},SNAPSHOT)

    def test_missing_wechat_session_does_not_use_app_or_web_session(self):
        cache = CredentialCache()
        for scope in ('qwhdmark:1021122301',WEB_SCOPE):
            cache.connect(scope,SNAPSHOT,Mock(cookies=Mock(jar=[])),lambda:{'channel':'other'})
        api = client_for(lambda:SNAPSHOT,cache)
        with patch.object(api,'_post') as post, self.assertRaises(WebCheckinError) as error:
            api.status()
        self.assertEqual(error.exception.code,'ACTIVITY_LOGIN_REQUIRED')
        post.assert_not_called()
        self.assertEqual(len(cache.entries),3)  # A missing lookup, not an authentication request.

    def test_known_signin_import_seeds_only_wechat_state_without_duplicate_query(self):
        class Storage:
            def __init__(self): self.values = {}
            def load_activity_data(self,key): return self.values.get(key)
            def save_activity_data(self,key,value): self.values[key] = deepcopy(value)
        storage, executor = Storage(), Mock()
        scheduler = WebCheckinScheduler(lambda _id:SNAPSHOT,lambda:[ACCOUNT],storage=storage,
            platform_id='wechat-checkin',executor=executor,now=lambda:datetime(2026,11,1,17,tzinfo=SHANGHAI))
        scheduler.logged_in('a',observation=OBSERVATION)
        scheduler.tick()
        executor.submit.assert_not_called()
        self.assertEqual(set(storage.values),{'wechat-checkin'})
        self.assertTrue(scheduler.status()['accounts'][0]['today_signed'])

    def test_old_persisted_auth_guidance_uses_current_sms_login_without_queries(self):
        scheduler = WebCheckinScheduler(lambda _id:SNAPSHOT,lambda:[ACCOUNT],executor=Mock(),
            platform_id='wechat-checkin',now=lambda:datetime(2026,11,1,18,tzinfo=SHANGHAI))
        scheduler.data['attempts'] = {'a':{'2026-11-01':{'state':'login_required',
            'code':'ACTIVITY_LOGIN_REQUIRED','message':'微信活动会话未授权或已失效，请在微信打开活动后同步授权。'}}}
        value = scheduler.status()['accounts'][0]
        self.assertIn('短信登录',value['message'])
        self.assertNotIn('同步授权',value['message'])
        self.assertTrue(value['activity_login_required'])
        scheduler.executor.submit.assert_not_called()


class WechatPluginTests(TestCase):
    def setUp(self):
        self.app = Flask('wechat-tests')
        self.app.config['TESTING'] = True
        self.controller = register(self.app,lambda _id:SNAPSHOT,lambda:[ACCOUNT],None)
        self.addCleanup(self.controller.stop)
        self.client = self.app.test_client()
        self.controller.login = Mock()
        self.controller.login.sms.return_value = {'message':'已发送','retry_after':60}
        self.controller.login.login.return_value = {'message':'已登录'}

    def test_polling_never_sends_sms_or_performs_login(self):
        self.assertEqual(self.client.get('/api/checkin-wechat/status').status_code,200)
        self.controller.login.sms.assert_not_called()
        self.controller.login.login.assert_not_called()
        self.assertEqual(self.client.post('/api/checkin-wechat/authorize',json={'id':'a'}).status_code,404)

    def test_explicit_sms_login_notifies_daily_work_only_after_login(self):
        with patch.object(self.controller.scheduler,'logged_in') as notify:
            response = self.client.post('/api/checkin-wechat/sms',json={'id':'a'})
            self.assertEqual(response.status_code,200)
            notify.assert_not_called()
            response = self.client.post('/api/checkin-wechat/login',json={'id':'a','code':'123456'})
            self.assertEqual(response.status_code,200)
            notify.assert_called_once_with('a')
        self.controller.login.sms.assert_called_once_with('a')
        self.controller.login.login.assert_called_once_with('a','123456')
        self.assertFalse(self.controller.auth_busy)

    def test_mismatch_does_not_notify_login_or_leave_busy(self):
        self.controller.login.login.side_effect = WebCheckinError('渠道不一致','CHANNEL_MISMATCH',409)
        with patch.object(self.controller.scheduler,'logged_in') as notify:
            response = self.client.post('/api/checkin-wechat/login',json={'id':'a','code':'123456'})
        self.assertEqual(response.status_code,409)
        notify.assert_not_called()
        self.assertFalse(self.controller.auth_busy)

    def test_wechat_page_uses_sms_and_owns_its_resources(self):
        response = self.client.get('/checkin/wechat')
        self.assertEqual(response.status_code,200)
        html = response.get_data(as_text=True)
        self.assertIn('data-checkin-channel="wechat"',html)
        self.assertIn('微信渠道短信登录',html)
        self.assertNotIn('同步微信授权',html)
        self.assertIn('/activities/wechat-checkin/static/web-checkin.js',html)
        with self.client.get('/activities/wechat-checkin/static/web-checkin.js') as resource:
            self.assertEqual(resource.status_code,200)

    def test_deleted_channel_blocks_authorization_and_clears_only_its_scope(self):
        self.controller.cache = Mock()
        self.controller.deleted = True
        self.controller.stop()
        self.controller.cache.forget.assert_called_once_with(scope=SCOPE)
        self.assertEqual(self.client.post('/api/checkin-wechat/sms',json={'id':'a'}).status_code,410)


if __name__ == '__main__':
    main()
