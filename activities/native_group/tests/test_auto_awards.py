from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta
import unittest
from unittest.mock import Mock, patch

from activities.native_group.client import NativeGroupClient, award_ready
from activities.native_group.scheduler import GroupScheduler
from activities.native_group.tests.test_native_group import Storage, observation
from features.checkin.client import CheckinError
from recharge.cmcc import SHANGHAI


def ready_observation(key='a', *, count=3, size=3, complete=True, prize='1'):
    state = observation({'groupSize': size, 'groupTaskStatus': 1 if complete else 0,
        'groupMembers': [{'phone': 'synthetic-' + member, 'authTaskStatus': '1',
                          'callTaskStatus': '1' if complete else None} for member in ('a', 'b', 'c')[:count]]})
    state['identity']['phone'] = 'synthetic-' + key
    state['campaign']['groupPrizeBtnStatus'] = prize
    return state


class AutomaticAwardTests(unittest.TestCase):
    def setUp(self):
        self.storage = Storage()
        self.now = datetime(2026, 11, 28, 12, tzinfo=SHANGHAI)
        self.accounts = [{'id': 'a', 'status': 'active'}]
        self.states = {key: ready_observation(key) for key in ('a', 'b', 'c')}
        self.clients = {}
        for key in self.states:
            client = Mock()
            @contextmanager
            def connect(key=key):
                yield 'session-' + key, {'phone': 'account-' + key}
            client.connect = connect
            client.rules.return_value = 'rules'
            client.inspect.side_effect = lambda session, key=key: deepcopy(self.states[key])
            def operate(session, snapshot, kind, state, *, before_submit, key=key, **options):
                self.assertEqual(kind, 'claim')
                self.assertEqual(snapshot['phone'], 'account-' + key)
                before_submit()
                self.assertEqual(self.storage.data['pending'][key]['kind'], 'claim')
                self.states[key]['campaign']['groupPrizeBtnStatus'] = '2'
            client.operate.side_effect = operate
            self.clients[key] = client
        self.manager = self.restart()

    def restart(self):
        return GroupScheduler(lambda: self.accounts, lambda key: self.clients[key], self.storage, now=lambda: self.now)

    def test_refresh_claims_only_after_officially_ready_and_already_claimed_is_not_replayed(self):
        result = self.manager.run('a')
        self.assertEqual(result['accounts'][0]['auto_claim']['state'], 'confirmed')
        self.assertIsNone(result['accounts'][0]['pending'])
        self.manager.run('a')
        self.manager.run('a', 'claim')
        self.restart().run('a')
        self.clients['a'].operate.assert_called_once()

    def test_four_completed_members_in_a_ten_person_team_cannot_claim(self):
        state = ready_observation(count=3, size=10)
        state['campaign']['userGroupInfo']['groupMembers'].append(
            {'phone': 'synthetic-d', 'authTaskStatus': '1', 'callTaskStatus': '1'})
        self.states['a'] = state
        self.manager.run('a')
        self.clients['a'].operate.assert_not_called()

    def test_official_team_readiness_and_current_membership_are_required(self):
        cases = []
        for task in (0, '0', None):
            state = ready_observation()
            state['campaign']['userGroupInfo']['groupTaskStatus'] = task
            cases.append(state)
        state = ready_observation()
        state['identity']['phone'] = 'another-team-account'
        cases.append(state)
        for state in cases:
            with self.subTest(state=state):
                self.assertFalse(award_ready(state))
                client = NativeGroupClient(Mock(), credential_cache=Mock())
                client._call = Mock()
                with self.assertRaises(CheckinError):
                    client.operate(Mock(), {}, 'claim', state, before_submit=Mock())
                client._call.assert_not_called()

    def test_officially_ready_team_is_not_blocked_by_stale_member_details(self):
        for state in self.states.values():
            state['campaign']['userGroupInfo']['groupTaskStatus'] = 2
            for member in state['campaign']['userGroupInfo']['groupMembers']:
                member.update(authTaskStatus=None, callTaskStatus='1')
        with patch('activities.native_group.scheduler.sync_existing_authorization') as authorize:
            result = self.manager.run('a')
        self.assertEqual(result['accounts'][0]['auto_claim']['state'], 'confirmed')
        self.clients['a'].operate.assert_called_once()
        authorize.assert_not_called()
        members = result['accounts'][0]['observation']['campaign']['userGroupInfo']['groupMembers']
        self.assertTrue(all(member['authTaskStatus'] is None for member in members))

    def test_official_readiness_retires_old_authorization_wait_without_faking_member_authorization(self):
        state = self.states['a']
        for member in state['campaign']['userGroupInfo']['groupMembers']:
            member.update(osType='android', authTaskStatus=None)
        self.manager.data['pending']['a'] = {'kind': 'call_authorize'}
        with patch('activities.native_group.scheduler.sync_existing_authorization') as authorize:
            result = self.manager.run('a')
        authorize.assert_not_called()
        self.clients['a'].operate.assert_called_once()
        self.assertIsNone(result['accounts'][0]['pending'])
        self.assertEqual(result['accounts'][0]['auth_sync']['state'], 'details_pending')
        self.assertTrue(all(member['authTaskStatus'] is None for member in
            result['accounts'][0]['observation']['campaign']['userGroupInfo']['groupMembers']))

    def test_official_readiness_never_retires_an_uncertain_award_submission(self):
        state = self.states['a']
        for member in state['campaign']['userGroupInfo']['groupMembers']:
            member.update(osType='android', authTaskStatus=None)
        self.manager.data['pending']['a'] = {'kind': 'claim', 'automatic': True}
        result = self.manager.run('a')
        self.assertEqual(result['accounts'][0]['pending']['kind'], 'claim')
        self.clients['a'].operate.assert_not_called()

    def test_ios_team_with_no_default_dialer_authorization_can_claim_when_officially_ready(self):
        for state in self.states.values():
            state['campaign']['showGroup'] = False
            for member in state['campaign']['userGroupInfo']['groupMembers']:
                member.update(osType='ios', authTaskStatus=None)
        self.manager.data['pending']['a'] = {'kind': 'call_authorize'}
        with patch('activities.native_group.scheduler.sync_existing_authorization') as authorize:
            result = self.manager.run('a')
        self.assertEqual(result['accounts'][0]['auto_claim']['state'], 'confirmed')
        authorize.assert_not_called()

    def test_claim_endpoint_allows_officially_ready_ios_without_android_qualification_flag(self):
        state = ready_observation()
        state['campaign']['showGroup'] = False
        for member in state['campaign']['userGroupInfo']['groupMembers']:
            member.update(osType='ios', authTaskStatus=None)
        client = NativeGroupClient(Mock(), credential_cache=Mock())
        client._call = Mock(return_value={})
        before = Mock()
        client.operate('session', {'phone': 'synthetic-account'}, 'claim', state, before_submit=before)
        self.assertEqual(client._call.call_args.args[1], 'redeemGroupAward')
        for member in state['campaign']['userGroupInfo']['groupMembers']:
            member['osType'] = 'android'
        self.assertFalse(award_ready(state))

    def test_ios_background_keeps_checking_until_official_team_status_becomes_ready(self):
        state = self.states['a']
        state['campaign']['showGroup'] = False
        state['campaign']['userGroupInfo']['groupTaskStatus'] = 0
        for member in state['campaign']['userGroupInfo']['groupMembers']:
            member.update(osType='ios', authTaskStatus=None)
        self.manager._save_observation('a', state)
        self.manager.tick()
        self.clients['a'].operate.assert_not_called()
        self.now += timedelta(minutes=1)
        state['campaign']['userGroupInfo']['groupTaskStatus'] = 2
        self.manager.tick()
        self.clients['a'].operate.assert_called_once()

    def test_all_owned_android_members_missing_authorization_are_repaired_then_claimed_without_calls(self):
        self.accounts.extend({'id': key, 'status': 'active'} for key in ('b', 'c'))
        self.manager.data['call_context'] = {'device_id': 'verified-device', 'xk': 'verified-key'}
        for key, state in self.states.items():
            state['campaign']['userGroupInfo']['groupTaskStatus'] = 0
            for member in state['campaign']['userGroupInfo']['groupMembers']:
                member.update(osType='android', authTaskStatus=None)
            self.manager._save_observation(key, state)
        def authorize(snapshot, context, *, before_submit):
            before_submit()
            key = snapshot['phone'].removeprefix('account-')
            for state in self.states.values():
                group = state['campaign']['userGroupInfo']
                next(member for member in group['groupMembers'] if member['phone'] == 'synthetic-' + key)['authTaskStatus'] = '1'
                if all(member['authTaskStatus'] == '1' for member in group['groupMembers']):
                    group['groupTaskStatus'] = 2
        with patch('activities.native_group.scheduler.sync_existing_authorization', side_effect=authorize) as sync:
            with patch('activities.native_group.scheduler.sync_confirmed_call') as call:
                self.manager.tick()
                self.manager.tick()
            self.assertEqual(sync.call_count, 3)
            call.assert_not_called()
        for client in self.clients.values():
            client.operate.assert_called_once()
        self.assertTrue(all(self.manager.data['auto_claims'][key]['state'] == 'confirmed' for key in ('a', 'b', 'c')))

    def test_automatic_claim_uses_official_award_endpoint_and_does_not_fabricate_prizes(self):
        snapshot = {'phone': 'synthetic-account'}
        client = NativeGroupClient(lambda: snapshot, credential_cache=Mock())
        client._call = Mock(return_value={})
        before = Mock()
        client.operate('session', snapshot, 'claim', ready_observation(), before_submit=before)
        client._call.assert_called_once_with('session', 'redeemGroupAward', {}, snapshot=snapshot, before_submit=before)

    def test_unknown_submission_is_persisted_and_restart_only_checks_results(self):
        def unknown(*args, before_submit, **kwargs):
            before_submit()
            raise CheckinError('connection lost', 'RESULT_UNCERTAIN', uncertain=True)
        self.clients['a'].operate.side_effect = unknown
        result = self.manager.run('a')
        self.assertEqual(result['accounts'][0]['pending']['kind'], 'claim')
        restarted = self.restart()
        restarted.tick()
        restarted.run('a')
        with self.assertRaises(CheckinError):
            restarted.run('a', 'claim')
        self.clients['a'].operate.assert_called_once()
        self.states['a']['campaign']['groupPrizeBtnStatus'] = '2'
        result = restarted.run('a')
        self.assertIsNone(result['accounts'][0]['pending'])
        self.assertEqual(result['accounts'][0]['auto_claim']['state'], 'confirmed')
        self.clients['a'].operate.assert_called_once()

    def test_readback_failure_after_an_accepted_claim_does_not_replay_after_restart(self):
        inspect = self.clients['a'].inspect.side_effect
        self.clients['a'].inspect.side_effect = [ready_observation(), CheckinError('read failed', 'UPSTREAM_NETWORK')]
        result = self.manager.run('a')
        self.assertEqual(result['accounts'][0]['auto_claim']['state'], 'pending')
        self.clients['a'].inspect.side_effect = inspect
        result = self.restart().run('a')
        self.assertEqual(result['accounts'][0]['auto_claim']['state'], 'confirmed')
        self.clients['a'].operate.assert_called_once()

    def test_definitive_rejection_stops_automatic_retries_but_allows_manual_retry(self):
        accepted = self.clients['a'].operate.side_effect
        def rejected(*args, before_submit, **kwargs):
            before_submit()
            error = CheckinError('库存不足', '10007', 409)
            error.definitive_rejection = True
            raise error
        self.clients['a'].operate.side_effect = rejected
        result = self.manager.run('a')
        self.assertEqual(result['accounts'][0]['auto_claim']['state'], 'failed')
        self.assertIsNone(result['accounts'][0]['pending'])
        self.restart().tick()
        self.manager.run('a')
        self.clients['a'].operate.assert_called_once()
        self.clients['a'].operate.side_effect = accepted
        result = self.manager.run('a', 'claim')
        self.assertEqual(result['accounts'][0]['auto_claim']['state'], 'confirmed')

    def test_last_call_confirmation_immediately_claims_without_asking_for_a_second_confirmation(self):
        state = self.states['a']
        state['campaign']['userGroupInfo']['groupTaskStatus'] = 0
        state['campaign']['userGroupInfo']['groupMembers'][0]['callTaskStatus'] = None
        self.manager.run('a', 'call_prepare', destination='13800138001')
        token = self.manager.data['phone_calls']['a']['id']
        def confirm(snapshot, *, before_submit):
            before_submit()
            state['campaign']['userGroupInfo']['groupMembers'][0]['callTaskStatus'] = '1'
            state['campaign']['userGroupInfo']['groupTaskStatus'] = 1
        with patch('activities.native_group.scheduler.sync_confirmed_call', side_effect=confirm) as sync:
            result = self.manager.run('a', 'call_confirm', call_id=token, connected=True)
        sync.assert_called_once()
        self.clients['a'].operate.assert_called_once()
        self.assertEqual(result['accounts'][0]['phone_call']['stage'], 'confirmed')
        self.assertEqual(result['accounts'][0]['auto_claim']['state'], 'confirmed')

    def test_restart_background_claims_each_valid_account_without_opening_a_page(self):
        self.accounts.extend({'id': key, 'status': 'active'} for key in ('b', 'c'))
        for key, state in self.states.items():
            self.manager._save_observation(key, state)
        restarted = self.restart()
        self.addCleanup(restarted.shutdown)
        restarted.start()
        original = restarted.thread
        restarted.start()
        self.assertIs(restarted.thread, original)
        with restarted.condition:
            completed = restarted.condition.wait_for(lambda: all(
                restarted.data['auto_claims'].get(key, {}).get('state') == 'confirmed'
                for key in ('a', 'b', 'c')), timeout=3)
        self.assertTrue(completed)
        restarted.shutdown()
        self.assertFalse(restarted.thread.is_alive())
        for client in self.clients.values():
            client.operate.assert_called_once()
        self.restart().tick()
        for client in self.clients.values():
            client.operate.assert_called_once()

    def test_background_checks_once_per_minute_and_stops_after_confirmed_award(self):
        self.states['a'] = ready_observation(complete=False)
        self.manager._save_observation('a', self.states['a'])
        self.manager.tick()
        self.manager.tick()
        self.clients['a'].inspect.assert_called_once()
        self.now += timedelta(minutes=1)
        self.states['a'] = ready_observation()
        self.manager.tick()
        self.clients['a'].operate.assert_called_once()
        checked = self.clients['a'].inspect.call_count
        self.now += timedelta(minutes=2)
        self.manager.tick()
        self.assertEqual(self.clients['a'].inspect.call_count, checked)

    def test_last_members_completion_wakes_other_owned_teammates_without_waiting_for_next_minute(self):
        self.accounts.extend({'id': key, 'status': 'active'} for key in ('b', 'c'))
        for key in self.states:
            self.manager._save_observation(key, ready_observation(key, complete=False))
            self.manager.next_checks[key] = self.now + timedelta(minutes=1)
        self.manager.run('a')
        self.manager.tick()
        for client in self.clients.values():
            client.operate.assert_called_once()

    def test_manual_claim_waits_for_running_automatic_claim_and_does_not_submit_twice(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event
        entered, release = Event(), Event()
        accepted = self.clients['a'].operate.side_effect
        def held(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(2))
            return accepted(*args, **kwargs)
        self.clients['a'].operate.side_effect = held
        with ThreadPoolExecutor(max_workers=2) as executor:
            automatic = executor.submit(self.manager.run, 'a')
            self.assertTrue(entered.wait(1))
            manual = executor.submit(self.manager.run, 'a', 'claim')
            release.set()
            self.assertEqual(automatic.result()['accounts'][0]['auto_claim']['state'], 'confirmed')
            self.assertEqual(manual.result()['accounts'][0]['auto_claim']['state'], 'confirmed')
        self.clients['a'].operate.assert_called_once()

    def test_unknown_claim_still_reconciles_if_inventory_later_shows_empty(self):
        def unknown(*args, before_submit, **kwargs):
            before_submit()
            raise CheckinError('unknown', 'RESULT_UNCERTAIN', uncertain=True)
        self.clients['a'].operate.side_effect = unknown
        self.manager.run('a')
        self.states['a']['campaign']['groupPrizeBtnStatus'] = '0'
        self.manager.tick()
        self.assertEqual(self.manager.data['observations']['a']['campaign']['groupPrizeBtnStatus'], '0')
        self.states['a']['campaign']['groupPrizeBtnStatus'] = '2'
        self.now += timedelta(minutes=1)
        self.manager.tick()
        self.assertFalse(self.manager.data['pending'])
        self.assertEqual(self.manager.data['auto_claims']['a']['state'], 'confirmed')
        self.clients['a'].operate.assert_called_once()

    def test_ineligible_background_read_is_once_per_day_even_after_restart_or_login_restoration(self):
        self.manager._save_observation('a', ready_observation(complete=False))
        self.clients['a'].inspect.side_effect = CheckinError('不符合资格', '10002', 409)
        self.manager.tick()
        self.now += timedelta(minutes=2)
        restarted = self.restart()
        restarted.login_restored('a')
        restarted.tick()
        self.clients['a'].inspect.assert_called_once()
        self.now += timedelta(days=1)
        restarted.tick()
        self.assertEqual(self.clients['a'].inspect.call_count, 2)


if __name__ == '__main__':
    unittest.main()
