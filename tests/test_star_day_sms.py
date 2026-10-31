from copy import deepcopy
from datetime import datetime
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from http_transport import requests
from login_storage import DatabaseLoginStorage
from recharge.cmcc import SHANGHAI
from star_day_auth import StarDaySessionCache,StarDayAuthError,star_day_session
from star_day_crypto import sm3,encrypt_sms_phone,encrypt_login_value,P,A,PUBLIC
from star_day_sms import StarDaySmsLogin
from star_day_scheduler import StarDayScheduler
from star_day_client import StarDayClient
from tests.test_star_day import Queue


class SmsTests(unittest.TestCase):
    def setUp(self):
        self.stamp=1793475000
        self.cache=StarDaySessionCache(now=lambda:self.stamp)
        self.snapshot={'id':'a','phone':'13800000001','cookie':'UID=uid; JSESSIONID=session'}
        self.client=Mock(headers={},cookies=requests.Cookies())
        self.client.post.return_value=Mock(status_code=200,json=lambda:{'status':'1'})
        self.sms=StarDaySmsLogin(self.cache,session_factory=lambda:self.client,now=lambda:self.stamp)

    def test_crypto_matches_standard_sm3_vectors_and_official_curve(self):
        for message in (b'',b'abc',b'abcd'*16):
            self.assertEqual(sm3(message),hashlib.new('sm3',message).digest())
        x,y=PUBLIC
        self.assertEqual((y*y-x*x*x-A*x-0x28E9FA9E9D9F5E344D5A9E4BCF6509A7F39789F515AB8F92DDBCBD414D940E93)%P,0)
        self.assertEqual(encrypt_login_value('123456'),'F/QpecCBCSh/8dobHXi7ug==')
        with patch('star_day_crypto.secrets.randbelow',return_value=1):
            encrypted=encrypt_sms_phone('13800000001',self.stamp*1000)
        self.assertTrue(encrypted.startswith('04'))
        self.assertEqual(len(encrypted),2+128+len('MTM4MDAwMDAwMDE6MTc5MzQ3NTAwMDAwMA==')*2+64)

    def test_manual_send_is_deduplicated_and_does_not_call_primary_login(self):
        self.sms.send(self.snapshot);self.sms.send(self.snapshot)
        self.assertEqual(self.client.post.call_count,1)
        url=self.client.post.call_args.args[0]
        self.assertTrue(url.endswith('/new/cmpSmsSend.action'))
        data=self.client.post.call_args.kwargs['data']
        self.assertEqual(data['sspId'],'75628')
        self.assertNotIn(self.snapshot['phone'],data['phone'])

    def test_login_uses_same_sms_session_and_survives_primary_credential_rotation(self):
        self.sms.send(self.snapshot)
        self.snapshot['cookie']='UID=new; JSESSIONID=changed'
        self.client.cookies.set('access_token','sms-activity-cookie',domain='qqt.cmicrwx.cn')
        self.sms.login(self.snapshot,'123456')
        self.assertEqual(self.client.post.call_args.kwargs['data'],{
            'phone':encrypt_login_value(self.snapshot['phone']),'smsCode':encrypt_login_value('123456')})
        self.assertEqual(self.cache.load('a',self.snapshot['phone'])['expires_at'],self.stamp+7200)
        with patch('star_day_auth.AppLoginSession.from_credentials') as primary:
            fresh=Mock(headers={},cookies=requests.Cookies())
            with star_day_session(lambda:self.snapshot,session_factory=lambda:fresh,cache=self.cache,allow_login=False):pass
            primary.assert_not_called()
        self.assertNotIn('a',self.sms.pending)

    def test_missing_expired_invalid_or_other_account_code_never_submits(self):
        for code in ('','abc123','12345'):
            with self.assertRaises(StarDayAuthError):self.sms.login(self.snapshot,code)
        self.sms.send(self.snapshot);sent=self.client.post.call_count
        self.stamp+=601
        with self.assertRaises(StarDayAuthError):self.sms.login(self.snapshot,'123456')
        other={**self.snapshot,'id':'b'}
        with self.assertRaises(StarDayAuthError):self.sms.login(other,'123456')
        self.assertEqual(self.client.post.call_count,sent)

    def test_failed_login_never_caches_cookie_or_automatically_resends_sms(self):
        self.sms.send(self.snapshot)
        self.client.post.return_value=Mock(status_code=200,json=lambda:{'status':'0'})
        with self.assertRaises(StarDayAuthError):self.sms.login(self.snapshot,'123456')
        self.assertIsNone(self.cache.load('a',self.snapshot['phone']))
        self.assertEqual(self.client.post.call_count,2)

    def test_background_activity_never_builds_login_outside_preparation_or_page_entry(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        db=DatabaseLoginStorage(Path(temp.name)/'accounts.sqlite3',protect=lambda b:b,unprotect=lambda b:b)
        now=datetime(2026,11,1,7,0,tzinfo=SHANGHAI)
        queue=Queue()
        scheduler=StarDayScheduler(lambda _:deepcopy(self.snapshot),lambda:[{'id':'a','status':'active','phone_masked':'138****0001'}],storage=db,executor=queue,now=lambda:now)
        scheduler.client_factory=lambda provider:StarDayClient(provider,session_factory=lambda:self.client,session_cache=scheduler.sessions,allow_login=False)
        with patch('star_day_auth.AppLoginSession.from_credentials') as primary:
            for _ in range(5):scheduler.tick();queue.drain();scheduler.status();queue.drain()
            primary.assert_not_called();self.client.post.assert_not_called()
        self.assertEqual(scheduler.data['accounts']['a']['query_state'],'waiting_session')

    def test_web_sms_routes_restore_only_campaign_and_never_expose_tokens(self):
        from app import create_app
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        app=create_app(database_path=Path(temp.name)/'accounts.sqlite3')
        shared=app.extensions['unified_logins']
        saved=shared.remember('13800000001','UID=main; JSESSIONID=main',{},'',name='SMS test')
        original=shared.credentials(saved.id)
        scheduler=app.extensions['star_day_scheduler'];scheduler.executor=Queue()
        scheduler.sms.session_factory=lambda:self.client
        def post(url,**kwargs):
            if url.endswith('/login.action'):
                self.client.cookies.set('access_token','web-sms-private-token',domain='qqt.cmicrwx.cn')
            return Mock(status_code=200,json=lambda:{'status':'1'})
        self.client.post.side_effect=post
        http=app.test_client()
        self.assertEqual(http.post('/api/star-day/login/sms',json={'login_id':'missing'}).status_code,400)
        self.client.post.assert_not_called()
        self.assertEqual(http.post('/api/star-day/login/sms',json={'login_id':saved.id}).status_code,200)
        result=http.post('/api/star-day/login/submit',json={'login_id':saved.id,'code':'012345'})
        self.assertEqual(result.status_code,200)
        cached=scheduler.sessions.load(saved.id,saved.phone)
        self.assertEqual(cached['source'],'sms')
        self.assertEqual(shared.credentials(saved.id),original)
        public=http.get('/api/star-day/status')
        self.assertNotIn('web-sms-private-token',public.text)
        self.assertNotIn('012345',public.text)
        self.assertEqual(self.client.post.call_count,2)


if __name__=='__main__':unittest.main()
