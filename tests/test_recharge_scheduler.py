import json
import random
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

from recharge.accounts import AccountManager
from recharge.app import create_app
from recharge.cmcc import ActivityClient, ActivityError, SHANGHAI
from recharge.scheduler import ClaimScheduler, retry_policy

NOW = datetime(2026, 10, 28, 8, 0, tzinfo=SHANGHAI)


class FakeClient:
    def __init__(self, profile_dir=None):
        self.profile_dir = profile_dir
        self.mode = 'cookie'
        self.operation_lock = threading.RLock()
        self.total_submissions = 0
        self.clock_anchor = None
        self.current = NOW
        self.calls = 0
        self.failures = 0
        self.permanent = False
        self.barrier = None
        self.bridge = Mock()
        self.draw_times = []

    def server_now(self):
        return self.current

    def refresh(self, **kwargs):
        if self.barrier:
            self.barrier.wait(timeout=2)
        return {'authenticated': True, 'mode': 'cookie', 'phone': '138****8000',
                'device_ready': True, 'start_hour': 8, 'can_claim': True,
                'rd_status': 1, 'server_time': self.current.isoformat()}

    def identity(self):
        return self.refresh()

    def claim(self, **kwargs):
        self.calls += 1
        self.total_submissions += 1
        self.draw_times.append(self.current)
        if self.barrier:
            self.barrier.wait(timeout=2)
        if self.calls <= self.failures:
            raise ActivityError('模拟失败', 'GOTO_LOGIN' if self.permanent else 'TEMPORARY',
                                401 if self.permanent else 502)
        return {'success': True, 'message': '模拟成功', 'prizes': []}

    def open_login(self, *args):
        if self.barrier:
            self.barrier.wait(timeout=2)
        self.mode = 'browser'

    def logout(self):
        self.mode = 'none'


