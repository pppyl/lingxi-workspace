import time
import unittest
from unittest.mock import Mock, patch

from app_login import (AppLoginSession, AppLoginError, AppCredentialError,
                       REFRESH_PATH, AUTH_REFRESH_PATH, INIT_PATH, merge_app_cookie)
from app import create_app
from unified_logins import UnifiedLogins
from session_maintenance import SessionMaintenance
from lingxi_client import LingxiError
from recharge.accounts import AccountManager
from recharge.cmcc import ActivityClient, ActivityError
from recharge.scheduler import ClaimScheduler


class RefreshProtocolTests(unittest.TestCase):
    def flow(self, **kwargs):
        return AppLoginSession(cookie='JSESSIONID=old; UID=u; ticketID=t; SessionType=1',
                               phone='19900000001',xk='unit-xk',**kwargs)

    def test_keepalive_is_official_path_and_plain_cellnum_without_sms(self):
        flow=self.flow()
        flow.call=Mock(return_value=(200,{'retCode':'000000'},{}))
        self.assertEqual(flow.refresh(),flow.cookie)
        flow.call.assert_called_once_with(REFRESH_PATH,{'cellNum':flow.phone})
        self.assertGreater(flow.export_context()['refreshed_at'],0)

    def test_timeout_uses_refresh_token_once_and_persists_rotation(self):
        flow=self.flow(refresh_token='old-refresh')
        def response(path,body):
            if path==REFRESH_PATH:return 200,{'retCode':'410000'},{}
            self.assertEqual(path,AUTH_REFRESH_PATH)
            self.assertEqual(body['refreshToken'],'old-refresh')
            self.assertEqual(body['sendSmsFlag'],'0')
            self.assertEqual(body['loginType'],'80028')
            self.assertNotEqual(body['cellNum'],flow.phone)
            return 200,{'retCode':'000000','rspBody':{'cookie':'JSESSIONID=new',
                          'refreshToken':'new-refresh','validityTerm':123456,'provinceCode':'100'}},{}
        flow.call=Mock(side_effect=response)
        flow.refresh()
        self.assertEqual(flow.call.call_count,2)
        self.assertIn('JSESSIONID=new',flow.cookie)
        self.assertIn('ticketID=t',flow.cookie)
        context=flow.export_context()
        self.assertEqual(context['refreshToken'],'new-refresh')
        rebuilt=AppLoginSession.from_credentials({'cookie':flow.cookie,'phone':flow.phone,
                                  'profile':flow.user_info,'app_context':context})
        self.assertEqual(rebuilt.device_id,flow.device_id)
        self.assertEqual(rebuilt.refresh_token,'new-refresh')
        self.assertEqual(rebuilt.validity_term,123456)

    def test_missing_legacy_token_after_confirmed_timeout_does_not_send_sms(self):
        flow=self.flow();flow.call=Mock(return_value=(200,{'retCode':'410000'},{}))
        with self.assertRaises(AppCredentialError) as error:flow.refresh()
        self.assertTrue(error.exception.login_required)
        self.assertEqual(flow.call.call_count,1)

    def test_provider_termination_and_transient_errors_are_distinguished(self):
        for code,required in [('420000',True),('220230',True),('400007',True),('500000',False),('weird',False)]:
            with self.subTest(code=code):
                flow=self.flow();flow.call=Mock(return_value=(200,{'retCode':code,'retDesc':'private detail'},{}))
                with self.assertRaises(AppCredentialError) as error:flow.refresh()
                self.assertEqual(error.exception.login_required,required)
                self.assertNotIn('private detail',str(error.exception))

    def test_initialization_cannot_replace_authenticated_cookie(self):
        flow=self.flow();flow.xk='null';flow.app_authenticated=True
        original=flow.cookie
        def response(path,body):
            if path==INIT_PATH:
                flow.cookie='JSESSIONID=anonymous'
                return 200,{'retCode':'000000','rspBody':{'ssv':'new-xk'}},{}
            self.assertEqual(flow.cookie,original)
            return 200,{'retCode':'000000'},{}
        flow.call=Mock(side_effect=response);flow.refresh()
        self.assertEqual(flow.xk,'new-xk');self.assertEqual(flow.cookie,original)

    def test_partial_cookie_headers_preserve_uid_and_replace_session(self):
        result=merge_app_cookie('JSESSIONID=old; UID=u; ticketID=t; Path=/',
                                ['JSESSIONID=new; Path=/; HttpOnly','ticketID=new-ticket'])
        self.assertIn('JSESSIONID=new',result);self.assertNotIn('JSESSIONID=old',result)
        self.assertIn('UID=u',result);self.assertIn('ticketID=new-ticket',result)

    def test_sms_login_retains_renewal_material(self):
        flow=self.flow(sms_sent_at=time.monotonic())
        flow.call=Mock(return_value=(200,{'retCode':'000000','rspBody':{
            'refreshToken':'login-refresh','validityTerm':123,'provinceCode':'100'}},{}))
        flow.login(flow.phone,'123456')
        self.assertEqual(flow.export_context()['refreshToken'],'login-refresh')


