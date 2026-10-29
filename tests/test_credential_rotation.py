from copy import deepcopy
import threading
import unittest
from unittest.mock import Mock, patch

from recharge.cmcc import ActivityClient, ActivityError
from unified_logins import UnifiedLogins


class MemoryStorage:
    def __init__(self, rows=None):
        self.rows = deepcopy(rows or [])
        self.saves = 0

    def load(self):
        return deepcopy(self.rows)

    def save(self, rows):
        self.rows = deepcopy(rows)
        self.saves += 1


class CredentialRotationTests(unittest.TestCase):
    def setUp(self):
        self.storage = MemoryStorage()
        self.pool = UnifiedLogins(self.storage)
        self.saved = self.pool.remember('19900000001', 'JSESSIONID=first-unit-cookie', {'provinceCode':'100'},
            'first-unit-token', '主号', app_context={'device_id':'unit-device','xk':'unit-xk'})

    def test_remember_updates_same_record_and_preserves_identity_and_alias(self):
        old = self.saved
        created = old.created_at
        revision = old.credential_revision
        renewed = self.pool.remember(old.phone, 'JSESSIONID=second-unit-cookie', {}, 'second-token',
                                     app_context={'device_id':'new-unit-device'})
        self.assertIs(old, renewed)
        self.assertEqual(old.id, renewed.id)
        self.assertEqual(renewed.created_at, created)
        self.assertEqual(renewed.name, '主号')
        self.assertEqual(renewed.credential_revision, revision + 1)
        self.assertEqual(old.cookie, 'JSESSIONID=second-unit-cookie')
        self.assertEqual(renewed.app_context, {'device_id':'new-unit-device'})

    def test_context_and_revision_survive_storage_roundtrip(self):
        self.pool.rotate_credentials(self.saved.phone, expected_revision=1, cookie='JSESSIONID=updated',
                                     app_context={'xk':'updated-unit-xk'})
        restored = UnifiedLogins(self.storage).get(self.saved.phone)
        self.assertEqual(restored.app_context, {'device_id':'unit-device','xk':'updated-unit-xk'})
        self.assertEqual(restored.credential_revision, 2)
        for secret in ('unit-device','updated-unit-xk','updated'):
            self.assertNotIn(secret, str(restored.public()))

    def test_legacy_rows_default_to_empty_context_and_first_revision(self):
        legacy = self.storage.load()
        del legacy[0]['app_context']; del legacy[0]['credential_revision']
        restored = UnifiedLogins(MemoryStorage(legacy)).get(self.saved.phone)
        self.assertEqual(restored.credential_revision, 1)
        self.assertEqual(restored.app_context, {})

    def test_cookie_rotation_is_cas_and_invalidates_only_old_business_token(self):
        result = self.pool.rotate_credentials(self.saved.phone, expected_revision=1,
            cookie='JSESSIONID=updated', app_context={'xk':'new-xk'})
        self.assertIs(result, self.saved)
        self.assertEqual(result.token, '')
        self.assertEqual(result.profile, {'provinceCode':'100'})
        self.assertEqual(result.app_context, {'device_id':'unit-device','xk':'new-xk'})
        self.assertEqual(result.status, 'restoring')
        self.assertEqual(result.credential_revision, 2)
        self.assertEqual(result.name, '主号')

    def test_same_cookie_context_rotation_preserves_business_token(self):
        old_token = self.saved.token
        self.pool.rotate_credentials(self.saved.phone, expected_revision=1,
            cookie=self.saved.cookie, app_context={'xk':'new-xk'})
        self.assertEqual(self.saved.token, old_token)
        self.assertEqual(self.saved.credential_revision, 2)

    def test_late_refresh_cannot_overwrite_newer_login_or_saved_database_value(self):
        input_revision = self.saved.credential_revision
        self.pool.remember(self.saved.phone,'JSESSIONID=new-login',{},'new-login-token')
        count = self.storage.saves
        stale = self.pool.rotate_credentials(self.saved.phone, expected_revision=input_revision,
                                             cookie='JSESSIONID=late-old-response')
        self.assertIsNone(stale)
        self.assertEqual(self.saved.cookie,'JSESSIONID=new-login')
        self.assertEqual(self.storage.saves,count)
        self.assertEqual(self.storage.rows[0]['cookie'],'JSESSIONID=new-login')

    def test_late_failure_mark_cannot_invalidate_newer_revision(self):
        input_revision = self.saved.credential_revision
        self.pool.remember(self.saved.phone,'JSESSIONID=new-login',{},'new-token')
        self.assertFalse(self.pool.mark(self.saved,'login_required','old failure',expected_revision=input_revision))
        self.assertEqual(self.saved.status,'active')
        self.assertTrue(self.pool.mark(self.saved,'retry','current failure',expected_revision=self.saved.credential_revision))

    def test_logout_and_new_login_cannot_reuse_old_refresh_revision(self):
        old_revision=self.saved.credential_revision
        old_id=self.saved.id
        self.pool.forget(self.saved.phone)
        current=self.pool.remember(self.saved.phone,'JSESSIONID=new-login',{},'new-token')
        self.assertNotEqual(current.id,old_id)
        self.assertGreater(current.credential_revision,old_revision)
        self.assertIsNone(self.pool.rotate_credentials(current.phone,expected_revision=old_revision,
                                                       cookie='JSESSIONID=late-old-refresh'))

    def test_snapshot_does_not_expose_mutable_context_references(self):
        self.pool.rotate_credentials(self.saved.phone,expected_revision=1,cookie=self.saved.cookie,
                                     app_context={'nested':{'key':'original'}})
        snapshot=self.pool.credentials(self.saved.id)
        snapshot['app_context']['nested']['key']='external-change'
        snapshot['profile']['provinceCode']='other'
        self.assertEqual(self.saved.app_context['nested']['key'],'original')
        self.assertEqual(self.saved.profile['provinceCode'],'100')

    def test_concurrent_refresh_results_have_one_cas_winner(self):
        barrier=threading.Barrier(3)
        results=[]
        def rotate(cookie):
            barrier.wait(timeout=2)
            results.append(self.pool.rotate_credentials(self.saved.phone,expected_revision=1,cookie=cookie))
        workers=[threading.Thread(target=rotate,args=('JSESSIONID='+str(i),)) for i in range(2)]
        for worker in workers: worker.start()
        barrier.wait(timeout=2)
        for worker in workers: worker.join(timeout=2)
        self.assertEqual(sum(result is not None for result in results),1)
        self.assertEqual(self.saved.credential_revision,2)


