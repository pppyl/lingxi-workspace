"""Credential reuse at protocol boundaries, with no live official requests."""
import base64
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from http.cookiejar import Cookie
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet
from curl_cffi import requests

from credential_cache import CredentialCache, token_expiry
from login_storage import DatabaseLoginStorage
from checkin_client import CheckinClient, CheckinError
from monthly_coupon_auth import guizhou_session
from monthly_coupon_client import MonthlyCouponClient, MonthlyCouponError
from coupon_client import CouponClient, CouponError, _cbc_encrypt
from recharge.cmcc import ActivityClient
from activities.seven_day.client import SevenDayClient, SevenDayError
from tests import test_checkin_client as checkin
from tests import test_coupon_client as wallet
from tests import test_monthly_coupon_auth as monthly


SNAPSHOT = {'id':'account-a','phone':'13800000000','cookie':'JSESSIONID=primary; UID=unit-uid',
            'profile':{},'credential_revision':1}


def http_client():
    return SimpleNamespace(headers={},cookies=requests.Cookies(),close=Mock())


class CredentialCacheTests(unittest.TestCase):
    def setUp(self):
        self.clock = [1000]
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)/'accounts.sqlite3'
        codec = Fernet(Fernet.generate_key())
        self.storage = DatabaseLoginStorage(self.path,protect=codec.encrypt,unprotect=codec.decrypt)
        self.cache = CredentialCache(self.storage,now=lambda:self.clock[0])
        self.logins = 0

    def connect(self, scope='scope-a', snapshot=None, cache=None, *, expires=None, token='opaque-secret-cookie'):
        client = http_client()
        def initialize():
            self.logins += 1
            client.cookies.jar.set_cookie(Cookie(0,'sessionToken',token,None,False,'official.test',True,False,
                '/',True,True,expires,expires is None,None,None,{'HttpOnly':None}))
            client.headers['Authorization'] = 'Bearer opaque-secret-header'
            return {'user':'private-account-context'}
        data = (cache or self.cache).connect(scope,snapshot or SNAPSHOT,client,initialize,headers=('Authorization',))
        self.assertEqual(data,{'user':'private-account-context'})
        return client

    def test_restart_and_primary_rotation_reuse_encrypted_credentials(self):
        first = self.connect()
        self.clock[0] += 86400  # No invented two-hour TTL for an opaque session.
        restarted = CredentialCache(self.storage,now=lambda:self.clock[0])
        second = self.connect(snapshot={**SNAPSHOT,'cookie':'JSESSIONID=rotated','credential_revision':8},cache=restarted)
        self.assertEqual(self.logins,1)
        self.assertEqual(list(first.cookies.jar)[0].value,list(second.cookies.jar)[0].value)
        self.assertEqual(second.headers['Authorization'],'Bearer opaque-secret-header')
        for path in Path(self.directory.name).glob('accounts.sqlite3*'):
            for secret in (b'opaque-secret-cookie',b'opaque-secret-header',b'private-account-context',SNAPSHOT['phone'].encode()):
                self.assertNotIn(secret,path.read_bytes())

    def test_accounts_realms_and_phone_mismatch_never_share_credentials(self):
        self.connect()
        self.connect(scope='scope-b')
        self.connect(snapshot={**SNAPSHOT,'id':'account-b','phone':'13900000001'})
        self.connect(snapshot={**SNAPSHOT,'phone':'13900000002'})
        self.assertEqual(self.logins,4)

    def test_cookie_scope_and_security_attributes_survive_restore(self):
        client = http_client()
        client.cookies.jar.set_cookie(Cookie(0,'auth','secret',None,False,'official.test',False,False,
            '/activity',False,True,None,True,None,None,{'HttpOnly':None,'SameSite':'Lax'}))
        self.cache.connect('scoped-cookie',SNAPSHOT,client,lambda:{})
        restored = http_client()
        initialize = Mock()
        self.cache.connect('scoped-cookie',SNAPSHOT,restored,initialize)
        initialize.assert_not_called()
        before,after = list(client.cookies.jar)[0],list(restored.cookies.jar)[0]
        for field in ('name','value','domain','domain_specified','domain_initial_dot','path','path_specified','secure','expires','_rest'):
            self.assertEqual(getattr(before,field),getattr(after,field))

    def test_official_cookie_expiry_causes_one_new_login(self):
        self.connect(expires=1100)
        self.clock[0] = 1099
        self.connect(expires=1200)
        self.assertEqual(self.logins,1)
        self.clock[0] = 1100
        self.connect(expires=1200)
        self.assertEqual(self.logins,2)

    def test_jwt_expiry_is_honored_and_opaque_values_have_no_assumed_expiry(self):
        token = 'header.'+base64.urlsafe_b64encode(b'{"exp":1100}').decode().rstrip('=')+'.signature'
        self.connect(token=token)
        self.assertEqual(token_expiry(token),1100)
        self.clock[0] = 1101
        self.connect()
        self.assertEqual(self.logins,2)
        for opaque in ('opaque','bad.bad.bad','a.W10.c','a.eyJleHAiOnRydWV9.c'):
            self.assertIsNone(token_expiry(opaque))

    def test_parallel_bootstrap_is_coalesced_on_success_and_failure(self):
        for failing in (False,True):
            with self.subTest(failing=failing):
                cache = CredentialCache()
                barrier = threading.Barrier(6)
                lock = threading.RLock()
                def key_lock(*args):
                    barrier.wait(timeout=5)
                    return lock
                cache._key_lock = key_lock
                attempts = Mock()
                error = RuntimeError('official authentication rejected')
                def run():
                    client = http_client()
                    def initialize():
                        attempts()
                        if failing:raise error
                        client.cookies.set('session','shared-login',domain='official.test')
                        return {'user':'a'}
                    try:
                        return cache.connect('same',SNAPSHOT,client,initialize)
                    except RuntimeError as caught:
                        return caught
                with ThreadPoolExecutor(max_workers=6) as executor:
                    results = list(executor.map(lambda _:run(),range(6)))
                attempts.assert_called_once()
                self.assertTrue(all(value is error if failing else value == {'user':'a'} for value in results))

    def test_stale_failure_cannot_delete_newer_auth_and_recovery_is_bounded(self):
        old = self.connect()
        new = self.connect()
        self.assertTrue(self.cache.recover(new))
        self.assertEqual(self.logins,2)
        self.cache.invalidate(old)
        self.connect()
        self.assertEqual(self.logins,2)
        self.assertFalse(self.cache.recover(new))
        self.assertEqual(self.logins,2)

    def test_logout_and_permanent_module_deletion_clear_only_target_credentials(self):
        self.connect();self.connect(scope='scope-b')
        other = {**SNAPSHOT,'id':'account-b','phone':'13900000001'}
        self.connect(snapshot=other)
        self.cache.forget(SNAPSHOT['id'])
        self.assertIsNone(self.cache.load('scope-a',SNAPSHOT))
        self.assertIsNone(self.storage.load_credentials('scope-b',SNAPSHOT['id']))
        self.assertIsNotNone(self.cache.load('scope-a',other))
        self.cache.forget(scope='scope-a')
        self.assertIsNone(CredentialCache(self.storage).load('scope-a',other))


