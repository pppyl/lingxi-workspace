from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from checkin_client import CheckinError, FLASH_PRODUCT
from checkin_scheduler import CheckinScheduler, CheckinTaskError
from login_storage import DatabaseLoginStorage
from recharge.cmcc import SHANGHAI


class QueueExecutor:
    def __init__(self):
        self.jobs = []

    def submit(self, fn, *args):
        self.jobs.append((fn, args))

    def drain(self):
        while self.jobs:
            fn, args = self.jobs.pop(0)
            fn(*args)

    def shutdown(self, **kwargs):
        pass


class CheckinSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.time = datetime(2026, 10, 29, 7, 0, tzinfo=SHANGHAI)
        self.accounts = [{'id': 'a', 'name': 'test', 'phone_masked': '199****0001', 'status': 'active'},
                         {'id': 'b', 'name': 'expired', 'phone_masked': '199****0002', 'status': 'login_required'}]
        self.executor = QueueExecutor()
        self.calls = []
        self.flash_options = []
        self.failure = None
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.storage = DatabaseLoginStorage(Path(self.temp.name) / 'db.sqlite3',
                protect=lambda b: b'protected:' + b, unprotect=lambda b: b.removeprefix(b'protected:'))
        self.scheduler = self.create()

    def create(self):
        outer = self
        class Client:
            def __init__(self, credentials):
                self.credentials = credentials

            def inspect(self):
                return {'date': outer.time.strftime('%Y%m%d'), 'today_state': 'unsigned', 'month_count': 0,
                        'products': [], 'signin_message': '未签到', 'flash_state': 'unavailable'}

            def signin(self, *, before_submit):
                before_submit()
                outer.assertTrue(any(r.get('submitted') for r in outer.storage.load_checkin_data()['daily'].values()))
                outer.calls.append(self.credentials())
                if outer.failure:
                    raise outer.failure
                return {**self.inspect(), 'state': 'signed', 'today_state': 'signed', 'month_count': 1,
                        'message': '签到成功', 'submitted': True}

            def flash(self, product, *, before_submit, **options):
                outer.flash_options.append(options)
                # Every request checks cancellation, but the durable marker is
                # written only on the first submission of this live round.
                before_submit()
                before_submit()
                outer.calls.append(product)
                return {'state': 'success', 'message': '已领取', 'attempt_count': 2,
                        'attempts': [{'index': 1, 'state': 'success', 'message': '已领取'}]}
        return CheckinScheduler(lambda key: key, lambda: deepcopy(self.accounts), storage=self.storage,
                                client_factory=Client, now=lambda: self.time, executor=self.executor)

    def configure(self):
        self.scheduler.save({'login_ids': ['a'], 'signin_enabled': True, 'signin_time': '08:00',
                             'flash_enabled': True, 'product_id': FLASH_PRODUCT})

    def test_global_configuration_needs_no_accounts_and_covers_later_logins(self):
        self.scheduler.save({'signin_enabled': True, 'signin_time': '06:30',
            'flash_enabled': True, 'product_id': FLASH_PRODUCT, 'burst_workers': 16})
        self.accounts.append({'id': 'c', 'status': 'active'})
        self.scheduler = self.create()
        self.scheduler.tick()
        self.executor.drain()
        self.assertEqual(self.calls, ['a', 'c'])
        self.assertTrue(all(t['enabled'] for t in self.scheduler.data['tasks'].values()))
        self.assertEqual({t['clock'] for t in self.scheduler.data['tasks'].values() if t['kind'] == 'signin'}, {'06:30'})
        self.assertEqual({t['burst_workers'] for t in self.scheduler.data['tasks'].values() if t['kind'] == 'flash'}, {16})

    def test_new_account_default_concurrency_follows_global_setting_not_legacy_override(self):
        self.configure()
        flash = next(t for t in self.scheduler.data['tasks'].values() if t['kind'] == 'flash')
        flash['burst_workers'] = 10
        self.scheduler._commit(self.scheduler.data)
        self.accounts[1]['status'] = 'active'
        self.scheduler = self.create()
        status = self.scheduler.status()
        new_flash = next(t for t in status['tasks'] if t['login_id'] == 'b' and t['kind'] == 'flash')
        self.assertEqual(new_flash['burst_workers'], status['settings']['burst_workers'])
        self.assertEqual(status['automation']['burst_workers'], status['settings']['burst_workers'])

    def test_late_login_after_clock_automatically_runs_without_new_configuration(self):
        self.scheduler.tick()
        self.time = self.time.replace(hour=9)
        self.accounts[1].update(status='active', credential_revision=2)
        self.scheduler.tick()
        self.executor.drain()
        self.assertEqual(self.calls, ['a', 'b'])
        self.scheduler.tick()
        self.executor.drain()
        self.assertEqual(self.calls, ['a', 'b'])

    def test_relogin_retries_known_signin_rejection_after_next_run_moved_to_tomorrow(self):
        self.accounts[0]['credential_revision'] = 1
        self.time = self.time.replace(hour=9)
        self.failure = CheckinError('GWT_LIMITED', 'GWT_LIMITED')
        self.scheduler.tick()
        self.executor.drain()
        self.scheduler.tick()
        self.assertFalse(self.executor.jobs)
        self.accounts[0]['status'] = 'login_required'
        self.scheduler.tick()
        self.accounts[0].update(status='active', credential_revision=2)
        self.failure = None
        self.scheduler.tick()
        self.executor.drain()
        self.assertEqual(self.calls, ['a', 'a'])
        task = next(t for t in self.scheduler.status()['tasks'] if t['login_id'] == 'a' and t['kind'] == 'signin')
        self.assertEqual(task['state'], 'already_signed')

    def test_temporary_definite_failure_retries_after_five_minutes_without_relogin(self):
        self.time = self.time.replace(hour=9)
        self.failure = CheckinError('GWT_LIMITED', 'GWT_LIMITED')
        self.scheduler.tick()
        self.executor.drain()
        failed = next(t for t in self.scheduler.data['tasks'].values() if t['kind'] == 'signin')
        self.assertEqual(datetime.fromisoformat(failed['next_run_at']), self.time+timedelta(minutes=5))
        self.time += timedelta(minutes=5)
        self.failure = None
        self.scheduler.tick()
        self.executor.drain()
        self.assertEqual(self.calls, ['a', 'a'])

    def test_relogin_after_uncertain_submission_only_queries_and_does_not_resubmit(self):
        self.time = self.time.replace(hour=9)
        self.failure = CheckinError('request timeout', uncertain=True)
        self.scheduler.tick()
        self.executor.drain()
        self.accounts[0]['credential_revision'] = 2
        self.failure = None
        self.scheduler.tick()
        self.executor.drain()
        self.assertEqual(self.calls, ['a'])

    def test_credentials_changed_during_failed_job_are_reconciled_without_waiting(self):
        self.time = self.time.replace(hour=9)
        self.accounts[0]['credential_revision'] = 1
        self.failure = CheckinError('GWT_LIMITED', 'GWT_LIMITED')
        self.scheduler.tick()
        self.accounts[0]['credential_revision'] = 2
        self.scheduler.tick()
        self.executor.drain()
        self.failure = None
        self.scheduler.tick()
        self.executor.drain()
        self.assertEqual(self.calls, ['a', 'a'])

    def test_task_view_uses_official_today_signin_even_if_schedule_was_reset(self):
        self.configure()
        self.scheduler.data['observations']['a'] = {'date': self.time.strftime('%Y%m%d'),
            'today_state': 'signed', 'query_message': 'old credentials changed'}
        status = self.scheduler.status()
        task = next(t for t in status['tasks'] if t['login_id'] == 'a' and t['kind'] == 'signin')
        self.assertEqual(task['state'], 'already_signed')
        self.assertEqual(status['accounts'][0]['signin_message'], '今日已签到')

    def test_daily_clock_and_no_early_or_duplicate_dispatch(self):
        self.configure()
        self.scheduler.tick()
        self.assertFalse(self.executor.jobs)
        self.time = self.time.replace(hour=8)
        self.scheduler.tick()
        self.scheduler.tick()
        self.assertEqual(len(self.executor.jobs), 1)
        self.executor.drain()
        self.scheduler.run(['a'])
        self.assertFalse(self.executor.jobs)
        self.time += timedelta(days=1)
        self.scheduler.tick()
        self.executor.drain()
        self.assertEqual(self.calls, ['a', 'a'])

    def test_noon_task_survives_restart_and_uses_stable_product(self):
        self.configure()
        self.scheduler = self.create()
        self.time = self.time.replace(hour=12)
        self.scheduler.tick()
        self.executor.drain()
        self.assertIn(FLASH_PRODUCT, self.calls)
        self.scheduler = self.create()
        self.scheduler.tick()
        self.executor.drain()
        self.assertEqual(self.calls.count(FLASH_PRODUCT), 1)

    def test_mutation_timeout_is_not_retried_after_restart(self):
        self.failure = CheckinError('结果不确定', uncertain=True)
        self.scheduler.run(['a'])
        self.executor.drain()
        self.scheduler = self.create()
        self.scheduler.run(['a'])
        self.executor.drain()
        self.assertEqual(self.calls, ['a'])

    def test_flash_prewarms_thirty_seconds_early_once_and_keeps_noon_target(self):
        self.configure()
        self.time = self.time.replace(hour=8)
        self.scheduler.tick()
        self.executor.drain()
        self.calls.clear()
        self.time = self.time.replace(hour=11, minute=59, second=29)
        self.scheduler.tick()
        self.assertFalse(self.executor.jobs)
        self.time += timedelta(seconds=1)
        self.scheduler.tick()
        self.scheduler.tick()
        self.assertEqual(len(self.executor.jobs), 1)
        self.executor.drain()
        options = self.flash_options[0]
        self.assertEqual(options['target_at'], self.time.replace(hour=12, minute=0, second=0))
        flash = next(t for t in self.scheduler.data['tasks'].values() if t['kind'] == 'flash')
        self.assertIn('2026-10-30T12:00', flash['next_run_at'])
        self.assertEqual(flash['history'][-1]['attempt_count'], 2)
        self.assertNotIn('attempts', self.scheduler.data['observations']['a'])

    def test_burst_configuration_survives_restart_and_reaches_worker(self):
        self.scheduler.save({'login_ids': ['a'], 'signin_enabled': False, 'flash_enabled': True,
                            'product_id': FLASH_PRODUCT, 'burst_count': 12, 'burst_interval': 0.08,
                            'burst_workers': 4})
        self.scheduler = self.create()
        self.time = self.time.replace(hour=12)
        self.scheduler.tick()
        self.executor.drain()
        self.assertEqual(self.flash_options[0]['burst'], {'burst_count': 12, 'burst_interval': 0.08, 'burst_workers': 4})
        self.scheduler = self.create()
        self.scheduler.tick()
        self.assertFalse(self.executor.jobs)

    def test_restart_before_first_request_resumes_same_noon_round(self):
        self.scheduler.save({'login_ids': ['a'], 'signin_enabled': False, 'flash_enabled': True,
                             'product_id': FLASH_PRODUCT})
        self.time = self.time.replace(hour=11, minute=59, second=30)
        self.scheduler.tick()
        self.executor.jobs.clear()  # Process exits during prewarming, before submission.
        self.scheduler = self.create()
        self.time += timedelta(seconds=5)
        self.scheduler.tick()
        self.executor.drain()
        self.assertEqual(self.calls, [FLASH_PRODUCT])
        self.scheduler = self.create()
        self.scheduler.tick()
        self.assertFalse(self.executor.jobs)

    def test_invalid_burst_parameters_are_rejected_without_partial_save(self):
        payload = {'login_ids': ['a'], 'signin_enabled': False, 'flash_enabled': True, 'product_id': FLASH_PRODUCT}
        for key, value in [('burst_count', 0), ('burst_count', True), ('burst_count', 101),
                           ('burst_workers', 0), ('burst_workers', 33), ('burst_interval', 0),
                           ('burst_interval', float('nan')), ('burst_interval', float('inf'))]:
            with self.subTest(key=key, value=value), self.assertRaises(CheckinTaskError):
                self.scheduler.save({**payload, key: value})
            self.assertEqual(self.scheduler.data['tasks'], {})

    def test_default_concurrency_persists_and_can_preserve_individual_tasks(self):
        self.configure()
        before = deepcopy(self.scheduler.data['tasks'])
        result = self.scheduler.update_settings({'burst_workers': 16, 'apply_existing': False})
        self.assertEqual(result['affected_tasks'], 0)
        self.assertEqual(self.scheduler.data['tasks'], before)
        self.scheduler = self.create()
        self.assertEqual(self.scheduler.status()['settings']['burst_workers'], 16)
        self.accounts.append({'id': 'c', 'name': 'new', 'phone_masked': '199****0003', 'status': 'active'})
        self.scheduler.save({'login_ids': ['c'], 'signin_enabled': False, 'flash_enabled': True,
                             'product_id': FLASH_PRODUCT})
        task = next(t for t in self.scheduler.data['tasks'].values() if t['login_id'] == 'c' and t['kind'] == 'flash')
        self.assertEqual(task['burst_workers'], 16)

    def test_sync_concurrency_preserves_signin_schedules_and_inflight_round(self):
        self.configure()
        self.time = self.time.replace(hour=11, minute=59, second=30)
        self.scheduler.tick()
        before = deepcopy(self.scheduler.data['tasks'])
        flash = next(t for t in self.scheduler.data['tasks'].values() if t['kind'] == 'flash')
        next_run = flash['next_run_at']
        result = self.scheduler.update_settings({'burst_workers': 32, 'apply_existing': True})
        self.assertEqual(result['affected_tasks'], 1)
        self.assertEqual(flash['state'], 'preparing')
        updated = self.scheduler.data['tasks'][flash['id']]
        self.assertEqual((updated['burst_workers'], updated['state'], updated['next_run_at']), (32, 'preparing', next_run))
        signin = next(t for t in self.scheduler.data['tasks'].values() if t['kind'] == 'signin')
        self.assertEqual(signin, before[signin['id']])
        self.executor.drain()
        self.assertEqual(self.flash_options[0]['burst']['burst_workers'], 8)
        self.time = self.time.replace(day=30)
        self.scheduler.tick();self.executor.drain()
        self.assertEqual(self.flash_options[-1]['burst']['burst_workers'], 32)

    def test_invalid_concurrency_settings_are_atomic(self):
        before = deepcopy(self.scheduler.data)
        for payload in ({'burst_workers': value} for value in (0, 33, True, 1.5, '16', None)):
            with self.subTest(payload=payload), self.assertRaises(CheckinTaskError):
                self.scheduler.update_settings(payload)
            self.assertEqual(self.scheduler.data, before)
        with self.assertRaises(CheckinTaskError):
            self.scheduler.update_settings({'burst_workers': 16, 'apply_existing': 'yes'})
        self.assertEqual(self.scheduler.data, before)

    def test_existing_flash_tasks_receive_defaults_without_changing_schedule(self):
        self.configure()
        saved = self.storage.load_checkin_data()
        saved.pop('settings')
        flash = next(t for t in saved['tasks'].values() if t['kind'] == 'flash')
        for key in ['burst_count', 'burst_interval', 'burst_workers']:
            flash.pop(key)
        before = flash['next_run_at']
        self.storage.save_checkin_data(saved)
        self.scheduler = self.create()
        migrated = self.scheduler.data['tasks'][flash['id']]
        self.assertEqual((migrated['burst_count'], migrated['burst_interval'], migrated['burst_workers']), (40, 0.05, 8))
        self.assertEqual(migrated['next_run_at'], before)
        self.assertEqual(self.scheduler.data['settings']['burst_workers'], 8)

    def test_crash_marker_persisted_before_post(self):
        self.scheduler.run(['a'])
        fn, args = self.executor.jobs.pop()
        self.scheduler = self.create()
        # Reservation without submission can be retried after a process crash.
        self.scheduler.run(['a'])
        self.executor.drain()
        saved = self.storage.load_checkin_data()
        record = next(iter(saved['daily'].values()))
        self.assertTrue(record['submitted'])
        self.scheduler = self.create()
        self.scheduler.run(['a'])
        self.assertFalse(self.executor.jobs)

    def test_stop_queued_job_prevents_upstream_write(self):
        self.scheduler.run(['a'])
        task_id = next(iter(self.scheduler.data['tasks']))
        self.scheduler.stop([task_id])
        self.executor.drain()
        self.assertFalse(self.calls)
        self.assertFalse(self.scheduler.data['tasks'][task_id]['enabled'])

    def test_missed_noon_is_skipped_instead_of_late_claim(self):
        self.configure()
        self.time = self.time.replace(hour=18)
        self.scheduler.tick()
        self.executor.drain()
        self.assertNotIn(FLASH_PRODUCT, self.calls)
        flash = next(t for t in self.scheduler.data['tasks'].values() if t['kind'] == 'flash')
        self.assertEqual(flash['state'], 'skipped')
        self.assertIn('2026-10-30T12:00', flash['next_run_at'])

    def test_status_enrolls_active_accounts_and_lists_expired_without_fabricated_zero(self):
        status = self.scheduler.status()
        self.assertEqual(len(status['accounts']), 2)
        self.assertEqual({(t['login_id'], t['kind'], t['enabled']) for t in status['tasks']},
                         {('a', 'signin', True), ('a', 'flash', True)})
        self.assertIsNone(status['accounts'][0]['month_count'])
        self.assertEqual(status['accounts'][0]['today_state'], 'unknown')
        self.assertEqual(len(self.executor.jobs), 1)
        self.executor.drain()
        self.assertEqual(self.scheduler.status()['accounts'][0]['month_count'], 0)
        self.scheduler.status()
        self.assertFalse(self.executor.jobs)

    def test_new_day_clears_yesterday_today_state(self):
        self.scheduler.run(['a'])
        self.executor.drain()
        self.assertEqual(self.scheduler.status()['accounts'][0]['today_state'], 'signed')
        self.time += timedelta(days=1)
        self.assertEqual(self.scheduler.status()['accounts'][0]['today_state'], 'unknown')

    def test_input_validation_does_not_create_any_tasks(self):
        with self.assertRaises(CheckinTaskError):
            self.scheduler.save({'login_ids': ['missing']})
        with self.assertRaises(CheckinTaskError):
            self.scheduler.run(['b'])
        self.assertFalse(self.scheduler.data['tasks'])

    def test_start_after_missed_time_checks_and_signs_today(self):
        self.time = self.time.replace(hour=18)
        self.configure()  # Future clock is tomorrow, but launch still reconciles today.
        self.scheduler = self.create()
        self.scheduler.check_startup()
        self.executor.drain()
        self.assertEqual(self.calls, ['a'])
        self.assertEqual(self.scheduler.status()['accounts'][0]['today_state'], 'signed')
        self.scheduler = self.create()
        self.scheduler.check_startup()
        self.executor.drain()
        self.assertEqual(self.calls, ['a'])

    def test_start_before_schedule_only_checks_and_does_not_sign_early(self):
        self.configure()
        self.scheduler.check_startup()
        self.executor.drain()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.scheduler.status()['accounts'][0]['today_state'], 'unsigned')

    def test_account_cannot_be_removed_while_queued(self):
        self.scheduler.run(['a'])
        with self.assertRaises(CheckinTaskError):
            self.scheduler.forget('a')
        self.executor.drain()
        self.scheduler.forget('a')
        self.assertEqual(self.scheduler.data['tasks'], {})
        self.assertEqual(self.scheduler.data['observations'], {})
        self.assertEqual(self.scheduler.data['daily'], {})

    def test_logout_deletes_only_target_signin_data_and_keeps_settings(self):
        self.configure()
        self.scheduler.run(['a']);self.executor.drain()
        self.accounts[1]['status'] = 'active'
        self.scheduler.save({'login_ids': ['b'], 'signin_enabled': True, 'flash_enabled': False})
        self.scheduler.run(['b']);self.executor.drain()
        self.scheduler.update_settings({'burst_workers': 16})
        before = deepcopy(self.scheduler.data)
        self.scheduler.forget('a')
        self.assertTrue(all(t['login_id'] == 'b' for t in self.scheduler.data['tasks'].values()))
        self.assertEqual(self.scheduler.data['observations'], {'b': before['observations']['b']})
        self.assertEqual(self.scheduler.data['daily'], {key: value for key, value in before['daily'].items() if key.startswith('b:')})
        self.assertEqual(self.scheduler.data['settings'], {'burst_workers': 16})
        for cache in (self.scheduler.checked, self.scheduler.refreshing, self.scheduler.account_locks):
            self.assertNotIn('a', cache)
        self.assertEqual(self.storage.load_checkin_data(), self.scheduler.data)
        self.assertFalse(self.scheduler._enqueue('a', 'signin', scheduled_at=self.time))
        self.assertTrue(all(t['login_id'] == 'b' for t in self.scheduler.data['tasks'].values()))

    def test_restart_purges_legacy_exited_accounts_but_keeps_expired_accounts(self):
        self.configure()
        self.scheduler.run(['a']);self.executor.drain()
        self.accounts[1]['status'] = 'active'
        self.scheduler.save({'login_ids': ['b'], 'signin_enabled': True, 'flash_enabled': False})
        self.accounts = [{**self.accounts[1], 'status': 'login_required'}]
        self.scheduler = self.create()
        data = self.storage.load_checkin_data()
        self.assertTrue(all(task['login_id'] == 'b' for task in data['tasks'].values()))
        self.assertNotIn('a', data['observations'])
        self.assertFalse(any(key.startswith('a:') for key in data['daily']))
        self.assertEqual([task['login_id'] for task in self.scheduler.status()['tasks']], ['b', 'b'])

    def test_background_refresh_cannot_recreate_data_after_logout(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                self.accounts = [{'id': 'a', 'name': 'test', 'status': 'active'}]
                self.scheduler = self.create()
                self.scheduler.status()
                def inspect():
                    self.scheduler.forget('a')
                    self.accounts = []
                    if fail:
                        raise CheckinError('账号已退出', 'APP_LOGIN_REQUIRED', 401)
                    return {'today_state': 'signed', 'month_count': 99}
                self.scheduler.client_factory = lambda _: Mock(inspect=inspect)
                self.executor.drain()
                self.assertEqual(self.storage.load_checkin_data()['observations'], {})
                self.assertEqual(self.scheduler.checked, {})
                self.assertEqual(self.scheduler.refreshing, set())
                self.assertEqual(self.scheduler.account_locks, {})


class CheckinApiTests(unittest.TestCase):
    def setUp(self):
        from app import create_app
        self.app = create_app()
        self.client = self.app.test_client()
        self.addCleanup(self.app.extensions['checkin_scheduler'].shutdown)

    def test_page_and_status_do_not_expose_credentials(self):
        page = self.client.get('/checkin')
        self.assertEqual(page.status_code, 200)
        self.assertIn('checkin-accounts', page.get_data(as_text=True))
        status = self.client.get('/api/checkin/status').get_json()
        self.assertEqual(status['accounts'], [])
        self.assertEqual(status['tasks'], [])
        self.assertNotIn('cookie', str(status).lower())

    def test_invalid_mutations_are_rejected(self):
        self.assertEqual(self.client.post('/api/checkin/run', json={'kind': 'flash', 'login_ids': []}).status_code, 400)
        self.assertEqual(self.client.post('/api/checkin/run', json={'kind': 'signin', 'login_ids': ['missing']}).status_code, 400)
        self.assertEqual(self.client.post('/api/checkin/tasks/stop', json={'ids': ['missing']}).status_code, 404)
        self.assertEqual(self.client.post('/api/checkin/tasks/save', json=[]).status_code, 400)

    def test_concurrency_setting_is_available_from_signin_page_and_saved_to_status(self):
        self.assertIn('checkin-settings-open', self.client.get('/checkin').get_data(as_text=True))
        response = self.client.post('/api/checkin/settings', json={'burst_workers': 16, 'apply_existing': True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['settings']['burst_workers'], 16)
        self.assertEqual(self.client.get('/api/checkin/status').get_json()['settings']['burst_workers'], 16)
        self.assertEqual(self.client.post('/api/checkin/settings', json={'burst_workers': 33}).status_code, 400)


if __name__ == '__main__':
    unittest.main()
