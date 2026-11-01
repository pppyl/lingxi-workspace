from copy import deepcopy
from datetime import datetime, timedelta
import json
import unittest

from recharge.cmcc import SHANGHAI
from activities.web_checkin.client import WebCheckinError
from activities.web_checkin.scheduler import WebCheckinScheduler


class Storage:
    def __init__(self):
        self.values, self.writes = {}, []

    def load_activity_data(self, key):
        return deepcopy(self.values.get(key))

    def save_activity_data(self, key, data):
        self.values[key] = deepcopy(data)
        self.writes.append((key, deepcopy(data)))


class Queue:
    def __init__(self):
        self.tasks = []

    def submit(self, function, *args):
        self.tasks.append((function, args))

    def drain(self):
        while self.tasks:
            function, args = self.tasks.pop(0)
            function(*args)

    def shutdown(self, wait=False):
        pass


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 1, 9, 0, tzinfo=SHANGHAI)
        self.pool = [dict(id='a', name='一号', phone_masked='138****0001', status='active'),
                     dict(id='b', name='二号', phone_masked='138****0002', status='active')]
        self.storage, self.queue = Storage(), Queue()
        self.queries, self.signs, self.signed, self.completed = [], [], set(), set()
        self.query_error = self.sign_error = None
        self.during_query = None
        outer = self

        class Client:
            def __init__(self, credentials):
                self.credentials = credentials

            def status(self):
                login_id = self.credentials()['id']
                outer.queries.append(login_id)
                if outer.during_query:
                    callback, outer.during_query = outer.during_query, None
                    callback()
                if outer.query_error:
                    raise outer.query_error
                return {'today_signed': login_id in outer.signed, 'sign_date': outer.now.date().isoformat(),
                        'cycle_complete': login_id in outer.completed,
                        'marked_times': 3 if login_id in outer.completed else len(outer.signed), 'total_times': 3, 'prizes': [],
                        'channel': 'dlwmh', 'message': '官方查询结果', 'token': 'must-not-leak'}

            def signin(self, before_submit):
                state = self.status()
                if state['today_signed'] or state['cycle_complete']:
                    return state
                login_id = self.credentials()['id']
                before_submit()
                saved = outer.storage.values['web-checkin']['attempts'][login_id][outer.now.date().isoformat()]
                outer.assertTrue(saved['submitted'])
                outer.signs.append(login_id)
                if outer.sign_error:
                    raise outer.sign_error
                outer.signed.add(login_id)
                return self.status()

        self.client_factory = Client
        self.scheduler = self.make()

    def make(self):
        return WebCheckinScheduler(lambda key: {'id': key, 'phone': '13800000001', 'cookie': 'secret'},
            lambda: deepcopy(self.pool), storage=self.storage, client_factory=self.client_factory,
            executor=self.queue, now=lambda: self.now)

    def test_default_covers_all_valid_accounts_and_status_never_fetches(self):
        self.pool.append(dict(id='expired', status='login_required'))
        self.scheduler.status()
        self.scheduler.status()
        self.assertEqual(self.queries, [])
        self.scheduler.tick()
        self.assertEqual(len(self.queue.tasks), 2)
        self.queue.drain()
        self.assertEqual(self.signs, ['a', 'b'])
        self.scheduler.tick()
        self.assertEqual(self.queue.tasks, [])
        self.assertTrue(all(row['today_signed'] for row in self.scheduler.status()['accounts'][:2]))
        self.assertEqual(set(self.storage.values), {'web-checkin'})

    def test_time_is_start_time_and_late_account_is_included(self):
        self.now = self.now.replace(hour=7)
        self.scheduler.tick()
        self.assertFalse(self.queue.tasks)
        self.now = self.now.replace(hour=18)
        self.scheduler.tick()
        self.queue.drain()
        self.pool.append(dict(id='late', status='active', phone_masked='138****0003'))
        self.scheduler.tick()
        self.queue.drain()
        self.assertEqual(self.signs, ['a', 'b', 'late'])

    def test_recovered_shared_login_catches_up_today(self):
        self.pool[1]['status'] = 'login_required'
        self.scheduler.tick()
        self.queue.drain()
        self.pool[1]['status'] = 'active'
        self.scheduler.tick()
        self.queue.drain()
        self.assertEqual(self.signs, ['a', 'b'])

    def test_restart_does_not_repeat_confirmed_signin(self):
        self.scheduler.tick()
        self.queue.drain()
        self.scheduler = self.make()
        self.scheduler.tick()
        self.assertFalse(self.queue.tasks)

    def test_new_day_has_independent_completion(self):
        self.scheduler.tick()
        self.queue.drain()
        self.now += timedelta(days=1)
        self.signed.clear()
        self.assertFalse(self.scheduler.status()['accounts'][0]['today_signed'])
        self.assertIn('尚未查询', self.scheduler.status()['accounts'][0]['message'])
        self.scheduler.tick()
        self.queue.drain()
        self.assertEqual(self.signs, ['a', 'b', 'a', 'b'])

    def test_settings_are_global_and_manual_query_is_still_available_when_disabled(self):
        self.scheduler.update_settings({'enabled': False, 'time': '09:31'})
        self.scheduler.tick()
        self.assertFalse(self.queue.tasks)
        self.scheduler.run('a', 'refresh')
        self.queue.drain()
        self.assertEqual(self.queries, ['a'])
        self.assertFalse(self.signs)
        self.assertIsNone(self.scheduler.status()['accounts'][0]['next_run_at'])

    def test_settings_validation(self):
        for payload in ({}, None, {'enabled': 1, 'time': '08:00'}, {'enabled': True, 'time': '25:00'}):
            with self.subTest(payload=payload), self.assertRaises(WebCheckinError):
                self.scheduler.update_settings(payload)

    def test_each_explicit_refresh_queries_official_progress(self):
        for _ in range(2):
            self.scheduler.run('a', 'refresh')
            self.queue.drain()
        self.assertEqual(self.queries, ['a', 'a'])
        self.assertFalse(self.signs)

    def test_signin_does_not_prepend_a_duplicate_status_call(self):
        self.scheduler.run('a', 'signin')
        self.queue.drain()
        # The client does its own before-and-after official progress checks.
        self.assertEqual(self.queries, ['a', 'a'])
        self.assertEqual(self.signs, ['a'])

    def test_already_signed_client_skips_mutation(self):
        self.signed.add('a')
        self.scheduler.run('a', 'signin')
        self.queue.drain()
        self.assertEqual(self.queries, ['a'])
        self.assertFalse(self.signs)

    def test_completed_cycle_without_today_sign_is_success_not_unknown(self):
        self.completed.add('a')
        self.scheduler.run('a', 'signin')
        self.queue.drain()
        row = self.scheduler.status()['accounts'][0]
        self.assertEqual(row['activity_state'], 'completed')
        self.assertTrue(row['cycle_complete'])
        self.assertFalse(row['today_signed'])
        self.assertEqual(row['message'], '本周期签到已完成。')
        self.assertEqual(row['next_run_at'], '2026-11-02T08:00:00+08:00')
        self.assertFalse(self.signs)
        self.assertFalse(self.scheduler.data['attempts']['a']['2026-11-01']['uncertain'])
        self.assertTrue(self.storage.values['web-checkin']['observations']['a']['cycle_complete'])

    def test_completed_cycle_is_only_checked_once_daily_including_restart(self):
        self.pool = [self.pool[0]]
        self.completed.add('a')
        self.scheduler.tick()
        self.queue.drain()
        self.assertEqual(self.queries, ['a'])
        self.now += timedelta(minutes=10)
        self.scheduler = self.make()
        for _ in range(3):
            self.scheduler.tick()
            self.queue.drain()
        self.assertEqual(self.queries, ['a'])
        self.now += timedelta(days=1)
        self.scheduler.tick()
        self.queue.drain()
        self.scheduler.tick()
        self.assertEqual(self.queries, ['a', 'a'])
        self.assertFalse(self.queue.tasks)
        self.assertFalse(self.signs)

    def test_cycle_reset_next_month_resumes_signin_after_official_check(self):
        self.pool = [self.pool[0]]
        self.completed.add('a')
        self.scheduler.tick()
        self.queue.drain()
        self.now = self.now.replace(month=12)
        self.completed.clear()
        self.scheduler.tick()
        self.queue.drain()
        self.assertEqual(self.signs, ['a'])
        self.assertFalse(self.scheduler.status()['accounts'][0]['cycle_complete'])
        self.assertTrue(self.scheduler.status()['accounts'][0]['today_signed'])

    def test_official_cycle_completion_reconciles_previous_unknown_without_replay(self):
        self.storage.values['web-checkin']['attempts'] = {'a': {'2026-11-01': {
            'state': 'running', 'submitted': True, 'started_at': (self.now-timedelta(minutes=10)).isoformat()}}}
        self.completed.add('a')
        self.scheduler = self.make()
        self.scheduler.run('a', 'refresh')
        self.queue.drain()
        self.assertEqual(self.scheduler.status()['accounts'][0]['activity_state'], 'completed')
        self.assertFalse(self.signs)
        self.assertFalse(self.scheduler.data['attempts']['a']['2026-11-01']['uncertain'])

    def test_completed_cycle_today_still_reports_official_today_sign(self):
        self.completed.add('a')
        self.signed.add('a')
        self.scheduler.run('a', 'refresh')
        self.queue.drain()
        row = self.scheduler.status()['accounts'][0]
        self.assertEqual(row['activity_state'], 'signed')
        self.assertTrue(row['cycle_complete'])
        self.assertTrue(row['today_signed'])

    def test_ineligible_is_checked_once_per_day_even_after_restart_or_shared_revision_change(self):
        self.query_error = WebCheckinError('不符合资格', 'INELIGIBLE', 409)
        self.scheduler.tick()
        self.queue.drain()
        self.now += timedelta(minutes=10)
        self.pool[0]['credential_revision'] = 99
        self.scheduler = self.make()
        self.scheduler.tick()
        self.assertFalse(self.queue.tasks)
        self.assertEqual(self.queries, ['a', 'b'])
        self.now += timedelta(days=1)
        self.scheduler.tick()
        self.queue.drain()
        self.assertEqual(len(self.queries), 4)

    def test_rate_limit_stops_today(self):
        self.query_error = WebCheckinError('请求频繁', '-800', 429)
        self.scheduler.tick()
        self.queue.drain()
        self.now += timedelta(hours=1)
        self.scheduler.tick()
        self.assertFalse(self.queue.tasks)
        self.assertEqual(self.scheduler.status()['accounts'][0]['activity_state'], 'rate_limited')

    def test_transient_failures_are_five_minutes_apart_and_bounded_to_five(self):
        self.query_error = WebCheckinError('连接失败', 'NETWORK_ERROR', 502)
        for _ in range(5):
            self.scheduler.tick()
            self.queue.drain()
            self.scheduler.tick()
            self.assertFalse(self.queue.tasks)
            self.now += timedelta(minutes=5)
        self.scheduler = self.make()
        self.scheduler.tick()
        self.assertFalse(self.queue.tasks)
        self.assertEqual(len(self.queries), 10)

    def test_missing_activity_login_waits_for_manual_login_without_sms_or_app_conversion(self):
        self.query_error = WebCheckinError('请先登录网页渠道', 'LOGIN_REQUIRED', 401)
        self.scheduler.tick()
        self.queue.drain()
        self.now += timedelta(hours=1)
        self.scheduler.tick()
        self.assertFalse(self.queue.tasks)
        self.query_error = None
        self.scheduler.logged_in('a')
        self.queue.drain()
        self.assertEqual(self.signs, ['a'])
        self.assertEqual(self.scheduler.status()['accounts'][0]['activity_state'], 'signed')

    def test_shared_login_recovery_rechecks_app_login_error_without_waiting_five_minutes(self):
        self.query_error = WebCheckinError('共享身份待恢复', 'APP_LOGIN_REQUIRED', 401)
        self.scheduler.tick()
        self.queue.drain()
        self.pool[0]['status'] = 'login_required'
        self.scheduler.tick()
        self.pool[0].update(status='active', credential_revision=2)
        self.query_error = None
        self.scheduler.tick()
        self.queue.drain()
        self.assertEqual(self.signs, ['a'])

    def test_unknown_submission_only_rechecks_and_never_replays(self):
        self.sign_error = WebCheckinError('结果不确定', 'UPSTREAM_NETWORK', 502, uncertain=True)
        self.scheduler.run('a', 'signin')
        self.queue.drain()
        self.assertEqual(self.signs, ['a'])
        self.now += timedelta(minutes=5)
        self.scheduler.tick()
        self.queue.drain()
        self.scheduler.run('a', 'signin')
        self.queue.drain()
        self.assertEqual(self.signs.count('a'), 1)
        self.signed.add('a')
        self.scheduler.run('a', 'refresh')
        self.queue.drain()
        self.assertEqual(self.scheduler.status()['accounts'][0]['activity_state'], 'signed')

    def test_explicit_submission_rejection_does_not_become_unknown(self):
        for code, expected in [('USER_LIMITED', 'ineligible'), ('-800', 'rate_limited'), ('REJECTED', 'failed')]:
            with self.subTest(code=code):
                self.sign_error = WebCheckinError('官方明确拒绝', code, 409, uncertain=False)
                self.scheduler.run('a', 'signin')
                self.queue.drain()
                row = self.scheduler.status()['accounts'][0]
                self.assertEqual(row['activity_state'], expected)
                record = self.scheduler.data['attempts']['a']['2026-11-01']
                self.assertFalse(record['uncertain'])
                self.assertFalse(record['submitted'])

    def test_interrupted_submitted_record_survives_restart_as_unknown(self):
        self.storage.values['web-checkin']['attempts'] = {'a': {'2026-11-01': {
            'state': 'running', 'submitted': True, 'started_at': (self.now-timedelta(minutes=10)).isoformat()}}}
        self.scheduler = self.make()
        self.scheduler.run('a', 'signin')
        self.queue.drain()
        self.assertFalse(self.signs)
        self.assertEqual(self.scheduler.status()['accounts'][0]['activity_state'], 'unknown_result')

    def test_concurrent_run_coalesces_per_account(self):
        self.scheduler.run('a', 'signin')
        self.scheduler.run('a', 'signin')
        self.assertEqual(len(self.queue.tasks), 1)
        self.queue.drain()
        self.assertEqual(self.signs, ['a'])

    def test_login_finished_during_query_is_not_dropped(self):
        self.during_query = lambda: self.scheduler.logged_in('a')
        self.scheduler.run('a', 'refresh')
        self.queue.drain()
        self.assertEqual(self.signs, ['a'])
        self.assertNotIn('a', self.scheduler.pending_login)

    def test_forget_removes_all_backend_state_and_old_accounts_pruned_at_startup(self):
        self.scheduler.tick()
        self.queue.drain()
        self.scheduler.forget('a')
        for key in ('observations', 'records', 'attempts'):
            self.assertNotIn('a', self.storage.values['web-checkin'][key])
        self.pool = [self.pool[0]]
        self.scheduler = self.make()
        for key in ('observations', 'records', 'attempts'):
            self.assertNotIn('b', self.storage.values['web-checkin'][key])

    def test_public_and_stored_state_do_not_contain_raw_credentials_or_upstream_extra_fields(self):
        self.query_error = WebCheckinError('号码13800000001暂不可用', 'NETWORK_ERROR', 502)
        self.scheduler.run('a', 'refresh')
        self.queue.drain()
        self.query_error = None
        self.scheduler.run('a', 'refresh')
        self.queue.drain()
        value = json.dumps({'public': self.scheduler.status(), 'stored': self.storage.values})
        for secret in ('13800000001', 'must-not-leak', 'secret', 'cookie', 'token'):
            self.assertNotIn(secret, value)

    def test_invalid_or_inactive_account_and_unknown_action_are_rejected(self):
        self.pool[0]['status'] = 'login_required'
        for login_id, kind in [('missing', 'refresh'), ('a', 'signin'), ('b', 'purchase')]:
            with self.subTest(login_id=login_id, kind=kind), self.assertRaises(WebCheckinError):
                self.scheduler.run(login_id, kind)

    def test_shutdown_prevents_queued_mutation(self):
        self.scheduler.run('a', 'signin')
        self.scheduler.shutdown()
        self.queue.drain()
        self.assertFalse(self.signs)


if __name__ == '__main__':
    unittest.main()
