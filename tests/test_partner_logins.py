from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from flask import Flask

from features.checkin.partner_logins import PartnerLogins,parse_import,register_partner_logins,TAOBAO_SCOPE,BAIDU_SCOPE
from features.checkin.client import CheckinError
from workspace.accounts.credentials import CredentialCache
from workspace.http_transport import requests


class MemoryStorage:
    path=Path('/isolated/data/accounts.sqlite3')
    def __init__(self):self.credentials={};self.activities={}
    def load_credentials(self,scope,id):return deepcopy(self.credentials.get((scope,id)))
    def save_credentials(self,scope,id,data):self.credentials[scope,id]=deepcopy(data)
    def delete_credentials(self,login_id=None,scope=None):
        self.credentials={k:v for k,v in self.credentials.items() if not ((login_id is None or k[1]==login_id) and (scope is None or k[0]==scope))}
    def load_activity_data(self,key):return deepcopy(self.activities.get(key))
    def save_activity_data(self,key,value):self.activities[key]=deepcopy(value)


class Beans:
    def __init__(self):self.updating=set();self.busy=False;self.tick=Mock();self.login_maintenance=None
    def begin_login_update(self,id):
        if self.busy or id in self.updating:return False
        self.updating.add(id);return True
    def end_login_update(self,id):self.updating.discard(id)


class Qr:
    def __init__(self,service):
        self.service=service;self.state='waiting';self.closed=False;self.client=requests.Session();self.polled=0
        self.client.cookies.set('cookie2','new-qr-session',domain='.taobao.com')
    def start(self):return 'data:image/png;base64,c3ludGhldGljLXRlc3Q='
    def poll(self):self.polled+=1;return self.state
    def close(self):self.closed=True;self.client.close()