class ActivityProviderTests(unittest.TestCase):
    def setUp(self):
        self.pool=UnifiedLogins()
        self.saved=self.pool.remember('19900000001','JSESSIONID=old',{},'old-token')
        self.provider=Mock(side_effect=lambda:self.pool.credentials(self.saved.id))
        self.client=ActivityClient(app_credentials_provider=self.provider)
        self.addCleanup(self.client.http.close)
        self.client.app_material={'cookie':'JSESSIONID=old','phone':self.saved.phone,'profile':{}}
        self.client.connect_app=Mock(return_value={'authenticated':True})
        self.client.app_login.login=Mock()
        self.client.app_login.send_sms=Mock()

    def test_reconnect_uses_latest_cookie_despite_stale_activity_copy(self):
        self.pool.rotate_credentials(self.saved.phone,expected_revision=1,cookie='JSESSIONID=new',
                                     app_context={'device_id':'unit-device'})
        self.client.renew_app_session()
        self.client.connect_app.assert_called_once_with(cookie='JSESSIONID=new',phone=self.saved.phone,profile={})
        self.assertEqual(self.client.app_credential_revision,2)
        self.client.app_login.login.assert_not_called()
        self.client.app_login.send_sms.assert_not_called()

    def test_rotated_cookie_is_used_before_read_and_reconnect_stays_bounded(self):
        self.pool.rotate_credentials(self.saved.phone,expected_revision=1,cookie='JSESSIONID=new')
        self.client.call=Mock(return_value={'code':'GOTO_LOGIN'})
        with self.assertRaises(ActivityError):self.client.read('queryDrawPrizeInfo')
        self.client.connect_app.assert_called_once_with(cookie='JSESSIONID=new',phone=self.saved.phone,profile={})
        self.client.call.assert_called_once()

    def test_missing_central_record_never_falls_back_to_old_copy(self):
        self.pool.forget(self.saved.phone)
        self.client.uncertain=True
        with self.assertRaises(ActivityError) as failure: self.client.renew_app_session()
        self.assertEqual(failure.exception.code,'APP_LOGIN_REQUIRED')
        self.client.connect_app.assert_not_called()
        self.assertTrue(self.client.uncertain)

    def test_provider_failure_is_retryable_and_hides_internal_details(self):
        self.provider.side_effect=RuntimeError('private internal detail')
        with self.assertRaises(ActivityError) as failure: self.client.renew_app_session()
        self.assertEqual(failure.exception.code,'APP_CREDENTIALS_UNAVAILABLE')
        self.assertTrue(failure.exception.retryable)
        self.assertNotIn('private internal detail',str(failure.exception))

    def test_read_can_reconnect_via_provider_without_old_app_material(self):
        self.client.mode='none'; self.client.app_material=None
        self.client.call=Mock(side_effect=[ActivityError('login needed','LOGIN_REQUIRED',401),
            {'code':'0','data':{'phone':self.saved.phone}}])
        self.assertEqual(self.client.require_login()['phone'],self.saved.phone)
        self.client.connect_app.assert_called_once_with(cookie='JSESSIONID=old',phone=self.saved.phone,profile={})

    def test_successful_reconnect_preserves_uncertain_claim(self):
        self.client.uncertain=True
        self.client.connect_app.side_effect=lambda **kwargs:setattr(self.client,'uncertain',False)
        self.client.renew_app_session()
        self.assertTrue(self.client.uncertain)

    def test_standalone_without_provider_keeps_existing_fallback(self):
        self.client.app_credentials_provider=None
        self.client.renew_app_session()
        self.client.connect_app.assert_called_once_with(**self.client.app_material)

    def test_explicit_logout_removes_provider(self):
        self.client.logout()
        self.assertIsNone(self.client.app_credentials_provider)
        self.assertIsNone(self.client.app_material)