class SharedRefreshTests(unittest.TestCase):
    def setUp(self):
        client=ActivityClient();self.addCleanup(client.http.close)
        self.client=client
        def connect(cookie,phone,profile):
            client.app_material={'cookie':cookie,'phone':phone,'profile':profile};client.mode='app'
            return {'authenticated':True,'mode':'app','phone':'199****0001'}
        client.connect_app=Mock(side_effect=connect)
        client.identity=Mock(return_value={'authenticated':True,'mode':'app'})
        self.manager=AccountManager(default_client=client)
        self.scheduler=ClaimScheduler(self.manager)
        self.app=create_app(enable_recharge=True,recharge_manager=self.manager,recharge_scheduler=self.scheduler)
        self.pool=self.app.extensions['unified_logins']
        self.saved=self.pool.remember('19900000001','JSESSIONID=old',{},'old-token','主号',
                                     app_context={'xk':'xk','device_id':'unit','refreshed_at':time.time()})
        self.manager.get().pool_login_id=self.saved.id
        self.http=self.app.test_client();self.http.get('/')

    def renewed(self,flow):
        flow.cookie='JSESSIONID=new';flow.refresh_token='new-refresh';flow.refreshed_at=time.time()
        return flow.cookie

    @patch('app.verify_token')
    @patch('app.exchange_cookie',return_value='new-token')
    def test_refresh_syncs_both_features_with_stable_login_and_conversation(self,exchange,verify):
        self.http.post('/api/login/select',json={'id':self.saved.id})
        logged=next(iter(self.app.extensions['lingxi_chat_states'].values()))
        conversation=logged.session_id
        with patch('app.AppLoginSession.refresh',autospec=True,side_effect=self.renewed):
            result=self.http.post('/api/logins/check',json={'id':self.saved.id})
        self.assertEqual(result.status_code,200)
        self.assertEqual(logged.session_id,conversation)
        self.assertEqual(logged.app_cookie,'JSESSIONID=new')
        self.assertEqual(logged.auth_code,'new-token')
        self.client.connect_app.assert_called_once_with('JSESSIONID=new',self.saved.phone,{})
        self.assertEqual(self.client.app_credentials_provider()['cookie'],'JSESSIONID=new')
        self.assertEqual(self.saved.app_context['refreshToken'],'new-refresh')
        self.assertNotIn(b'new-refresh',result.data)

    @patch('app.verify_token')
    def test_activity_failure_keeps_primary_login_usable_and_retries_in_background(self,verify):
        self.client.connect_app.side_effect=ActivityError('temporary','APP_SSO_FAILED',502,retryable=True)
        with patch('app.AppLoginSession.refresh',autospec=True,side_effect=lambda s:s.cookie):
            maintenance=self.app.extensions['session_maintenance'];maintenance.tick()
        self.assertEqual(self.saved.status,'active')
        self.assertTrue(self.saved.public()['logged_in'])
        self.assertIn(self.saved.id,maintenance.due)

    @patch('app.verify_token')
    def test_late_lingxi_exchange_cannot_overwrite_rotated_token(self,verify):
        self.saved.token=''
        def exchange(cookie):
            self.pool.rotate_credentials(self.saved.phone,expected_revision=self.saved.credential_revision,
                                          cookie='JSESSIONID=new',token='latest-token')
            return 'stale-token'
        with patch('app.exchange_cookie',side_effect=exchange):
            response=self.http.post('/api/login/select',json={'id':self.saved.id})
        self.assertEqual(response.status_code,200)
        self.assertEqual(self.saved.token,'latest-token')

    def test_revision_update_wakes_a_backed_off_account(self):
        check=Mock(return_value=False)
        now=[0];maintenance=SessionMaintenance(self.pool,check,now=lambda:now[0])
        maintenance.tick();maintenance.tick();self.assertEqual(check.call_count,1)
        self.pool.rotate_credentials(self.saved.phone,expected_revision=1,cookie='JSESSIONID=new')
        maintenance.tick();self.assertEqual(check.call_count,2)


if __name__=='__main__':unittest.main()