class CheckinReuseTests(unittest.TestCase):
    def setUp(self):
        self.cache = CredentialCache()
        self.snapshot = deepcopy(SNAPSHOT)
        self.sessions = []
        def factory():
            session = checkin.ProtocolSession()
            session.cookies = requests.Cookies()
            self.sessions.append(session)
            return session
        self.client = CheckinClient(lambda:deepcopy(self.snapshot),session_factory=factory,credential_cache=self.cache)

    def test_query_and_signin_share_auth_even_after_primary_cookie_rotation(self):
        self.client.inspect()
        self.snapshot.update(cookie='JSESSIONID=rotated',credential_revision=2)
        self.client.inspect()
        self.client.signin()
        logins = [call for session in self.sessions for call in session.calls if '/qwhdsso/appTokenLogin' in call[1]]
        self.assertEqual(len(logins),1)

    def test_expired_read_recovers_once_and_failed_mutation_is_not_replayed(self):
        with self.client._session() as (session,snapshot):
            original = session.post
            denied = [True]
            def post(url,**kwargs):
                if url.endswith('info/commonInfo') and denied:
                    denied.pop()
                    return checkin.Response({'code':'GOTO_LOGIN'})
                return original(url,**kwargs)
            session.post = post
            self.client._post(session,'info/commonInfo')
            session.post = Mock(return_value=checkin.Response({'code':'GOTO_LOGIN'}))
            with self.assertRaises(CheckinError):
                self.client._post(session,'mark31/domark',snapshot=snapshot)
            self.assertEqual(session.post.call_count,1)
            self.assertIsNone(self.cache.load('qwhdmark:1021122301',self.snapshot))

    def test_http_401_recovers_a_read_and_stops_after_one_attempt(self):
        with self.client._session() as (http,_):
            original = http.post
            def post(url,**kwargs):
                return checkin.Response(status=401) if url.endswith('info/commonInfo') else original(url,**kwargs)
            http.post = post
            with self.assertRaises(CheckinError) as caught:self.client._post(http,'info/commonInfo')
            self.assertEqual(caught.exception.code,'ACTIVITY_LOGIN_REQUIRED')
            self.assertEqual(sum('/qwhdsso/appTokenLogin' in c[1] for c in http.calls),2)