class BusinessSsoErrorsTests(unittest.TestCase):
    def setUp(self):
        self.client=ActivityClient()
        self.addCleanup(self.client.http.close)
        self.http=Mock()
        self.http.get.return_value=Mock(status_code=200,url='https://wx.10086.cn/entry',
                                        text="var loginPath = '/appTokenLogin?sid=unit';")
        self.http.post.return_value.status_code=200

    def assert_business_failure(self, payload, expected_code):
        self.http.post.return_value.json.return_value=payload
        with patch('recharge.cmcc.requests.Session',return_value=self.http):
            with self.assertRaises(ActivityError) as failure:
                self.client.connect_app('JSESSIONID=unit-cookie','19900000001')
        self.assertEqual(failure.exception.code,expected_code)
        self.assertEqual(failure.exception.status,502)
        self.assertTrue(failure.exception.retryable)
        self.assertNotIn('短信',str(failure.exception))
        self.assertEqual(self.client.mode,'none')
        self.http.close.assert_called_once()

    def test_maintenance_code_does_not_invalidate_main_app_credentials(self):
        self.assert_business_failure({'code':'SERVICE_MAINTENANCE','msg':'private detail'},'APP_SSO_FAILED')

    def test_non_object_and_missing_destination_are_retryable_format_errors(self):
        self.assert_business_failure([],'APP_SSO_FORMAT')

    def test_success_with_invalid_data_object_is_retryable_format_error(self):
        self.assert_business_failure({'code':'SUCCESS','data':'invalid'},'APP_SSO_FORMAT')

    def test_a_cookie_rotated_during_sso_cannot_commit_old_activity_session(self):
        pool=UnifiedLogins()
        saved=pool.remember('19900000001','JSESSIONID=unit-cookie',{},'token')
        self.client.app_credentials_provider=lambda:pool.credentials(saved.id)
        old_http=self.client.http
        def rotate_during_auth(*args,**kwargs):
            pool.rotate_credentials(saved.phone,expected_revision=1,cookie='JSESSIONID=new-unit-cookie')
            return Mock(status_code=200,json=lambda:{'code':'SUCCESS','data':{'url':'/redirect'}})
        self.http.post.side_effect=rotate_during_auth
        page=self.http.get.return_value
        self.http.get.side_effect=[page,Mock(status_code=200),Mock(status_code=200,json=lambda:{'code':'0','data':{}})]
        with patch('recharge.cmcc.requests.Session',return_value=self.http):
            with self.assertRaises(ActivityError) as failure:
                self.client.connect_app('JSESSIONID=unit-cookie',saved.phone,{})
        self.assertEqual(failure.exception.code,'APP_CREDENTIALS_CHANGED')
        self.assertTrue(failure.exception.retryable)
        self.assertIs(self.client.http,old_http)
        self.assertIsNone(self.client.app_material)
        self.http.close.assert_called_once()

    def test_rotation_during_browser_cleanup_cannot_commit_old_session(self):
        pool=UnifiedLogins()
        saved=pool.remember('19900000001','JSESSIONID=unit-cookie',{},'token')
        self.client.app_credentials_provider=lambda:pool.credentials(saved.id)
        old_http=self.client.http
        self.http.post.return_value.json.return_value={'code':'SUCCESS','data':{'url':'/redirect'}}
        page=self.http.get.return_value
        self.http.get.side_effect=[page,Mock(status_code=200),Mock(status_code=200,json=lambda:{'code':'0','data':{}})]
        self.client.bridge.process=Mock()
        self.client.bridge.process.poll.return_value=None
        self.client.bridge.rpc=Mock(side_effect=lambda *args:pool.rotate_credentials(saved.phone,
            expected_revision=1,cookie='JSESSIONID=new-unit-cookie'))
        with patch('recharge.cmcc.requests.Session',return_value=self.http):
            with self.assertRaises(ActivityError) as failure:
                self.client.connect_app('JSESSIONID=unit-cookie',saved.phone,{})
        self.assertEqual(failure.exception.code,'APP_CREDENTIALS_CHANGED')
        self.assertIs(self.client.http,old_http)
        self.assertIsNone(self.client.app_material)
        self.http.close.assert_called_once()
        self.client.bridge.process=None


if __name__=='__main__': unittest.main()
