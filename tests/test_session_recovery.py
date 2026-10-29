import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
from contextlib import closing
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet
from curl_cffi.requests.exceptions import ConnectionError

from app import create_app
from app_login import AppCredentialError
from lingxi_client import LingxiError
from login_storage import DatabaseLoginStorage, LoginStorageError, _dpapi
from unified_logins import UnifiedLogins
from session_maintenance import SessionMaintenance
from recharge.accounts import AccountManager
from recharge.cmcc import ActivityClient, ActivityError, SHANGHAI
from recharge.scheduler import ClaimScheduler


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'accounts.sqlite3'
        self.codec = Fernet(Fernet.generate_key())
        self.storage = DatabaseLoginStorage(self.path, protect=self.codec.encrypt, unprotect=self.codec.decrypt)
        self.pool = UnifiedLogins(self.storage)

    def remember(self, phone='19900000001'):
        return self.pool.remember(phone, 'JSESSIONID=private-cookie-marker', {'provinceCode':'100'}, 'private-token-marker', '主号')

    def test_database_stores_encrypted_credentials_and_restores_stable_account_ids(self):
        saved = self.remember()
        restored = UnifiedLogins(self.storage).get(saved.phone)
        self.assertEqual(restored.id, saved.id)
        self.assertEqual(restored.profile, saved.profile)
        self.assertEqual(restored.name, saved.name)
        self.assertEqual(restored.status, 'restoring')
        for path in Path(self.directory.name).glob('accounts.sqlite3*'):
            for secret in [b'private-cookie-marker', b'private-token-marker', b'19900000001']:
                self.assertNotIn(secret, path.read_bytes())
        with closing(sqlite3.connect(self.path)) as db, db:
            self.assertEqual(db.execute('SELECT phone_masked FROM account_sessions').fetchone()[0], '199****0001')
        self.assertNotIn('private-cookie', str(restored.public()))

    def test_forget_removes_only_one_saved_login(self):
        one, two = self.remember(), self.remember('19900000002')
        self.pool.forget(one.phone)
        restored = UnifiedLogins(self.storage)
        self.assertIsNone(restored.get(one.phone))
        self.assertEqual(restored.get(two.phone).id, two.id)

    def test_failed_encryption_preserves_previous_transaction(self):
        saved = self.remember()
        self.storage.protect = Mock(side_effect=LoginStorageError('加密失败'))
        self.pool.remember('19900000002', 'JSESSIONID=new', {}, 'new')
        self.assertTrue(self.pool.warning)
        self.assertEqual([r['id'] for r in self.storage.load()], [saved.id])

    def test_corrupt_credentials_are_reported_and_not_overwritten(self):
        saved = self.remember()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('UPDATE account_sessions SET payload=?', (b'corrupt',))
        # A codec failure is translated to a safe error at the storage boundary.
        self.storage.unprotect = Mock(side_effect=LoginStorageError('无法解密本机登录态'))
        restored = UnifiedLogins(self.storage)
        self.assertTrue(restored.warning)
        restored.remember('19900000003', 'JSESSIONID=new', {}, 'new')
        with closing(sqlite3.connect(self.path)) as db, db:
            self.assertEqual(db.execute('SELECT login_id,payload FROM account_sessions').fetchall(), [(saved.id,b'corrupt')])

    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI integration')
    def test_windows_dpapi_roundtrip_uses_current_user(self):
        value = b'unit-test-only-cookie'
        encrypted = _dpapi(value)
        self.assertNotIn(value, encrypted)
        self.assertEqual(_dpapi(encrypted, decrypt=True), value)
        with self.assertRaises(LoginStorageError):
            _dpapi(b'corrupt', decrypt=True)

    def test_legacy_account_directory_migrates_to_database_without_new_ids(self):
        old = Path(self.directory.name) / 'accounts.json'
        old.write_text(json.dumps({'accounts':[{'id':'default','name':'旧账号','pool_login_id':'a'*32,'phone_masked':'199****0001'}]}), encoding='utf-8')
        manager = AccountManager(storage=old, database=self.storage)
        self.assertEqual(manager.get().pool_login_id, 'a'*32)
        self.assertEqual(manager.get().snapshot()['phone'], '199****0001')
        self.assertFalse(manager.get().snapshot()['authenticated'])
        self.assertEqual(self.storage.load_accounts()[0]['id'], 'default')
        old.write_text('invalid legacy file',encoding='utf-8')
        restored = AccountManager(storage=old, database=self.storage)
        self.assertFalse(restored.load_error)
        self.assertEqual(restored.get().name,'旧账号')