class RecordingEvent:
    def __init__(self, cancel_on_wait=False, advance=None):
        self.intervals = []
        self.cancel_on_wait = cancel_on_wait
        self.cancelled = False
        self.advance = advance

    def is_set(self):
        return self.cancelled

    def wait(self, interval):
        self.intervals.append(interval)
        if self.advance:
            self.advance(interval)
        self.cancelled = self.cancel_on_wait
        return self.cancelled


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.manager = AccountManager(default_client=self.client, client_factory=FakeClient)
        self.scheduler = ClaimScheduler(self.manager, now=lambda: NOW, rng=random.Random(5))

    def arm(self, **settings):
        self.scheduler.schedule(['default'], settings, immediate=True)

    def test_success_stops_its_account_immediately(self):
        self.client.failures = 2
        self.arm()
        event = RecordingEvent()
        self.scheduler._run_attempts('default', event)
        job = self.scheduler.snapshots()['default']
        self.assertEqual(job['state'], 'success')
        self.assertEqual(self.client.calls, 3)
        self.assertEqual(job['attempts'], 3)
        self.assertEqual(job['submissions'], 3)
        self.assertEqual(len(event.intervals), 2)
        self.assertFalse(job['enabled'])

    def test_twenty_round_cap_and_interval_bounds(self):
        self.client.failures = 100
        self.arm()
        event = RecordingEvent()
        self.scheduler._run_attempts('default', event)
        self.assertEqual(self.client.calls, 20)
        self.assertEqual(self.scheduler.snapshots()['default']['state'], 'exhausted')
        self.assertEqual(len(event.intervals), 19)
        self.assertTrue(all(0.2 <= delay <= 1 for delay in event.intervals))
        self.assertGreater(len(set(event.intervals)), 1)

    def test_cancel_interrupts_retry_wait(self):
        self.client.failures = 100
        self.arm()
        self.scheduler._run_attempts('default', RecordingEvent(cancel_on_wait=True))
        self.assertEqual(self.client.calls, 1)
        self.assertEqual(self.scheduler.snapshots()['default']['state'], 'cancelled')

    def test_auth_failure_does_not_repeat_login_requests(self):
        self.client.failures, self.client.permanent = 20, True
        self.arm()
        self.scheduler._run_attempts('default', RecordingEvent())
        self.assertEqual(self.client.calls, 1)
        self.assertEqual(self.scheduler.snapshots()['default']['state'], 'failed')

    def test_two_accounts_enter_claim_concurrently(self):
        other = self.manager.add('账号 2')
        barrier = threading.Barrier(2)
        self.client.barrier = other.client.barrier = barrier
        self.scheduler.schedule(['default', other.id], {'max_attempts': 1}, immediate=True)
        self.scheduler.tick()
        with self.scheduler.lock:
            threads = list(self.scheduler.workers.values())
        for thread in threads:
            thread.join(3)
            self.assertFalse(thread.is_alive())
        jobs = self.scheduler.snapshots()
        self.assertEqual(jobs['default']['state'], 'success')
        self.assertEqual(jobs[other.id]['state'], 'success')
        self.assertEqual(self.client.calls, 1)
        self.assertEqual(other.client.calls, 1)

    def test_no_claim_before_official_clock_reaches_target(self):
        self.arm()
        self.client.current = NOW - timedelta(seconds=0.9)
        event = RecordingEvent(advance=lambda delay: setattr(self.client, 'current', self.client.current + timedelta(seconds=delay)))
        self.scheduler._worker('default', NOW, event)
        self.assertEqual(self.client.calls, 1)
        self.assertGreaterEqual(self.client.draw_times[0], NOW)
        self.assertAlmostEqual(sum(event.intervals), 0.9)

    def test_future_job_does_not_start_early(self):
        target = NOW + timedelta(days=7)
        self.scheduler.schedule(['default'], {'run_at': target.isoformat()})
        self.scheduler.tick()
        self.assertFalse(self.scheduler.workers)
        self.assertEqual(self.client.calls, 0)

    def test_weekly_success_arms_next_wednesday(self):
        self.scheduler.now = lambda: NOW - timedelta(seconds=1)
        self.scheduler.schedule(['default'], {'run_at': NOW.isoformat(), 'weekly': True})
        self.scheduler._run_attempts('default', RecordingEvent())
        job = self.scheduler.snapshots()['default']
        self.assertEqual(job['state'], 'success')
        self.assertTrue(job['enabled'])
        self.assertEqual(job['next_run_at'], (NOW + timedelta(days=7)).isoformat())

    def test_restart_does_not_replay_inflight_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'schedule.json'
            self.scheduler.storage = path
            self.arm()
            self.scheduler._update('default', state='running', attempts=2)
            restored = ClaimScheduler(self.manager, storage=path, now=lambda: NOW)
            self.assertEqual(restored.snapshots()['default']['state'], 'interrupted')
            self.assertFalse(restored.snapshots()['default']['enabled'])
            restored.tick()
            self.assertEqual(self.client.calls, 0)

    def test_waiting_schedule_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'schedule.json'
            self.scheduler.storage = path
            self.scheduler.schedule(['default'], {'run_at': (NOW + timedelta(days=7)).isoformat(), 'weekly': True})
            restored = ClaimScheduler(self.manager, storage=path, now=lambda: NOW)
            self.assertEqual(restored.snapshots()['default']['state'], 'waiting')
            self.assertTrue(restored.snapshots()['default']['enabled'])

    def test_running_job_cannot_be_overwritten(self):
        self.scheduler.workers['default'] = Mock()
        with self.assertRaises(ActivityError):
            self.arm()

    def test_policy_rejects_out_of_range_values(self):
        for data in [{'interval_min': 0.1}, {'interval_max': 2}, {'interval_min': 0.9, 'interval_max': 0.2},
                     {'max_attempts': 21}, {'max_attempts': True}, {'max_attempts': 1.2}, {'interval_min': 'nan'}]:
            with self.assertRaises(ActivityError):
                retry_policy(data)


