"""Synthetic identities and mocked HTTP only; no real SMS or entitlement claims."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta
from http.cookies import SimpleCookie
from http.cookiejar import Cookie
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from flask import Flask

from app_login import AppLoginError
from credential_cache import CredentialCache
from http_transport import requests, RequestException
from mobile_rights import MobileRightsManager, register_mobile_rights
from mobile_rights_auth import SCOPE,ENTRY,APP_TOKEN_PATH,MobileRightsError,cookie_header,wap_session
from mobile_rights_client import RightsClient
from recharge.cmcc import SHANGHAI

NOW=datetime(2026,11,6,12,tzinfo=SHANGHAI)
SNAPSHOT={'id':'a','phone':'13800000000','cookie':'JSESSIONID=unit','profile':{},'app_context':{},'credential_revision':1}


class Response:
    def __init__(self,value,status=200):self.value,self.status_code,self.ok=value,status,status==200
    def json(self):return deepcopy(self.value)


class MemoryStorage:
    def __init__(self):self.credentials,self.activities={},{}
    def load_credentials(self,scope,owner):return deepcopy(self.credentials.get((scope,owner)))
    def save_credentials(self,scope,owner,value):self.credentials[scope,owner]=deepcopy(value)
    def delete_credentials(self,login_id=None,scope=None):self.credentials={key:value for key,value in self.credentials.items() if not((login_id is None or key[1]==login_id) and(scope is None or key[0]==scope))}
    def load_activity_data(self,key):return deepcopy(self.activities.get(key))
    def save_activity_data(self,key,value):self.activities[key]=deepcopy(value)


def order(identity='o1',status='5',*,name='会员权益',phone=SNAPSHOT['phone'],when=NOW):
    return {'orderid':identity,'rightName':name,'status':status,'userPhone':phone,'orderTime':int(when.timestamp()*1000)}


def inventory(rows):return Response({'X_RESULTCODE':'0','data':{'list':rows,'hasNext':False}})


class WapAuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.storage=MemoryStorage();self.cache=CredentialCache(self.storage)
        self.clients=[];self.flow=SimpleNamespace(cookie=SNAPSHOT['cookie'],call=Mock(return_value=(200,{'retCode':'000000','rspBody':{'token':'private-app-ticket'}},{})))
        self.factory=Mock(return_value=self.flow)
    def session(self,phone=SNAPSHOT['phone'],valid='true'):
        ss=requests.Session();self.addCleanup(ss.close);self.clients.append(ss)
        def post(url,**kwargs):
            self.assertEqual(url,SCOPE+'/login/leadeonLoginByToken')
            self.assertEqual(kwargs['data'],{'token':'private-app-ticket'})
            self.assertNotIn('Cookie',ss.headers)
            ss.cookies.set('WAP_SID','wap-auth',domain='wap.gz.10086.cn',path='/waps')
            return Response({'X_RESULTCODE':'0','X_RESULTINFO':'SUCCESS'})
        ss.post=Mock(side_effect=post)
        ss.get=Mock(return_value=Response({'X_RESULTCODE':'0','data':{'isValidate':valid,'serialNumber':phone}}))
        return ss
    def connect(self,snapshot=None,session=None,**kwargs):
        return wap_session(snapshot or deepcopy(SNAPSHOT),self.cache,app_factory=self.factory,session_factory=lambda:session or self.session(),**kwargs)
    def test_app_ticket_uses_official_sso_and_identity_metadata_without_sending_sms(self):
        ss=self.connect()
        self.flow.call.assert_called_once_with(APP_TOKEN_PATH,{'sourceId':'030011','url':ENTRY,'cellNum':SNAPSHOT['phone']})
        info=ss.get.call_args
        self.assertEqual(info.args[0],SCOPE+'/componant/initQueryLoginInfo')
        params=info.kwargs['params'];self.assertEqual(params['ajaxSubmitType'],'post');self.assertTrue(params['ajax_code'])
        cookies=SimpleCookie(info.kwargs['headers']['Cookie'])
        self.assertEqual(cookies[params['ajax_randomcode']].value,'0');self.assertEqual(cookies['WAP_SID'].value,'wap-auth')
        saved=self.cache.load(SCOPE,SNAPSHOT)
        self.assertEqual(saved['data'],{'serviceNum':SNAPSHOT['phone'],'method':'app'})
        self.assertNotIn('private-app-ticket',str(saved))
    def test_valid_wap_credentials_survive_primary_cookie_revision_changes_and_program_restart(self):
        first=self.connect();first.close()
        changed={**SNAPSHOT,'cookie':'JSESSIONID=rotated','credential_revision':99}
        self.cache=CredentialCache(self.storage)
        next_ss=self.session();self.connect(changed,next_ss)
        self.factory.assert_called_once();next_ss.post.assert_not_called();next_ss.get.assert_not_called()
        self.assertEqual(next_ss.cookies.get('WAP_SID'),'wap-auth')
    def test_identity_mismatch_never_caches_authentication(self):
        with self.assertRaises(MobileRightsError) as raised:self.connect(session=self.session('13900000000'))
        self.assertEqual(raised.exception.code,'WAP_IDENTITY_MISMATCH');self.assertIsNone(self.cache.load(SCOPE,SNAPSHOT))
    def test_read_only_app_recovery_is_bounded_and_uses_the_latest_identity(self):
        self.flow.call.side_effect=[(200,{'retCode':'400007'},{}),(200,{'retCode':'000000','rspBody':{'token':'private-app-ticket'}},{})]
        recover=Mock(return_value={**SNAPSHOT,'cookie':'JSESSIONID=new','credential_revision':2})
        self.connect(recover=recover)
        recover.assert_called_once();self.assertEqual(self.flow.call.call_count,2)
    def test_gateway_rejection_does_not_turn_into_a_primary_login_or_hidden_retry(self):
        ss=self.session();ss.post=Mock(return_value=Response({},403));recover=Mock()
        with self.assertRaises(MobileRightsError) as raised:self.connect(session=ss,recover=recover)
        self.assertEqual(raised.exception.code,'WAP_FORBIDDEN');recover.assert_not_called();ss.post.assert_called_once()
    def test_busy_shared_identity_recovery_is_reported_without_sending_another_ticket_or_sms(self):
        self.flow.call.return_value=(200,{'retCode':'400007'},{})
        recover=Mock(side_effect=AppLoginError('当前用券验证码尚未完成'))
        with self.assertRaises(MobileRightsError) as raised:self.connect(recover=recover)
        self.assertEqual(raised.exception.code,'APP_IDENTITY_BUSY');self.assertEqual(raised.exception.status,409)
        self.flow.call.assert_called_once();self.clients[0].post.assert_not_called()
    def test_nonce_is_request_private_and_cookie_path_and_domain_are_respected(self):
        ss=self.session();ss.cookies.set('WAP_SID','auth',domain='wap.gz.10086.cn',path='/waps')
        ss.cookies.set('0.111','0',domain='wap.gz.10086.cn',path='/waps');ss.cookies.set('absign','old',domain='wap.gz.10086.cn',path='/')
        ss.cookies.set('scoped','only-point',domain='wap.gz.10086.cn',path='/waps/pointClaim')
        ss.cookies.set('unrelated','secret',domain='example.invalid',path='/')
        before=[(c.name,c.value,c.path) for c in ss.cookies.jar]
        header=cookie_header(ss,'0.222',SCOPE+'/pointClaim/initQueryPointClaim');values=SimpleCookie(header)
        self.assertEqual(values['WAP_SID'].value,'auth');self.assertEqual(values['scoped'].value,'only-point')
        self.assertIn('0.222',values);self.assertNotIn('0.111',values);self.assertNotIn('absign',values);self.assertNotIn('unrelated',values)
        self.assertEqual(before,[(c.name,c.value,c.path) for c in ss.cookies.jar])
    def test_server_cookie_rotation_is_saved_without_a_new_ticket(self):
        ss=self.connect();ss.cookies.set('WAP_SID','new-auth',domain='wap.gz.10086.cn',path='/waps');self.cache.sync(ss)
        restored=self.connect(session=self.session())
        self.assertEqual(restored.cookies.get('WAP_SID'),'new-auth');self.factory.assert_called_once()
    def test_expired_exchange_ticket_does_not_expire_a_valid_opaque_wap_session(self):
        now=[1000];self.cache=CredentialCache(self.storage,now=lambda:now[0])
        first=self.session()
        first.cookies.jar.set_cookie(Cookie(0,'ticketId','short-exchange-ticket',None,False,'wap.gz.10086.cn',True,False,'/',True,True,1001,False,None,None,{},False))
        self.connect(session=first)
        self.assertIsNone(self.cache.load(SCOPE,SNAPSHOT)['expires_at'])
        now[0]=1100
        second=self.session();self.connect(session=second)
        self.factory.assert_called_once();second.post.assert_not_called()
        self.assertEqual(second.cookies.get('WAP_SID'),'wap-auth')
    def test_legacy_ticket_only_deadline_is_migrated_without_bootstrapping_again(self):
        self.connect()
        saved=self.storage.credentials[SCOPE,'a'];saved['expires_at']=1;saved.pop('expiry_cookies')
        self.cache=CredentialCache(self.storage)
        second=self.session();self.connect(session=second)
        self.factory.assert_called_once();second.post.assert_not_called();self.assertIsNone(self.cache.load(SCOPE,SNAPSHOT)['expires_at'])
    def test_real_wap_expiry_and_server_extension_are_saved_from_session_cookies(self):
        self.cache=CredentialCache(self.storage,now=lambda:1000)
        first=self.connect();cookie=Cookie(0,'WAP_SID','wap-auth',None,False,'wap.gz.10086.cn',True,False,'/waps',True,True,1500,False,None,None,{},False)
        first.cookies.jar.set_cookie(cookie)
        self.cache.sync(first);self.assertEqual(self.cache.load(SCOPE,SNAPSHOT)['expires_at'],1500)
        cookie.expires=2000;first.cookies.jar.set_cookie(cookie)
        self.cache.sync(first);self.assertEqual(self.cache.load(SCOPE,SNAPSHOT)['expires_at'],2000)


class RightsProtocolTests(unittest.TestCase):
    def setUp(self):
        self.ss=requests.Session();self.addCleanup(self.ss.close)
        self.ss.cookies.set('WAP_SID','auth',domain='wap.gz.10086.cn',path='/waps')
        self.attempts={}
        def before(order_id):
            if order_id in self.attempts:raise MobileRightsError('已提交','ALREADY_SUBMITTED',409)
            self.attempts[order_id]={'status':'unknown'}
        def finish(order_id,value):
            if value is None:self.attempts.pop(order_id,None)
            else:self.attempts[order_id]=deepcopy(value)
        self.client=RightsClient(self.ss,SNAPSHOT['phone'],before_claim=before,finish_claim=finish,claimed=lambda key:self.attempts.get(key),now=lambda:NOW)
    def test_current_month_query_is_read_only_deduplicates_and_keeps_unknown_status(self):
        old=order('old',when=NOW.replace(month=10));self.ss.get=Mock(return_value=inventory([order(),order(),order('done','1'),order('unknown','3'),old]))
        result=self.client.query(['未发放的权益'])
        self.assertEqual(len(result['items']),4);self.assertTrue(result['complete']);self.ss.get.assert_called_once()
        self.assertEqual(result['items'][2]['status'],'官方状态 3');self.assertEqual(result['items'][3]['status'],'本月未发放')
        self.assertTrue(result['items'][0]['can_claim']);self.assertNotEqual(result['items'][0]['id'],'o1')
        self.assertNotIn('rightReceive',self.ss.get.call_args.args[0])
    def test_only_an_order_from_this_successful_query_can_be_claimed_once(self):
        self.ss.get=Mock(side_effect=[inventory([order()]),Response({'X_RESULTCODE':'0','X_RESULTINFO':'成功'}),inventory([order()])])
        action=self.client.query()['items'][0]['id']
        with self.assertRaises(MobileRightsError):self.client.claim('foreign-order')
        result=self.client.claim(action);self.assertEqual(result['status'],'success')
        self.assertEqual(self.ss.get.call_args.kwargs['params']['orderId'],'o1')
        with self.assertRaises(MobileRightsError):self.client.claim(action)
        refreshed=self.client.query()['items'][0];self.assertFalse(refreshed['can_claim']);self.assertIn('领取成功',refreshed['status'])
        self.assertEqual(self.ss.get.call_count,3)
    def test_uncertain_submission_is_not_replayed_by_another_query(self):
        self.ss.get=Mock(side_effect=[inventory([order()]),RequestException('lost'),inventory([order()])])
        action=self.client.query()['items'][0]['id']
        with self.assertRaises(MobileRightsError) as raised:self.client.claim(action)
        self.assertEqual(raised.exception.code,'RIGHTS_RESULT_UNKNOWN')
        self.assertFalse(self.client.query()['items'][0]['can_claim']);self.assertEqual(self.ss.get.call_count,3)
    def test_empty_orders_support_tracking_and_failed_or_foreign_queries_clear_old_eligibility(self):
        self.ss.get=Mock(side_effect=[inventory([]),inventory([order()]),inventory([order(phone='13900000000')])])
        self.assertEqual(self.client.query(['关注权益'])['items'][0]['status'],'本月未发放')
        action=self.client.query()['items'][0]['id']
        with self.assertRaises(MobileRightsError) as raised:self.client.query()
        self.assertEqual(raised.exception.code,'ACCOUNT_MISMATCH')
        with self.assertRaises(MobileRightsError):self.client.claim(action)
        self.assertEqual(self.ss.get.call_count,3)


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.storage=MemoryStorage();self.cache=CredentialCache(self.storage);self.sessions=[]
        self.snapshots={'a':deepcopy(SNAPSHOT),'b':{**SNAPSHOT,'id':'b','phone':'13900000000'}}
        def connector(snapshot,cache,**kwargs):
            ss=requests.Session();self.addCleanup(ss.close)
            cache.connect(SCOPE,snapshot,ss,lambda:{'serviceNum':snapshot['phone']})
            self.sessions.append(ss);ss.get=Mock(return_value=inventory([]));return ss
        self.connector=Mock(side_effect=connector)
        self.accounts=lambda:[{'id':key,'name':key,'status':'active'} for key in self.snapshots]
        self.credentials=lambda key:deepcopy(self.snapshots.get(key))
        self.manager=MobileRightsManager(self.accounts,self.credentials,cache=self.cache,storage=self.storage,connector=self.connector)
        self.encrypt=patch('mobile_rights_youth.request_params',return_value={'6daEXCun':'synthetic'});self.encrypt.start();self.addCleanup(self.encrypt.stop)
    def test_shared_primary_revision_changes_do_not_replace_the_wap_context(self):
        context=self.manager._context('a');self.snapshots['a'].update(cookie='JSESSIONID=new',credential_revision=99)
        self.assertIs(self.manager._context('a'),context);self.connector.assert_called_once()
        self.assertIsNot(self.manager._context('b'),context);self.assertEqual(self.connector.call_count,2)
    def test_youth_can_finish_while_rights_query_is_still_running(self):
        context=self.manager._context('a');entered,release=Event(),Event()
        def response(url,**kwargs):
            if 'queryOrderList' in url:entered.set();release.wait(3);return inventory([])
            return Response({'X_RESULTCODE':'0','verticalClassVips':[]})
        context.session.get.side_effect=response
        with ThreadPoolExecutor(max_workers=2) as executor:
            rights=executor.submit(self.manager.query,'a');self.assertTrue(entered.wait(1))
            youth=executor.submit(self.manager.youth_query,'a')
            try:self.assertEqual(youth.result(timeout=1),{'coupons':[]});self.assertFalse(rights.done())
            finally:release.set()
            rights.result(timeout=2)
        self.connector.assert_called_once()
    def test_same_account_bootstrap_is_merged_across_concurrent_queries(self):
        entered,release=Event(),Event();original=self.connector.side_effect
        def connect(*args,**kwargs):entered.set();release.wait(3);return original(*args,**kwargs)
        self.connector.side_effect=connect
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures=[executor.submit(self.manager._context,'a') for _ in range(3)]
            self.assertTrue(entered.wait(1));self.assertTrue(self.manager.busy_login('a'));release.set();contexts=[future.result(timeout=2) for future in futures]
        self.assertTrue(all(context is contexts[0] for context in contexts));self.connector.assert_called_once()
        self.assertFalse(self.manager.busy_login('a'))
    def test_401_recovers_once_but_rate_limit_or_unknown_writes_do_not_reauthenticate(self):
        context=self.manager._context('a');context.session.get.return_value=Response({},401)
        result=self.manager.query('a');self.assertEqual(result['items'],[]);self.assertEqual(self.connector.call_count,2)
        current=self.manager._context('a');current.session.get.return_value=Response({'X_RESULTCODE':'-800','X_RESULTINFO':'请求频繁'})
        with self.assertRaises(MobileRightsError) as raised:self.manager.query('a')
        self.assertEqual(raised.exception.status,429);self.assertEqual(self.connector.call_count,2)
    def test_sms_bound_context_is_not_replaced_by_unrelated_failed_queries(self):
        context=self.manager._context('a');context.youth._selected=('member','coupon');context.session.get.return_value=Response({},401)
        with self.assertRaises(MobileRightsError) as raised:self.manager.query('a')
        self.assertEqual(raised.exception.code,'WAP_SESSION_BUSY');self.connector.assert_called_once()
        self.assertIs(self.manager._context('a'),context)
    def test_new_wap_expiry_envelope_recovers_once_for_each_read_module(self):
        for method in ('query','youth_query','point_query'):
            with self.subTest(method=method):
                self.manager.forget('a')
                context=self.manager._context('a')
                context.session.get.return_value=Response({'code':'0002','msg':'未登录或获取用户信息失败','data':None})
                fresh=self.connector.side_effect
                def reconnect(*args,**kwargs):
                    ss=fresh(*args,**kwargs)
                    ss.get.return_value=Response({'X_RESULTCODE':'0','data':{'list':[],'hasNext':False},
                                                  'verticalClassVips':[],'AbleDrawScore':'0','TotalBalance':'10'})
                    return ss
                with patch.object(self.manager,'connector',Mock(side_effect=reconnect)) as refreshed:
                    getattr(self.manager,method)('a')
                    refreshed.assert_called_once()
    def test_repeated_wap_expiry_stops_after_one_read_recovery(self):
        context=self.manager._context('a')
        expired=Response({'code':'0002','msg':'未登录或获取用户信息失败','data':None})
        context.session.get.return_value=expired
        original=self.connector.side_effect
        def reconnect(*args,**kwargs):
            ss=original(*args,**kwargs);ss.get.return_value=expired;return ss
        self.connector.side_effect=reconnect
        with self.assertRaises(MobileRightsError) as raised:self.manager.query('a')
        self.assertEqual(raised.exception.code,'WAP_LOGIN_REQUIRED');self.assertEqual(self.connector.call_count,2)
    def test_wap_expiry_during_claim_never_replays_the_submission(self):
        context=self.manager._context('a')
        context.session.get=Mock(side_effect=[inventory([order()]),Response({'code':'0002','msg':'未登录或获取用户信息失败','data':None})])
        action=self.manager.query('a')['items'][0]['id']
        with self.assertRaises(MobileRightsError) as raised:self.manager.claim('a',action)
        self.assertEqual(raised.exception.code,'WAP_LOGIN_REQUIRED')
        self.connector.assert_called_once();self.assertEqual(context.session.get.call_count,2)
    def test_tracking_and_unknown_submission_markers_persist_and_logout_only_forgets_one_account(self):
        self.manager.track('a','关注权益',True);self.manager.track('b','其他权益',True);self.manager._before_claim('a','order-a')
        rebuilt=MobileRightsManager(self.accounts,self.credentials,storage=self.storage,cache=self.cache,connector=self.connector)
        self.assertEqual(rebuilt._claimed('a','order-a')['status'],'unknown')
        self.manager.forget('a')
        self.assertNotIn('a',self.manager.data['tracked']);self.assertEqual(self.manager.data['tracked']['b'],['其他权益'])
    def test_routes_list_accounts_without_connecting_and_reject_unowned_actions(self):
        app=Flask(__name__);register_mobile_rights(app,self.accounts,self.credentials);manager=app.extensions['mobile_rights']
        client=app.test_client();manager.connector=self.connector
        self.assertEqual(client.get('/api/mobile-rights/accounts').status_code,200);self.connector.assert_not_called()
        self.assertEqual(client.post('/api/mobile-rights/claim',json={'login_id':'missing','id':'arbitrary'}).status_code,404)
        manager.claim=Mock(return_value={'status':'success'})
        result=client.post('/api/mobile-rights/claim',json={'login_id':'a','id':'server-owned','order_id':'inject','phone':'13900000000'})
        self.assertEqual(result.status_code,200);manager.claim.assert_called_once_with('a','server-owned')


if __name__=='__main__':unittest.main()