class WalletReuseTests(unittest.TestCase):
    def setUp(self):
        self.cache,self.snapshot,self.sessions = CredentialCache(),deepcopy(SNAPSHOT),[]
        self.flow = Mock()
        self.flow.call.return_value = (200,{'retCode':'000000','rspBody':{'token':'one-use-app-ticket'}},{})
        def factory():
            session = wallet.Session()
            session.cookies = requests.Cookies()
            session.msg_id = base64.b64encode(_cbc_encrypt('flow-'+str(len(self.sessions))).encode()).decode()
            self.sessions.append(session)
            return session
        self.client = CouponClient(lambda:deepcopy(self.snapshot),session_factory=factory,
                                  app_factory=lambda _:self.flow,credential_cache=self.cache)

    def test_reuses_wallet_auth_but_keeps_each_flow_msgid_and_otp_session_independent(self):
        with self.client._session() as (http,identity,current):
            self.snapshot.update(cookie='JSESSIONID=renewed',credential_revision=3)
            self.client.query(wallet_session=(http,identity,current))
            first_msgid = identity['msg_id']
            with self.client._session() as (_,other,_):
                self.assertNotEqual(first_msgid,other['msg_id'])
            current()  # A background primary update must not evict a pending OTP.
        self.assertEqual(self.flow.call.call_count,1)
        self.assertEqual(sum('/auth/loginfo' in url for s in self.sessions for url,_ in s.calls),2)

    def test_business_rejection_does_not_relogin_or_replay_submission(self):
        self.client.query()
        with self.assertRaises(CouponError):
            with self.client._session() as (http,_,_):
                http.post = Mock(return_value=wallet.Response({'retCode':'500002'}))
                # This is an actual business rejection, not login expiry.
                from coupon_client import _unpack
                _unpack(self.client._json(http.post('https://touch.10086.cn/test',json={})))
        self.client.query()
        self.assertEqual(self.flow.call.call_count,1)
        http.post.assert_called_once()

    def test_expired_wallet_loginfo_recovers_before_starting_an_operation(self):
        self.client.query()
        factory = self.client.session_factory
        def expired_factory():
            http = factory()
            original = http.get
            denied = [True]
            def get(url,**kwargs):
                if url.endswith('/auth/loginfo') and denied:
                    denied.pop()
                    return wallet.Response(status=401)
                return original(url,**kwargs)
            http.get = get
            return http
        self.client.session_factory = expired_factory
        self.client.query()
        self.assertEqual(self.flow.call.call_count,2)

    def test_query_expiry_recovers_immediately_with_latest_app_cookie_and_new_msgid(self):
        self.client.query()
        factory = self.client.session_factory
        old_msgid = []
        app_inputs = []
        self.client.app_factory = lambda snapshot:app_inputs.append(snapshot['cookie']) or self.flow
        fresh_msgid = base64.b64encode(_cbc_encrypt('renewed-flow-msgid').encode()).decode()
        def expired_factory():
            http = factory()
            old_msgid.append(http.msg_id)
            original = http.post
            denied = [True]
            def post(url,**kwargs):
                response = original(url,**kwargs)
                if denied:
                    denied.pop()
                    self.snapshot.update(cookie='JSESSIONID=latest-primary',credential_revision=7)
                    http.msg_id = fresh_msgid
                    return wallet.Response({'retCode':'500003'})
                return response
            http.post = post
            return http
        self.client.session_factory = expired_factory
        with patch('coupon_client._payload',wraps=wallet._payload) as payload:
            result = self.client.query()
        self.assertEqual(len(result['items']),1)
        self.assertEqual([c.args[1] for c in payload.call_args_list],[old_msgid[0],fresh_msgid])
        self.assertEqual(app_inputs,['JSESSIONID=latest-primary'])
        self.assertEqual(self.flow.call.call_count,2)
        self.assertEqual(len(self.sessions[-1].posts),2)

    def test_auth_recovery_budget_is_shared_by_loginfo_and_query(self):
        self.client.query()
        factory = self.client.session_factory
        def expired_factory():
            http = factory()
            original = http.get
            denied = [True]
            def get(url,**kwargs):
                if url.endswith('/auth/loginfo') and denied:
                    denied.pop()
                    return wallet.Response(status=401)
                return original(url,**kwargs)
            http.get = get
            http.post = Mock(return_value=wallet.Response({'retCode':'500003'}))
            return http
        self.client.session_factory = expired_factory
        with self.assertRaises(CouponError) as caught:self.client.query()
        self.assertEqual(caught.exception.code,'COUPON_LOGIN_REQUIRED')
        self.assertEqual(self.flow.call.call_count,2)
        self.sessions[-1].post.assert_called_once()

    def test_expiry_partway_through_history_restarts_read_without_duplicate_rows(self):
        factory = self.client.session_factory
        def expired_factory():
            http = factory()
            original = http.post
            def post(url,**kwargs):
                response = original(url,**kwargs)
                return wallet.Response({'retCode':'500003'}) if len(http.posts) == 3 else response
            http.post = post
            return http
        self.client.session_factory = expired_factory
        result = self.client.query(state='history')
        self.assertEqual((len(result['items']),result['total']),(4,4))
        self.assertEqual(len(self.sessions[-1].posts),7)
        self.assertEqual(self.flow.call.call_count,2)

    def test_changed_account_identity_is_rejected_without_reauthentication(self):
        with self.assertRaises(CouponError) as caught:
            with self.client._session() as (_,_,current):
                self.snapshot['id'] = 'new-account-identity'
                current()
        self.assertEqual(caught.exception.code,'APP_CREDENTIALS_CHANGED')
        self.assertEqual(self.flow.call.call_count,1)

    def test_parallel_query_expiry_renews_once_for_the_same_account(self):
        self.client.query()
        factory = self.client.session_factory
        barrier = threading.Barrier(6)
        def expired_factory():
            http = factory()
            original = http.post
            denied = [True]
            def post(url,**kwargs):
                response = original(url,**kwargs)
                if denied:
                    denied.pop();barrier.wait(timeout=5)
                    return wallet.Response({'retCode':'500003'})
                return response
            http.post = post
            return http
        def run(_):
            client = CouponClient(lambda:deepcopy(self.snapshot),session_factory=expired_factory,
                                  app_factory=lambda _:self.flow,credential_cache=self.cache)
            return client.query()
        with ThreadPoolExecutor(max_workers=6) as executor:
            results = list(executor.map(run,range(6)))
        self.assertTrue(all(len(value['items']) == 1 for value in results))
        self.assertEqual(self.flow.call.call_count,2)

    def test_rate_limit_does_not_reauthenticate_or_repeat_the_query(self):
        factory = self.client.session_factory
        def limited_factory():
            http = factory()
            http.post = Mock(return_value=wallet.Response({'retCode':'-800'}))
            return http
        self.client.session_factory = limited_factory
        with self.assertRaises(CouponError) as caught:self.client.query()
        self.assertEqual(caught.exception.code,'UPSTREAM_REJECTED')
        self.sessions[0].post.assert_called_once()
        self.flow.call.assert_called_once()
        self.assertIsNotNone(self.cache.load('coupon-wallet:12014',self.snapshot))


