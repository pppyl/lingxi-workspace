from contextlib import contextmanager
from datetime import datetime
import unittest
from unittest.mock import Mock, patch

from activities.native_group.phone_call import (CallSession, LAST_CALL_PATH, CALL_GRANT_PATH,
                                                sync_confirmed_call, sync_existing_authorization, automatic_call_context)
from activities.native_group.scheduler import GroupScheduler
from activities.native_group.tests.test_native_group import Storage, observation
from features.checkin.client import CheckinError
from recharge.cmcc import SHANGHAI
from workspace.accounts.login import AppLoginError, AppLoginSession, REFRESH_PATH


def call_observation(*, done=False, authorized=True):
    return observation({'groupSize': 3, 'groupMembers': [
        {'phone': '138****8000', 'authTaskStatus': '1' if authorized else None,
         'callTaskStatus': '1' if done else None}]})


class ManualPhoneCallTests(unittest.TestCase):
    def setUp(self):
        self.storage = Storage()
        self.client = Mock()
        @contextmanager
        def connect():
            yield Mock(), {'phone': '13800138000', 'cookie': 'JSESSIONID=synthetic'}
        self.client.connect = connect
        self.client.inspect.return_value = call_observation()
        self.client.rules.return_value = 'rules'
        self.accounts = lambda: [{'id': key, 'status': 'active'} for key in ('a', 'b')]
        self.factory = lambda key: self.client
        self.now = lambda: datetime(2026, 11, 28, tzinfo=SHANGHAI)
        self.manager = GroupScheduler(self.accounts, self.factory, self.storage, now=self.now)
        self.sender = patch('activities.native_group.scheduler.sync_confirmed_call').start()
        self.addCleanup(patch.stopall)
        self.sender.side_effect = lambda snapshot, before_submit: before_submit()

    def prepare(self, key='a'):
        self.manager.run(key, 'call_prepare', destination='13800138001')
        return self.manager.data['phone_calls'][key]['id']

    def test_prepare_only_binds_account_and_number_without_call_or_completion_report(self):
        token = self.prepare()
        self.assertEqual(self.prepare(), token)
        self.assertNotEqual(self.prepare('b'), token)
        self.sender.assert_not_called()
        self.client.operate.assert_not_called()
        self.assertFalse(self.manager.data['pending'])
        self.assertIsNone(self.manager.data['observations']['a']['campaign']['userGroupInfo']['groupMembers'][0]['callTaskStatus'])

    def test_phone_number_cannot_supply_a_uri_service_or_additional_dial_commands(self):
        for value in ('10086', 'tel:13800138001', '13800138001,123', '13800138001;123', '１２３４５６７８９０１', None):
            with self.subTest(value=value), self.assertRaises(CheckinError):
                self.manager.run('a', 'call_prepare', destination=value)
        self.client.inspect.assert_not_called()
        self.sender.assert_not_called()

    def test_authorization_must_be_officially_confirmed_before_dial_preparation(self):
        self.client.inspect.return_value = call_observation(authorized=False)
        with self.assertRaises(CheckinError) as raised:
            self.prepare()
        self.assertEqual(raised.exception.code, 'CALL_AUTH_REQUIRED')
        self.assertFalse(self.manager.data['phone_calls'])
        self.sender.assert_not_called()

    def test_new_account_prepares_dialing_after_syncing_existing_real_authorization(self):
        context = {'role_holder': 'com.greenpoint.android.mc10086.activity',
                   'device_id': 'verified-device', 'xk': 'verified-device-key'}
        self.manager.data['call_context'] = context
        self.client.inspect.side_effect = [call_observation(authorized=False), call_observation()]
        def sync(snapshot, device_context, *, before_submit):
            self.assertEqual(device_context, context)
            before_submit()
            self.assertEqual(self.storage.data['pending']['b']['kind'], 'call_authorize')
        with patch('activities.native_group.scheduler.sync_existing_authorization', side_effect=sync) as authorize:
            token = self.prepare('b')
            self.assertTrue(token)
            self.assertFalse(self.manager.data['pending'])
            self.client.inspect.side_effect = None
            self.client.inspect.return_value = call_observation()
            self.assertEqual(self.prepare('b'), token)
            self.manager.run('b')
            self.manager.run('b', 'call_authorize')
            restarted = GroupScheduler(self.accounts, self.factory, self.storage, now=self.now)
            self.assertTrue(restarted.status()['call_authorization_available'])
            self.assertEqual(restarted.run('b', 'call_prepare', destination='13800138001')
                             ['accounts'][1]['phone_call']['id'], token)
            authorize.assert_called_once()
        self.sender.assert_not_called()

    def test_refresh_and_open_queries_do_not_report_authorization(self):
        self.manager.data['call_context'] = {'device_id': 'verified-device'}
        self.client.inspect.return_value = call_observation(authorized=False)
        with patch('activities.native_group.scheduler.sync_existing_authorization') as authorize:
            self.manager.status()
            self.manager.run('a')
            authorize.assert_not_called()
        self.assertTrue(self.manager.status()['call_authorization_available'])
        self.assertNotIn('call_context', self.manager.status())

    def test_completed_call_does_not_skip_missing_android_authorization(self):
        self.manager.data['call_context'] = {'device_id': 'verified-device'}
        self.client.inspect.side_effect = [call_observation(done=True, authorized=False), call_observation(done=True)]
        with patch('activities.native_group.scheduler.sync_existing_authorization',
                   side_effect=lambda snapshot, context, before_submit: before_submit()) as authorize:
            result = self.manager.run('a', 'call_prepare', destination='13800138001')
        authorize.assert_called_once()
        self.sender.assert_not_called()
        self.client.operate.assert_not_called()
        self.assertFalse(self.manager.data['phone_calls'])
        self.assertEqual(result['accounts'][0]['auth_sync']['state'], 'confirmed')

    def test_refresh_repairs_completed_calls_authorization_once_using_existing_context(self):
        self.manager.data['call_context'] = {'device_id': 'verified-device'}
        self.client.inspect.return_value = call_observation(done=True, authorized=False)
        def authorize(snapshot, context, *, before_submit):
            before_submit()
            self.client.inspect.return_value = call_observation(done=True)
        with patch('activities.native_group.scheduler.sync_existing_authorization', side_effect=authorize) as sync:
            self.manager.run('a')
            self.manager.run('a')
            self.manager.run('a', 'call_authorize')
        sync.assert_called_once()
        self.sender.assert_not_called()

    def test_unknown_authorization_repair_keeps_completed_call_and_never_replays_after_restart(self):
        self.manager.data['call_context'] = {'device_id': 'verified-device'}
        self.client.inspect.return_value = call_observation(done=True, authorized=False)
        self.manager.data['phone_calls']['a'] = {'id': 'completed-call', 'stage': 'confirmed'}
        def authorize(snapshot, context, *, before_submit):
            before_submit()
            raise CheckinError('unknown', 'RESULT_UNCERTAIN', uncertain=True)
        with patch('activities.native_group.scheduler.sync_existing_authorization', side_effect=authorize) as sync:
            result = self.manager.run('a')
            self.assertEqual(result['accounts'][0]['auth_sync']['state'], 'pending')
            restarted = GroupScheduler(self.accounts, self.factory, self.storage, now=self.now)
            restarted.run('a')
            self.assertEqual(restarted.data['phone_calls']['a']['stage'], 'confirmed')
            self.client.inspect.return_value = call_observation(done=True)
            restarted.run('a')
        sync.assert_called_once()
        self.sender.assert_not_called()
        self.assertFalse(restarted.data['pending'])

    def test_failed_authorization_repair_does_not_reject_a_completed_call_and_requires_explicit_retry(self):
        self.manager.data['call_context'] = {'device_id': 'verified-device'}
        self.client.inspect.return_value = call_observation(done=True, authorized=False)
        self.manager.data['phone_calls']['a'] = {'id': 'completed-call', 'stage': 'confirmed'}
        def reject(snapshot, context, *, before_submit):
            before_submit()
            error = CheckinError('not accepted', 'CALL_AUTH_REJECTED', 409)
            error.definitive_rejection = True
            raise error
        with patch('activities.native_group.scheduler.sync_existing_authorization', side_effect=reject) as sync:
            self.manager.run('a')
            self.manager.run('a')
            self.assertEqual(self.manager.data['auth_sync']['a']['state'], 'failed')
            self.assertEqual(self.manager.data['phone_calls']['a']['stage'], 'confirmed')
            sync.assert_called_once()
            def accept(snapshot, context, *, before_submit):
                before_submit()
                self.client.inspect.return_value = call_observation(done=True)
            sync.side_effect = accept
            result = self.manager.run('a', 'call_authorize')
        self.assertEqual(result['accounts'][0]['auth_sync']['state'], 'confirmed')
        self.sender.assert_not_called()

    def test_ios_completed_call_needs_no_default_dialer_authorization_and_clears_obsolete_wait(self):
        state = call_observation(done=True, authorized=False)
        state['campaign']['userGroupInfo']['groupMembers'][0]['osType'] = 'ios'
        self.client.inspect.return_value = state
        self.manager.data['pending']['a'] = {'kind': 'call_authorize'}
        with patch('activities.native_group.scheduler.sync_existing_authorization') as authorize:
            result = self.manager.run('a')
            self.manager.run('a', 'call_prepare', destination='13800138001')
        self.assertEqual(result['accounts'][0]['auth_sync']['state'], 'not_required')
        self.assertFalse(self.manager.data['pending'])
        authorize.assert_not_called()
        self.sender.assert_not_called()

    def test_uncertain_authorization_survives_restart_without_replay_or_dial_draft(self):
        self.manager.data['call_context'] = {'device_id': 'verified-device'}
        self.client.inspect.return_value = call_observation(authorized=False)
        def sync(snapshot, context, *, before_submit):
            before_submit()
        with patch('activities.native_group.scheduler.sync_existing_authorization', side_effect=sync) as authorize:
            with self.assertRaises(CheckinError) as raised:
                self.prepare()
            self.assertTrue(raised.exception.uncertain)
            self.assertFalse(self.manager.data['phone_calls'])
            restarted = GroupScheduler(self.accounts, self.factory, self.storage, now=self.now)
            with self.assertRaises(CheckinError):
                restarted.run('a', 'call_prepare', destination='13800138001')
            with self.assertRaises(CheckinError):
                restarted.run('a', 'call_authorize')
            authorize.assert_called_once()
            self.client.inspect.return_value = call_observation()
            restarted.run('a')
            self.assertFalse(restarted.data['pending'])
            restarted.run('a', 'call_prepare', destination='13800138001')
            authorize.assert_called_once()
        self.sender.assert_not_called()

    def test_concurrent_new_account_preparations_share_one_authorization_sync(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event
        self.manager.data['call_context'] = {'device_id': 'verified-device'}
        self.client.inspect.return_value = call_observation(authorized=False)
        entered, release = Event(), Event()
        def sync(snapshot, context, *, before_submit):
            before_submit()
            entered.set()
            self.assertTrue(release.wait(2))
            self.client.inspect.return_value = call_observation()
        with patch('activities.native_group.scheduler.sync_existing_authorization', side_effect=sync) as authorize:
            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(self.prepare)
                self.assertTrue(entered.wait(1))
                second = executor.submit(self.prepare)
                release.set()
                self.assertEqual(first.result(), second.result())
            authorize.assert_called_once()

    def test_expired_app_identity_is_detected_before_asking_operator_to_call(self):
        @contextmanager
        def connect():
            yield Mock(), {'phone': '13800138000', 'primary_status': 'login_required'}
        self.client.connect = connect
        with self.assertRaises(CheckinError) as raised:
            self.prepare()
        self.assertEqual(raised.exception.code, 'APP_LOGIN_REQUIRED')
        self.assertFalse(self.manager.data['phone_calls'])
        self.sender.assert_not_called()

    def test_explicit_real_call_confirmation_and_account_bound_ticket_are_required(self):
        token = self.prepare()
        for key, ticket, connected in [('a', token, False), ('a', token, 'true'),
                                       ('a', 'wrong-ticket', True), ('b', token, True)]:
            with self.subTest(key=key, connected=connected), self.assertRaises(CheckinError):
                self.manager.run(key, 'call_confirm', call_id=ticket, connected=connected)
        self.sender.assert_not_called()

    def test_official_readback_alone_confirms_completion_and_prevents_resubmission(self):
        token = self.prepare()
        self.client.inspect.side_effect = [call_observation(), call_observation(done=True)]
        result = self.manager.run('a', 'call_confirm', call_id=token, connected=True)
        self.assertEqual(result['accounts'][0]['phone_call']['stage'], 'confirmed')
        self.assertFalse(self.manager.data['pending'])
        self.client.inspect.side_effect = None
        self.client.inspect.return_value = call_observation(done=True)
        self.manager.run('a', 'call_confirm', call_id=token, connected=True)
        self.manager.run('a', 'call_prepare', destination='13800138001')
        self.sender.assert_called_once()

    def test_accepted_report_is_not_completion_and_pending_survives_restart(self):
        token = self.prepare()
        with self.assertRaises(CheckinError) as raised:
            self.manager.run('a', 'call_confirm', call_id=token, connected=True)
        self.assertTrue(raised.exception.uncertain)
        self.assertEqual(self.storage.data['phone_calls']['a']['confirmation_source'], 'operator_real_call')
        restarted = GroupScheduler(self.accounts, self.factory, self.storage, now=self.now)
        with self.assertRaises(CheckinError):
            restarted.run('a', 'call_confirm', call_id=token, connected=True)
        with self.assertRaises(CheckinError):
            restarted.run('a', 'call_prepare', destination='13800138002')
        self.sender.assert_called_once()
        self.client.inspect.return_value = call_observation(done=True)
        restarted.run('a')
        self.assertFalse(restarted.data['pending'])
        self.assertEqual(restarted.data['phone_calls']['a']['stage'], 'confirmed')

    def test_unknown_submission_is_read_back_without_replaying_it(self):
        token = self.prepare()
        def uncertain(snapshot, before_submit):
            before_submit()
            raise CheckinError('unknown', 'RESULT_UNCERTAIN', uncertain=True)
        self.sender.side_effect = uncertain
        with self.assertRaises(CheckinError):
            self.manager.run('a', 'call_confirm', call_id=token, connected=True)
        self.manager.run('a')
        self.assertTrue(self.manager.data['pending']['a'])
        self.sender.assert_called_once()

    def test_previous_completed_call_cannot_be_reported_again_if_official_state_changes(self):
        token = self.prepare()
        self.manager.data['phone_calls']['a']['stage'] = 'confirmed'
        with self.assertRaises(CheckinError) as raised:
            self.manager.run('a', 'call_confirm', call_id=token, connected=True)
        self.assertEqual(raised.exception.code, 'CALL_ALREADY_SUBMITTED')
        self.sender.assert_not_called()

    def test_definitive_rejection_clears_pending_but_does_not_reuse_the_report(self):
        token = self.prepare()
        def rejected(snapshot, before_submit):
            before_submit()
            error = CheckinError('rejected', 'CALL_SYNC_REJECTED', 409)
            error.definitive_rejection = True
            raise error
        self.sender.side_effect = rejected
        with self.assertRaises(CheckinError):
            self.manager.run('a', 'call_confirm', call_id=token, connected=True)
        self.assertFalse(self.manager.data['pending'])
        self.assertEqual(self.manager.data['phone_calls']['a']['stage'], 'rejected')
        with self.assertRaises(CheckinError):
            self.manager.run('a', 'call_confirm', call_id=token, connected=True)
        self.sender.assert_called_once()

    def test_forget_clears_only_that_accounts_dial_draft(self):
        self.prepare('a')
        self.prepare('b')
        self.manager.forget('a')
        self.assertEqual(set(self.manager.data['phone_calls']), {'b'})


class CallSyncProtocolTests(unittest.TestCase):
    def test_automatic_context_reuses_existing_parameters_without_initializing_or_rotating_identity(self):
        from copy import deepcopy
        snapshot = {'phone': '13800138000', 'cookie': 'JSESSIONID=own',
                    'app_context': {'device_id': 'own-device', 'xk': 'own-key', 'refreshToken': 'own-refresh'}}
        before = deepcopy(snapshot)
        with patch.object(AppLoginSession, 'initialize') as initialize:
            context = automatic_call_context(snapshot)
        initialize.assert_not_called()
        self.assertEqual((context['device_id'], context['xk']), ('own-device', 'own-key'))
        self.assertEqual(snapshot, before)
        self.assertFalse({'cookie', 'phone', 'refreshToken'} & context.keys())

    def test_automatic_context_initializes_missing_parameters_once_without_overwriting_primary_credentials(self):
        snapshot = {'phone': '13800138000', 'cookie': 'JSESSIONID=own', 'app_context': {}}
        flow = AppLoginSession.from_credentials(snapshot)
        flow.initialize = Mock(side_effect=lambda: setattr(flow, 'xk', 'official-device-key'))
        with patch.object(AppLoginSession, 'from_credentials', return_value=flow):
            context = automatic_call_context(snapshot)
        flow.initialize.assert_called_once()
        self.assertEqual(context['xk'], 'official-device-key')
        self.assertEqual(snapshot['app_context'], {})
        self.assertEqual(snapshot['cookie'], 'JSESSIONID=own')

    def test_expired_identity_does_not_initialize_for_automatic_context(self):
        with patch.object(AppLoginSession, 'from_credentials') as create:
            with self.assertRaises(CheckinError):
                automatic_call_context({'primary_status': 'login_required'})
        create.assert_not_called()

    def test_authorization_reuses_real_device_but_keeps_current_accounts_identity(self):
        from copy import deepcopy
        snapshot = {'phone': '13800138000', 'cookie': 'JSESSIONID=current-account',
                    'app_context': {'device_id': 'account-device', 'xk': 'account-key', 'refreshToken': 'own-token'}}
        original = deepcopy(snapshot)
        device = {'role_holder': 'com.greenpoint.android.mc10086.activity',
                  'device_id': 'verified-device', 'xk': 'verified-key'}
        session = CallSession.from_credentials(snapshot)
        session.call = Mock(return_value=(200, {'retCode': '000000'}, {}))
        before = Mock()
        with patch.object(CallSession, 'from_credentials', return_value=session) as create:
            sync_existing_authorization(snapshot, device, before_submit=before)
        isolated = create.call_args.args[0]
        self.assertEqual(isolated['cookie'], snapshot['cookie'])
        self.assertEqual(isolated['phone'], snapshot['phone'])
        self.assertEqual(isolated['app_context'], {'device_id': 'verified-device', 'xk': 'verified-key', 'refreshToken': 'own-token'})
        self.assertEqual(snapshot, original)
        self.assertEqual(session.call.call_args_list[0].args, (REFRESH_PATH, {'cellNum': snapshot['phone']}))
        self.assertEqual(session.call.call_args_list[1].args,
                         (CALL_GRANT_PATH, {'cellNum': snapshot['phone'], 'isCallGrant': True}))
        before.assert_called_once()

    def test_authorization_requires_a_configured_real_role_and_accepted_session(self):
        snapshot = {'phone': '13800138000', 'cookie': 'JSESSIONID=current-account'}
        device = {'role_holder': 'com.greenpoint.android.mc10086.activity', 'device_id': 'real', 'xk': 'real-key'}
        before = Mock()
        with patch.object(CallSession, 'from_credentials') as create:
            for context in (None, {'device_id': 'invented'}, {**device, 'role_holder': 'other.package'}):
                with self.assertRaises(CheckinError):
                    sync_existing_authorization(snapshot, context, before_submit=before)
            create.assert_not_called()
            create.return_value.call.return_value = (200, {'retCode': '410000'}, {})
            with self.assertRaises(CheckinError):
                sync_existing_authorization(snapshot, device, before_submit=before)
            create.return_value.call.assert_called_once_with(REFRESH_PATH, {'cellNum': snapshot['phone']})
        before.assert_not_called()

    def test_authorization_transport_failure_after_submission_is_not_retried(self):
        snapshot = {'phone': '13800138000', 'cookie': 'JSESSIONID=current-account'}
        device = {'role_holder': 'com.greenpoint.android.mc10086.activity', 'device_id': 'real', 'xk': 'real-key'}
        session = Mock(cookie=snapshot['cookie'])
        session.call.side_effect = [(200, {'retCode': '000000'}, {}), AppLoginError('timeout')]
        before = Mock()
        with patch.object(CallSession, 'from_credentials', return_value=session):
            with self.assertRaises(CheckinError) as raised:
                sync_existing_authorization(snapshot, device, before_submit=before)
        self.assertTrue(raised.exception.uncertain)
        self.assertEqual(session.call.call_count, 2)
        before.assert_called_once()

    def test_sync_uses_logged_in_account_and_original_business_envelope(self):
        snapshot = {'phone': '13800138000', 'cookie': 'JSESSIONID=synthetic',
                    'profile': {'provinceCode': '851', 'cityCode': '851'}}
        session = CallSession.from_credentials(snapshot)
        envelope = session.envelope({'cellNum': snapshot['phone']})
        self.assertEqual((envelope['tel'], envelope['prov'], envelope['city']), ('13800138000', '851', '851'))
        session.call = Mock(return_value=(200, {'retCode': '000000'}, {}))
        before = Mock()
        with patch.object(CallSession, 'from_credentials', return_value=session):
            sync_confirmed_call(snapshot, before_submit=before)
        before.assert_called_once()
        session.call.assert_called_once_with(LAST_CALL_PATH, {'cellNum': '13800138000'})

    def test_network_error_is_uncertain_and_never_retried(self):
        session = Mock()
        session.call.side_effect = AppLoginError('timeout')
        with patch.object(CallSession, 'from_credentials', return_value=session):
            with self.assertRaises(CheckinError) as raised:
                sync_confirmed_call({'phone': '13800138000'}, before_submit=Mock())
        self.assertTrue(raised.exception.uncertain)
        session.call.assert_called_once()

    def test_expired_app_identity_does_not_send_a_report_or_force_login(self):
        before = Mock()
        with patch.object(CallSession, 'from_credentials') as session:
            with self.assertRaises(CheckinError):
                sync_confirmed_call({'primary_status': 'login_required'}, before_submit=before)
        before.assert_not_called()
        session.assert_not_called()