class MultiAccountApiTests(unittest.TestCase):
    def setUp(self):
        self.manager = AccountManager(default_client=FakeClient(), client_factory=FakeClient)
        self.scheduler = ClaimScheduler(self.manager, now=lambda: NOW)
        self.app = create_app(manager=self.manager, scheduler=self.scheduler)
        self.app.testing = True
        self.http = self.app.test_client()
        self.http.get('/')
        with self.http.session_transaction() as state:
            self.headers = {'X-CSRF-Token': state['csrf']}

    def test_account_selection_does_not_touch_other_account(self):
        other = self.manager.add('账号 2')
        self.http.post('/api/claim', json={'account_id': other.id}, headers=self.headers)
        self.assertEqual(self.manager.get().client.calls, 0)
        self.assertEqual(other.client.calls, 1)

    def test_batch_refresh_uses_independent_workers(self):
        other = self.manager.add('账号 2')
        barrier = threading.Barrier(2)
        self.manager.get().client.barrier = other.client.barrier = barrier
        response = self.http.post('/api/accounts/batch', json={'account_ids': ['default', other.id],
            'operation': 'refresh'}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(all(r['ok'] for r in response.json['results']))

    def test_scheduling_api_preserves_every_account_and_policy(self):
        other = self.manager.add('账号 2')
        target = NOW + timedelta(days=7)
        response = self.http.post('/api/schedules', json={'account_ids': ['default', other.id],
            'run_at': target.isoformat(), 'interval_min': 0.2, 'interval_max': 1,
            'max_attempts': 20, 'weekly': True}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json['jobs']), 2)
        for job in response.json['jobs'].values():
            self.assertEqual(job['max_attempts'], 20)
            self.assertEqual(job['run_at'], target.isoformat())
        cancelled = self.http.post('/api/schedules/cancel', json={'account_ids': ['default', other.id]}, headers=self.headers)
        self.assertEqual(cancelled.status_code, 200)
        self.assertFalse(any(j['enabled'] for j in self.scheduler.snapshots().values()))

    def test_csrf_required_for_scheduler(self):
        self.assertEqual(self.http.post('/api/schedules', json={'account_ids': ['default']}).status_code, 403)

    def test_task_page_and_poll_preserve_live_tasks_without_activity_requests(self):
        self.scheduler.schedule(['default'], {'run_at': (NOW + timedelta(days=7)).isoformat(), 'weekly': True})
        before = self.scheduler.snapshots()
        self.client = self.manager.get().client
        self.client.identity = Mock(side_effect=AssertionError('task poll must stay local'))
        self.client.refresh = Mock(side_effect=AssertionError('task poll must stay local'))
        page = self.http.get('/static/tasks.html')
        self.assertEqual(page.status_code, 200)
        page.close()
        response = self.http.get('/api/accounts')
        self.assertEqual(response.json['jobs'], before)
        self.assertEqual(self.scheduler.snapshots(), before)
        self.assertEqual(self.client.calls, 0)

    def test_stop_selected_task_preserves_other_accounts_and_their_records(self):
        other = self.manager.add('账号 2')
        settings = {'run_at': (NOW + timedelta(days=7)).isoformat(), 'weekly': True}
        self.scheduler.schedule(['default', other.id], settings)
        untouched = self.scheduler.snapshots()[other.id]
        response = self.http.post('/api/schedules/cancel', json={'account_ids': ['default']}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.scheduler.snapshots()[other.id], untouched)
        self.assertFalse(self.scheduler.snapshots()['default']['enabled'])
        self.assertEqual(self.manager.get().client.calls + other.client.calls, 0)

    def test_rebook_only_replaces_selected_account_and_does_not_execute_it(self):
        other = self.manager.add('账号 2')
        self.scheduler.schedule(['default', other.id], {'run_at': (NOW + timedelta(days=7)).isoformat()})
        untouched = self.scheduler.snapshots()[other.id]
        response = self.http.post('/api/schedules', json={'account_ids': ['default'],
            'run_at': (NOW + timedelta(days=14)).isoformat(), 'weekly': True,
            'interval_min': 0.3, 'interval_max': 0.8, 'max_attempts': 12}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.scheduler.snapshots()[other.id], untouched)
        updated = self.scheduler.snapshots()['default']
        self.assertEqual(updated['run_at'], (NOW + timedelta(days=14)).isoformat())
        self.assertEqual(updated['max_attempts'], 12)
        self.assertEqual(self.manager.get().client.calls + other.client.calls, 0)

    def test_running_account_does_not_block_other_batch_refresh(self):
        other = self.manager.add('账号 2')
        self.scheduler.workers['default'] = Mock()
        response = self.http.post('/api/accounts/batch', json={'account_ids': ['default', other.id],
            'operation': 'refresh'}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        results = {r['account_id']: r for r in response.json['results']}
        self.assertFalse(results['default']['ok'])
        self.assertTrue(results[other.id]['ok'])

    def test_status_poll_does_not_compete_with_scheduled_requests(self):
        self.scheduler.workers['default'] = Mock()
        client = self.manager.get().client
        client.identity = Mock()
        response = self.http.get('/api/status?account_id=default')
        self.assertEqual(response.status_code, 200)
        client.identity.assert_not_called()
        response = self.http.post('/api/refresh', json={'account_id': 'default'}, headers=self.headers)
        self.assertEqual(response.status_code, 409)

    def test_cookie_jars_and_browser_profiles_are_separate(self):
        manager = AccountManager(default_client=ActivityClient())
        other = manager.add('账号 2')
        one = manager.get().client
        one.http.cookies.set('test', 'one', domain='wx.10086.cn')
        other.client.http.cookies.set('test', 'two', domain='wx.10086.cn')
        self.assertEqual(one.http.cookies.get('test'), 'one')
        self.assertEqual(other.client.http.cookies.get('test'), 'two')
        self.assertNotEqual(one.bridge.profile_dir, other.client.bridge.profile_dir)


if __name__ == '__main__':
    unittest.main()