class MonthlyReuseTests(unittest.TestCase):
    def setUp(self):
        self.cache,self.snapshot,self.sessions = CredentialCache(),deepcopy(SNAPSHOT),[]
        self.flow = Mock()
        self.flow.call.return_value = (200,{'retCode':'000000','rspBody':{'token':'one-use-ticket'}},{})
        def factory():
            session = monthly.Session()
            session.cookies = requests.Cookies()
            self.sessions.append(session)
            return session
        self.factory = factory
        self.patch = patch('monthly_coupon_auth.AppLoginSession.from_credentials',return_value=self.flow)
        self.patch.start();self.addCleanup(self.patch.stop)

    def session(self):
        return guizhou_session(lambda:deepcopy(self.snapshot),monthly.ENTRY,
                               session_factory=self.factory,credential_cache=self.cache)

    def test_provincial_bearer_and_cookie_are_reused_after_primary_update(self):
        with self.session() as first:
            bearer = first.headers['AuthorizationAweb']
        self.snapshot.update(cookie='JSESSIONID=renewed',credential_revision=2)
        with self.session() as second:
            self.assertEqual(second.headers['AuthorizationAweb'],bearer)
            self.assertEqual(second.cookies.get('ActivityWeb-Token'),'activity-token')
        self.assertEqual(self.flow.call.call_count,1)
        self.assertEqual([len(s.calls) for s in self.sessions],[1,0])

    def test_read_recovers_once_but_claim_never_replays(self):
        client = MonthlyCouponClient(lambda:deepcopy(self.snapshot),entry_url=monthly.ENTRY,credential_cache=self.cache)
        with self.session() as http:
            http.get = Mock(side_effect=[monthly.Response({'code':401}),monthly.Response({'code':200,'data':{}})])
            client._get(http,'initInfo')
            self.assertEqual(http.get.call_count,2)
            self.assertEqual(self.flow.call.call_count,2)
            http.get = Mock(return_value=monthly.Response({'code':401}))
            with self.assertRaises(MonthlyCouponError):
                client._get(http,'receiveCoupon',action='direct',period='202611',snapshot=self.snapshot)
            self.assertEqual(http.get.call_count,1)
            self.assertEqual(self.flow.call.call_count,2)