class PartnerLoginTests(unittest.TestCase):
    def setUp(self):
        self.clock=1000;self.storage=MemoryStorage();self.cache=CredentialCache(self.storage,now=lambda:self.clock);self.beans=Beans()
        self.snapshots={'a':{'phone':'13800138000','cookie':'parent-a'},'b':{'phone':'13900139000','cookie':'parent-b'}}
        self.accounts=lambda:[{'id':id,'status':'active'} for id in self.snapshots]
        self.seed('a',TAOBAO_SCOPE,'old-a',{'uid':'uid-a'})
        self.seed('b',TAOBAO_SCOPE,'old-b',{'uid':'uid-b'})
        self.seed('a',BAIDU_SCOPE,'old-map',{'uid':'map-a','phone_info':{'cuid':'owned-device','sv':'22.0.0'}})
        self.manager=PartnerLogins(self.accounts,lambda id:self.snapshots.get(id),self.cache,self.beans,qr_factory=Qr,now=lambda:self.clock)
        self.addCleanup(lambda:[self.manager.cancel(f['owner'],id) for id,f in list(self.manager.flows.items())])
    def snapshot(self,id='a'):return {**self.snapshots[id],'id':id}
    def seed(self,id,scope,token,data):
        with requests.Session() as client:
            client.cookies.set('cookie2' if scope==TAOBAO_SCOPE else 'BDUSS',token,domain='.taobao.com' if scope==TAOBAO_SCOPE else '.baidu.com')
            self.cache.connect(scope,self.snapshot(id),client,lambda:data,expiry_cookies=('cookie2','sgcookie') if scope==TAOBAO_SCOPE else ('BDUSS','BDUSS_BFESS'))

    def test_invalid_import_preserves_current_valid_session(self):
        before=self.cache.load(TAOBAO_SCOPE,self.snapshot())
        with patch.object(self.manager,'validate',side_effect=CheckinError('新资料失效','TAOBAO_LOGIN_REQUIRED',401)):
            with self.assertRaises(CheckinError):self.manager.import_session('a','taobao','cookie2=invalid-new')
        self.assertEqual(self.cache.load(TAOBAO_SCOPE,self.snapshot()),before)
        self.assertFalse(self.beans.updating)

    def test_verified_import_only_replaces_the_requested_account_and_realm(self):
        other=self.cache.load(TAOBAO_SCOPE,self.snapshot('b'));map_saved=self.cache.load(BAIDU_SCOPE,self.snapshot())
        with patch.object(self.manager,'validate',return_value={'uid':'uid-a'}):
            reply=self.manager.import_session('a','taobao','cookie2=new-a')
        self.assertEqual(self.cache.load(TAOBAO_SCOPE,self.snapshot())['cookies'][0]['value'],'new-a')
        self.assertEqual(self.cache.load(TAOBAO_SCOPE,self.snapshot('b')),other)
        self.assertEqual(self.cache.load(BAIDU_SCOPE,self.snapshot()),map_saved)
        self.assertNotIn('new-a',json.dumps(reply));self.assertNotIn('parent-a',json.dumps(reply))
        self.beans.tick.assert_called_once_with(restored={'a'})

    def test_wrong_official_account_never_overwrites_existing_session(self):
        before=self.cache.load(TAOBAO_SCOPE,self.snapshot())
        with patch('features.checkin.partner_logins.CheckinTaobao._call',return_value={'userNumId':'different-account'}):
            with self.assertRaises(CheckinError) as error:self.manager.import_session('a','taobao','cookie2=another-user')
        self.assertEqual(error.exception.code,'ACCOUNT_MISMATCH');self.assertEqual(self.cache.load(TAOBAO_SCOPE,self.snapshot()),before)

    def test_legacy_import_checks_original_identity_before_accepting_a_new_account(self):
        self.cache.entries[TAOBAO_SCOPE,'a']['data'].pop('uid')
        self.manager.profiles['a:taobao'].pop('uid')
        before=self.cache.peek(TAOBAO_SCOPE,self.snapshot())
        with patch('features.checkin.partner_logins.CheckinTaobao._call',side_effect=[{'userNumId':'uid-a'},{'userNumId':'different-user'}]):
            with self.assertRaises(CheckinError) as raised:self.manager.import_session('a','taobao','cookie2=wrong-account')
        self.assertEqual(raised.exception.code,'ACCOUNT_MISMATCH')
        after=self.cache.peek(TAOBAO_SCOPE,self.snapshot())
        self.assertEqual(after['cookies'],before['cookies']);self.assertEqual(after['generation'],before['generation'])
        self.assertEqual(after['data']['uid'],'uid-a')

    def test_regular_check_preserves_auth_generation_and_initialization_time(self):
        before=self.cache.load(TAOBAO_SCOPE,self.snapshot());self.clock+=100
        with patch.object(self.manager,'validate',return_value={'uid':'uid-a'}):self.manager.check('a','taobao')
        after=self.cache.load(TAOBAO_SCOPE,self.snapshot())
        self.assertEqual(before['generation'],after['generation']);self.assertEqual(before['created_at'],after['created_at'])

    def test_network_error_does_not_discard_or_report_login_expiry(self):
        before=self.cache.load(TAOBAO_SCOPE,self.snapshot())
        with patch.object(self.manager,'validate',side_effect=CheckinError('网络暂不可用','UPSTREAM_NETWORK')):
            with self.assertRaises(CheckinError):self.manager.check('a','taobao')
        self.assertEqual(self.cache.load(TAOBAO_SCOPE,self.snapshot()),before)
        self.assertEqual(self.manager.status('a')['services'][1]['state'],'saved')

    def test_kuaishou_validated_import_and_check_keep_secrets_in_bound_credentials_only(self):
        from features.checkin.kuaishou import SCOPE
        from tests.test_kuaishou import login
        data=login();other=self.cache.peek(TAOBAO_SCOPE,self.snapshot('b'))
        with patch('features.checkin.partner_logins.CheckinKuaishou._request',return_value={'result':1}):
            reply=self.manager.import_session('a','kuaishou',json.dumps(data))
            before=self.cache.peek(SCOPE,self.snapshot())
            self.manager.check('a','kuaishou')
        after=self.cache.peek(SCOPE,self.snapshot())
        self.assertEqual(after['generation'],before['generation'])
        self.assertEqual(after['data']['api_service_token'],'synthetic-token')
        self.assertIsNone(after['expires_at'])
        self.assertNotIn('synthetic-token',json.dumps(reply))
        self.assertNotIn('synthetic-client-salt',json.dumps(self.manager.profiles))
        self.assertEqual(self.cache.peek(TAOBAO_SCOPE,self.snapshot('b')),other)
        self.assertFalse(next(x for x in reply['services'] if x['service']=='kuaishou')['qr_available'])

    def test_kuaishou_rejected_replacement_keeps_old_login_and_account_binding(self):
        from features.checkin.kuaishou import SCOPE
        from tests.test_kuaishou import login
        with patch('features.checkin.partner_logins.CheckinKuaishou._request',return_value={'result':1}):
            self.manager.import_session('a','kuaishou',json.dumps(login()))
        original=self.cache.peek(SCOPE,self.snapshot())
        replacement={**login(),'api_service_token':'rejected-new-token'}
        with patch('features.checkin.partner_logins.CheckinKuaishou._request',side_effect=CheckinError('拒绝','KUAISHOU_QUERY_REJECTED',409)):
            with self.assertRaises(CheckinError):self.manager.import_session('a','kuaishou',json.dumps(replacement))
        self.assertEqual(self.cache.peek(SCOPE,self.snapshot()),original)
        with self.assertRaises(CheckinError):
            self.manager.import_session('a','kuaishou',json.dumps({**login(),'uid':'other-owner'}))

    def test_incomplete_identity_response_is_not_mistaken_for_expired_authentication(self):
        before=self.cache.peek(TAOBAO_SCOPE,self.snapshot())
        with patch('features.checkin.partner_logins.CheckinTaobao._call',return_value={}):
            with self.assertRaises(CheckinError) as error:self.manager.check('a','taobao')
        self.assertEqual(error.exception.code,'PARTNER_PROFILE_REJECTED')
        self.assertEqual(self.cache.peek(TAOBAO_SCOPE,self.snapshot()),before)

    def test_explicit_expiry_removes_only_rejected_session_and_retains_identity_metadata(self):
        with patch.object(self.manager,'validate',side_effect=CheckinError('确已失效','TAOBAO_LOGIN_REQUIRED',401)):
            with self.assertRaises(CheckinError):self.manager.check('a','taobao')
        self.assertIsNone(self.cache.load(TAOBAO_SCOPE,self.snapshot()))
        self.assertIsNotNone(self.cache.load(BAIDU_SCOPE,self.snapshot()))
        self.assertEqual(self.manager.profiles['a:taobao']['uid'],'uid-a')

    def test_busy_or_unknown_submission_blocks_authentication_before_start(self):
        self.beans.busy=True;factory=Mock();self.manager.qr_factory=factory
        with self.assertRaises(CheckinError) as error:self.manager.start_qr('owner','a','taobao')
        self.assertEqual(error.exception.code,'ACCOUNT_BUSY');factory.assert_not_called()

    def test_qr_confirmation_must_be_validated_before_install_and_resume(self):
        started=self.manager.start_qr('owner','a','taobao');flow=self.manager.flow('owner',started['flow_id'])
        self.assertIn('a',self.beans.updating);flow['qr'].state='confirmed'
        with patch.object(self.manager,'validate',return_value={'uid':'uid-a'}) as validate:
            result=self.manager.poll('owner',started['flow_id'])
        self.assertEqual(result['state'],'complete');validate.assert_called_once()
        self.assertTrue(flow['qr'].closed);self.assertFalse(self.beans.updating)
        self.beans.tick.assert_called_once_with(restored={'a'})

    def test_expired_local_dialog_does_not_claim_a_successful_update(self):
        start=self.manager.start_qr('owner','a','taobao');self.clock+=901
        before=self.cache.load(TAOBAO_SCOPE,self.snapshot())
        result=self.manager.poll('owner',start['flow_id'])
        self.assertEqual(result['state'],'closed');self.assertFalse(self.beans.updating)
        self.beans.tick.assert_not_called();self.assertEqual(self.cache.load(TAOBAO_SCOPE,self.snapshot()),before)

    def test_foreign_browser_session_cannot_poll_another_login(self):
        start=self.manager.start_qr('owner-a','a','taobao')
        with self.assertRaises(CheckinError) as error:self.manager.poll('owner-b',start['flow_id'])
        self.assertEqual(error.exception.status,404)

    def test_duplicate_poll_does_not_duplicate_official_requests(self):
        start=self.manager.start_qr('owner','a','taobao');flow=self.manager.flow('owner',start['flow_id']);flow['lock'].acquire()
        try:self.assertEqual(self.manager.poll('owner',start['flow_id'])['state'],'waiting')
        finally:flow['lock'].release()
        self.assertEqual(flow['qr'].polled,0)

    def test_cancelled_inflight_login_keeps_jobs_paused_until_request_finishes(self):
        start=self.manager.start_qr('owner','a','taobao');flow=self.manager.flow('owner',start['flow_id'])
        before=self.cache.load(TAOBAO_SCOPE,self.snapshot())
        def cancel_during_poll():
            self.manager.cancel('owner',start['flow_id']);self.assertIn('a',self.beans.updating);return 'confirmed'
        flow['qr'].poll=cancel_during_poll
        with patch.object(self.manager,'validate',return_value={'uid':'uid-a'}):result=self.manager.poll('owner',start['flow_id'])
        self.assertEqual(result['state'],'expired');self.assertEqual(self.cache.load(TAOBAO_SCOPE,self.snapshot()),before)
        self.assertFalse(self.beans.updating);self.beans.tick.assert_not_called()

    def test_baidu_device_identity_survives_credential_expiry_for_qr_renewal(self):
        old=self.cache.peek(BAIDU_SCOPE,self.snapshot());self.cache.discard(BAIDU_SCOPE,self.snapshot(),expected_generation=old['generation'])
        self.assertTrue(self.manager.status('a')['services'][0]['qr_available'])

    def test_removed_session_does_not_keep_an_old_success_message(self):
        self.manager._record('a','taobao',{'message':'已核对官方身份并保存登录态。','error_code':''})
        before=self.cache.peek(TAOBAO_SCOPE,self.snapshot())
        self.cache.discard(TAOBAO_SCOPE,self.snapshot(),expected_generation=before['generation'])
        row=self.manager.status('a')['services'][1]
        self.assertEqual(row['state'],'needs_update');self.assertIn('没有可用登录态',row['message'])
        self.assertEqual(self.manager.context('a','baidu-map')[2]['phone_info']['cuid'],'owned-device')