@unittest.skipUnless(os.name == 'nt', 'Windows encrypted account database')
class RecoveryIntegrationTests(unittest.TestCase):
    def setUp(self):
        def refreshed(flow):
            flow.refreshed_at = __import__('time').time()
            return flow.cookie
        renewal = patch('app.AppLoginSession.refresh',autospec=True,side_effect=refreshed)
        self.renewal = renewal.start()
        self.addCleanup(renewal.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'accounts.sqlite3'
        self.jobs_path = Path(self.directory.name) / 'jobs.json'
        self.clients = []
        self.app, self.manager, self.scheduler = self.build()
        self.pool = self.app.extensions['unified_logins']
        self.saved = self.pool.remember('19900000001','JSESSIONID=private-cookie',{},'private-token','主号')
        self.http, self.headers = self.http_for(self.app)
        response = self.http.post('/recharge/api/login/attach', json={'login_id':self.saved.id,'account_id':'default'}, headers=self.headers)
        self.assertEqual(response.status_code,200)
        target = (datetime.now(SHANGHAI) + timedelta(days=7)).replace(hour=8,minute=0,second=0,microsecond=0)
        target += timedelta(days=(2-target.weekday())%7)
        self.scheduler.schedule(['default'],{'run_at':target.isoformat(),'weekly':True})

    def factory(self, profile_dir=None):
        client = ActivityClient(profile_dir)
        def connect(cookie,phone,profile):
            client.mode = 'app'
            client.app_material = {'cookie':cookie,'phone':phone,'profile':profile}
            return {'authenticated':True,'mode':'app','phone':phone[:3]+'****'+phone[-4:]}
        client.connect_app = Mock(side_effect=connect)
        client.identity = Mock(return_value={'authenticated':True,'mode':'app','phone':'199****0001'})
        client.claim = Mock(side_effect=AssertionError('recovery must never submit a claim'))
        client.app_login.send_sms = Mock(side_effect=AssertionError('recovery must never send SMS'))
        client.app_login.login = Mock(side_effect=AssertionError('recovery must never perform a new APP login'))
        self.clients.append(client)
        return client

    def build(self):
        manager = AccountManager(database=DatabaseLoginStorage(self.path), client_factory=self.factory)
        scheduler = ClaimScheduler(manager,storage=self.jobs_path)
        app = create_app(enable_recharge=True, database_path=self.path, recharge_manager=manager, recharge_scheduler=scheduler)
        app.testing = True
        return app,manager,scheduler

    def http_for(self, app):
        http = app.test_client(); http.get('/'); http.get('/recharge/')
        child = app.extensions['recharge_app']
        value = http.get_cookie('recharge_session',path='/recharge').value
        return http,{'X-CSRF-Token':child.session_interface.get_signing_serializer(child).loads(value)['csrf']}

    @patch('app.verify_token')
    def test_restart_restores_same_account_and_waiting_job_without_sms_or_claim(self, verify):
        before = self.scheduler.snapshots()
        app, manager, scheduler = self.build()
        self.assertEqual(app.extensions['unified_logins'].get(self.saved.phone).id,self.saved.id)
        self.assertEqual(manager.get().pool_login_id,self.saved.id)
        self.assertEqual(manager.get().cached['session_status'],'restoring')
        app.extensions['session_maintenance'].tick()
        self.assertTrue(manager.get().snapshot()['authenticated'])
        self.assertEqual(scheduler.snapshots(),before)
        self.assertEqual(len(manager.accounts),1)
        client = manager.get().client
        client.connect_app.assert_called_once_with(self.saved.cookie,self.saved.phone,{})
        client.app_login.send_sms.assert_not_called(); client.app_login.login.assert_not_called(); client.claim.assert_not_called()

    @patch('app.exchange_cookie',return_value='renewed-token')
    @patch('app.verify_token',side_effect=[LingxiError('expired',authentication=True),None])
    def test_expired_lingxi_token_is_exchanged_from_same_app_cookie_and_saved(self, verify, exchange):
        self.app.extensions['session_maintenance'].tick()
        exchange.assert_called_once_with(self.saved.cookie)
        self.assertEqual(self.pool.get(self.saved.phone).token,'renewed-token')
        self.assertEqual(UnifiedLogins(DatabaseLoginStorage(self.path)).get(self.saved.phone).token,'renewed-token')
        self.assertTrue(self.scheduler.snapshots()['default']['enabled'])

    @patch('app.verify_token',side_effect=ConnectionError('temporary network failure'))
    def test_transient_failure_keeps_database_cookie_and_schedule(self, verify):
        self.app.extensions['session_maintenance'].tick()
        self.assertEqual(self.pool.get(self.saved.phone).status,'retry')
        self.assertEqual(UnifiedLogins(DatabaseLoginStorage(self.path)).get(self.saved.phone).cookie,self.saved.cookie)
        self.assertTrue(self.scheduler.snapshots()['default']['enabled'])
        self.assertTrue(self.manager.get().cached['authenticated'])

    @patch('app.exchange_cookie',side_effect=LingxiError('rejected',authentication=True))
    @patch('app.verify_token',side_effect=LingxiError('expired',authentication=True))
    def test_confirmed_app_termination_requires_user_action_instead_of_repeated_login(self, verify, exchange):
        self.renewal.side_effect=AppCredentialError('官方要求重新验证账号',code='420000',login_required=True)
        maintenance = self.app.extensions['session_maintenance']
        maintenance.tick(); maintenance.due.clear(); maintenance.tick()
        self.assertEqual(self.pool.get(self.saved.phone).status,'login_required')
        exchange.assert_not_called()
        self.manager.get().client.app_login.send_sms.assert_not_called()

    @patch('app.verify_token')
    def test_logout_deletes_database_record_and_does_not_resurrect(self, verify):
        self.app.extensions['session_maintenance'].tick()
        response = self.http.post('/api/logins/logout',json={'id':self.saved.id})
        self.assertEqual(response.status_code,200)
        self.app.extensions['session_maintenance'].due.clear(); self.app.extensions['session_maintenance'].tick()
        self.assertFalse(DatabaseLoginStorage(self.path).load())
        self.assertFalse(self.scheduler.snapshots()['default']['enabled'])
        self.assertFalse(self.manager.get().pool_login_id)

    def test_explicit_reconnect_of_legacy_slot_preserves_waiting_schedule(self):
        self.manager.get().pool_login_id = ''
        self.manager.get().client.mode = 'none'
        self.manager.get().client.app_material = None
        before = self.scheduler.snapshots()
        response = self.http.post('/recharge/api/login/attach',json={'login_id':self.saved.id,'account_id':'default'},headers=self.headers)
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json['account_id'],'default')
        self.assertEqual(self.scheduler.snapshots(),before)
        self.assertEqual(len(self.manager.accounts),1)

    def test_logout_before_background_restore_cancels_bound_waiting_job(self):
        app, manager, scheduler = self.build()
        http, _ = self.http_for(app)
        response = http.post('/api/logins/logout',json={'id':self.saved.id})
        self.assertEqual(response.status_code,200)
        self.assertFalse(scheduler.snapshots()['default']['enabled'])
        self.assertFalse(manager.get().pool_login_id)
        manager.get().client.connect_app.assert_not_called()
        self.assertFalse(DatabaseLoginStorage(self.path).load())

    def test_account_directory_remains_readable_while_reconnecting(self):
        def identity():
            reader = threading.Thread(target=self.manager.snapshots)
            reader.start(); reader.join(timeout=1)
            self.assertFalse(reader.is_alive(), 'directory lock must not cover network operations')
            return {'authenticated':True,'mode':'app','phone':'199****0001'}
        self.manager.get().client.identity.side_effect = identity
        result = self.http.post('/recharge/api/login/attach',json={'login_id':self.saved.id},headers=self.headers)
        self.assertEqual(result.status_code,200)