class SevenDayReuseTests(unittest.TestCase):
    def setUp(self):
        self.cache,self.snapshot,self.sessions = CredentialCache(),deepcopy(SNAPSHOT),[]
        self.flow = Mock()
        self.flow.call.return_value = (200,{'retCode':'000000'},{})
        def factory():
            http = http_client()
            http.post = Mock(side_effect=lambda url,**kw:monthly.Response({'code':'0','data':
                {'userId':'unit-user','provinceCode':'100'} if url.endswith('user/loginCheck') else {}}))
            self.sessions.append(http)
            return http
        self.factory = factory
        self.patch = patch('activities.seven_day.client.AppLoginSession.from_credentials',return_value=self.flow)
        self.patch.start();self.addCleanup(self.patch.stop)

    def connect(self):
        client = SevenDayClient(lambda:deepcopy(self.snapshot),session_factory=self.factory,credential_cache=self.cache)
        client.connect();self.addCleanup(client.close)
        return client

    def test_multiple_operations_reuse_coc_login_and_preserve_user_context(self):
        first = self.connect()
        self.snapshot.update(cookie='JSESSIONID=rotated; UID=unit-uid',credential_revision=2)
        second = self.connect()
        self.assertEqual(first.user,second.user)
        self.assertEqual(second.user['userId'],'unit-user')
        self.assertEqual(self.flow.call.call_count,1)
        self.assertEqual(self.flow.call.call_args.args[1]['uId'],'unit-uid')
        self.assertEqual([h.post.call_count for h in self.sessions],[2,0])

    def test_explicit_auth_rejection_recovers_read_but_never_replays_write(self):
        client = self.connect()
        original = client.client.post
        denied = [True]
        def post(url,**kwargs):
            if url.endswith('activities/prize/drawDayNum') and denied:
                denied.pop()
                return monthly.Response({'code':'40'})
            return original(url,**kwargs)
        client.client.post = post
        client.post('activities/prize/drawDayNum',{})
        self.assertEqual(self.flow.call.call_count,2)
        client.client.post = Mock(return_value=monthly.Response({'code':'40'}))
        with self.assertRaises(SevenDayError):client.post('activities/prize/templatePrize',{},mutation=True)
        client.client.post.assert_called_once()
        self.assertEqual(self.flow.call.call_count,2)


class WednesdayReuseTests(unittest.TestCase):
    def test_persisted_activity_auth_survives_cookie_rotation_until_explicit_rejection(self):
        snapshot,cache = deepcopy(SNAPSHOT),CredentialCache()
        sessions = []
        def factory(**kwargs):
            http = http_client();sessions.append(http);return http
        calls = []
        def login(client,cookie,phone,profile,*,candidate,raw):
            calls.append(cookie)
            candidate.cookies.set('sessionToken','wed-session',domain='wx.10086.cn')
            return {'phone':phone,'provinceCode':'100'}
        with patch('recharge.cmcc.requests.Session',side_effect=factory),patch.object(ActivityClient,'_connect_app',login):
            client = ActivityClient(app_credentials_provider=lambda:deepcopy(snapshot),credential_cache=cache)
            self.addCleanup(client.http.close)
            client.connect_app(snapshot['cookie'],snapshot['phone'],{})
            checked = client.user_checked_at
            snapshot.update(cookie='JSESSIONID=new',credential_revision=2)
            client.connect_app(snapshot['cookie'],snapshot['phone'],{})
            self.assertLess(abs(client.user_checked_at-checked),.1)
            client.call = Mock(side_effect=[{'code':'GOTO_LOGIN'},{'code':'0','data':{}}])
            self.assertEqual(client.read('queryDrawPrizeInfo')['code'],'0')
            self.assertEqual(calls,['JSESSIONID=primary; UID=unit-uid','JSESSIONID=new'])
            self.assertEqual(client.call.call_count,2)


if __name__ == '__main__':unittest.main()