class ImportFormatTests(unittest.TestCase):
    def test_cookie_header_json_and_netscape_formats(self):
        for text in ['Cookie: cookie2=token; sgcookie=long',
                     '[{"name":"cookie2","value":"token","domain":".taobao.com","expirationDate":2000}]',
                     '# Netscape\n.taobao.com\tTRUE\t/\tTRUE\t2000\tcookie2\ttoken']:
            with self.subTest(text=text):self.assertEqual(parse_import(text,'taobao')[0][0]['name'],'cookie2')

    def test_foreign_domains_header_injection_and_invalid_expiry_are_rejected(self):
        for text in ['cookie2=token\r\nX-Header=secret',
                     '[{"name":"cookie2","value":"x","domain":"taobao.com.evil.example"}]',
                     '[{"name":"cookie2","value":"x","expires":"inf"}]']:
            with self.subTest(text=text):
                with self.assertRaises(CheckinError):parse_import(text,'taobao')


class PartnerApiTests(unittest.TestCase):
    def test_missing_account_and_malformed_payload_are_rejected(self):
        app=Flask(__name__);app.secret_key='test-only';storage=MemoryStorage();beans=Beans()
        register_partner_logins(app,lambda:[],lambda _:None,CredentialCache(storage),beans)
        client=app.test_client()
        self.assertEqual(client.get('/api/checkin/partners').status_code,400)
        self.assertEqual(client.post('/api/checkin/partners/import',json=[]).status_code,400)
        self.assertEqual(client.post('/api/checkin/partners/check',json={'login_id':'a','service':[]}).status_code,400)
        self.assertEqual(client.post('/api/checkin/partners/qr-poll',json={'flow_id':'other'}).status_code,404)


if __name__=='__main__':unittest.main()