class ActivityRenewalTests(unittest.TestCase):
    def setUp(self):
        self.client = ActivityClient()
        self.client.mode = 'app'
        self.client.app_material = {'cookie':'JSESSIONID=private','phone':'19900000001','profile':{}}
        self.client.connect_app = Mock(return_value={'authenticated':True})

    def test_auth_failed_read_reconnects_once_using_same_cookie(self):
        self.client.call = Mock(side_effect=[{'code':'GOTO_LOGIN'}, {'code':'0','data':{'phone':'19900000001'}}])
        self.assertEqual(self.client.require_login()['phone'],'19900000001')
        self.client.connect_app.assert_called_once_with(**self.client.app_material)
        self.assertEqual(self.client.call.call_count,2)

    def test_rejected_read_is_not_reconnected_in_a_loop(self):
        self.client.call = Mock(return_value={'code':'GOTO_LOGIN'})
        with self.assertRaises(ActivityError): self.client.require_login()
        self.client.connect_app.assert_called_once()
        self.assertEqual(self.client.call.call_count,2)

    def test_reconnect_does_not_clear_an_uncertain_claim(self):
        self.client.uncertain = True
        self.client.connect_app.side_effect = lambda **kwargs: setattr(self.client,'uncertain',False)
        self.client.renew_app_session()
        self.assertTrue(self.client.uncertain)
        self.assertEqual(self.client.total_submissions,0)

    def test_claim_endpoint_cannot_be_replayed_by_read_recovery(self):
        self.client.call = Mock()
        with self.assertRaises(ActivityError): self.client.read('drawPrize')
        self.client.call.assert_not_called(); self.client.connect_app.assert_not_called()


class MaintenanceTests(unittest.TestCase):
    def test_transient_recovery_has_backoff_and_does_not_block_other_accounts(self):
        pool = UnifiedLogins()
        a=pool.remember('19900000001','a',{},'a'); b=pool.remember('19900000002','b',{},'b')
        now = [0]
        check = Mock(side_effect=[False,True,True])
        maintenance = SessionMaintenance(pool,check,now=lambda:now[0])
        maintenance.tick(); maintenance.tick()
        self.assertEqual(check.call_count,2)
        now[0]=60; maintenance.tick()
        self.assertEqual(check.call_args_list[-1].args[0],a)
        self.assertEqual(check.call_count,3)


if __name__ == '__main__': unittest.main()
