from copy import deepcopy
from datetime import datetime,timezone,timedelta
import base64
import unittest
from unittest.mock import Mock

from credential_cache import CredentialCache
from coupon_notices import CouponNotices,SCOPE,INTERVAL
from tests.test_coupon_client import encoded,Response
from curl_cffi import requests


class Store:
    def __init__(self):self.data=None;self.credentials={}
    def load_activity_data(self,scope):return deepcopy(self.data)
    def save_activity_data(self,scope,data):self.data=deepcopy(data)
    def load_credentials(self,scope,login_id):return deepcopy(self.credentials.get((scope,login_id)))
    def save_credentials(self,scope,login_id,data):self.credentials[scope,login_id]=deepcopy(data)
    def delete_credentials(self,login_id=None,scope=None):self.credentials.pop((scope,login_id),None)


class CouponNoticeTests(unittest.TestCase):
    def setUp(self):
        self.clock=[datetime(2031,4,15,8,tzinfo=timezone(timedelta(hours=8))).timestamp()]
        self.store=Store();self.cache=CredentialCache(self.store,now=lambda:self.clock[0])
        self.snapshot={'id':'fixture','phone':'13800000000','profile':{'provinceCode':'851'}}
        self.cache.entries['coupon-wallet:12014','fixture']={'phone':self.snapshot['phone'],'cookies':[],
            'headers':{},'data':{},'expires_at':None,'created_at':self.clock[0],'generation':'fixture'}
        stamp=lambda t:datetime.fromtimestamp(t,timezone(timedelta(hours=8))).strftime('%Y%m%d%H%M%S')
        self.rows=[{'id':19,'title':'官方临时公告','start_time':stamp(self.clock[0]-60),
                    'end_time':stamp(self.clock[0]+3600),'operate_time':stamp(self.clock[0]-600)}]
        self.detail={'title':'服务安排更新','content':'<p>新的服务安排。</p><script>unsafe()</script><p>以本次通知为准。</p>'}
        self.calls=[];self.sessions=[];self.failure=False
        outer=self
        class Session:
            def __init__(self):self.headers={};self.cookies=requests.Cookies();self.closed=False;outer.sessions.append(self)
            def get(self,url,**options):
                outer.calls.append((url,options))
                if outer.failure:return Response({'retCode':'500003'})
                return Response(encoded(outer.detail if 'id' in options['params'] else outer.rows))
            def close(self):self.closed=True
        self.factory=Mock(side_effect=Session)
        self.service=CouponNotices(lambda:[self.snapshot],self.cache,self.store,now=lambda:self.clock[0],session_factory=self.factory)

    def test_live_list_and_detail_use_official_get_fields_and_reused_wallet_without_app_login(self):
        result=self.service.get()
        self.assertEqual(len(result['notices']),1)
        self.assertEqual(result['notices'][0]['title'],'服务安排更新')
        self.assertEqual(result['notices'][0]['content'],'新的服务安排。\n以本次通知为准。')
        self.assertNotIn('unsafe',str(result))
        self.assertEqual(len(self.calls),2)
        self.assertTrue(all(url.endswith('/i/v1/cust/noticeinfo/851') for url,_ in self.calls))
        params=self.calls[1][1]['params']
        self.assertEqual(base64.b64decode(params['id']),b'19')
        self.assertEqual(base64.b64decode(params['channelCode']),b'0003')
        self.assertNotIn('inParam',params)
        self.assertNotIn('phone',str(result));self.assertNotIn('13800000000',str(result))
        self.assertTrue(self.sessions[0].closed)

    def test_refresh_and_restart_reuse_cache_then_read_changed_official_content(self):
        self.service.get();self.service.get()
        restarted=CouponNotices(lambda:[self.snapshot],self.cache,self.store,now=lambda:self.clock[0],session_factory=self.factory)
        restarted.get();self.assertEqual(len(self.calls),2)
        self.clock[0]+=INTERVAL+1
        self.detail={'title':'另一场维护','content':'<p>新的维护时间，以官方本次返回为准。</p>'}
        result=restarted.get()
        self.assertEqual(len(self.calls),4)
        self.assertEqual(result['notices'][0]['title'],'另一场维护')
        self.assertIn('新的维护时间',result['notices'][0]['content'])

    def test_official_empty_list_clears_previous_notices(self):
        self.service.get();self.clock[0]+=INTERVAL+1;self.rows=[]
        result=self.service.get()
        self.assertEqual(result['notices'],[])
        self.assertFalse(result['stale'])

    def test_expired_cached_notice_disappears_without_waiting_for_network(self):
        self.service.get()
        self.store.data['851']['notices'][0]['end_at']=datetime.fromtimestamp(self.clock[0]+1,timezone.utc).isoformat()
        self.clock[0]+=2
        restarted=CouponNotices(lambda:[self.snapshot],self.cache,self.store,now=lambda:self.clock[0],session_factory=self.factory)
        self.assertEqual(restarted.get()['notices'],[])
        self.assertEqual(len(self.calls),2)

    def test_failed_notice_read_keeps_previous_content_and_does_not_reauthenticate_wallet(self):
        self.service.get();self.clock[0]+=INTERVAL+1;self.failure=True
        result=self.service.get();self.service.get()
        self.assertTrue(result['stale'])
        self.assertEqual(result['notices'][0]['title'],'服务安排更新')
        self.assertEqual(len(self.calls),3)
        self.assertIsNotNone(self.cache.load('coupon-wallet:12014',self.snapshot))
        self.assertIsNone(self.cache.load(SCOPE,self.snapshot))

    def test_missing_wallet_does_not_bootstrap_any_login_for_notices(self):
        self.cache.entries.clear()
        result=self.service.get()
        self.assertEqual(result['notices'],[])
        self.assertIn('不会为读取公告额外登录',result['message'])
        self.factory.assert_not_called()

    def test_notice_api_returns_cached_public_data_and_preserves_access_gate(self):
        from app import create_app
        app=create_app();client=app.test_client()
        app.extensions['coupon_notices'].get=Mock(return_value={'notices':[],'checked_at':None,'stale':False,'message':''})
        self.assertEqual(client.get('/api/coupons/notices').status_code,200)
        self.assertEqual(client.get('/api/coupons/notices',base_url='http://public.example:5500',
            environ_overrides={'REMOTE_ADDR':'2001:db8::2'}).status_code,428)
        app.extensions['coupon_notices'].get.assert_called_once()
