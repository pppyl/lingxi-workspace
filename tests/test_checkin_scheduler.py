from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest

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

            def flash(self, product, *, before_submit):
                before_submit()
                outer.calls.append(product)
                return {'state': 'success', 'message': '已领取'}
        return CheckinScheduler(lambda key: key, lambda: deepcopy(self.accounts), storage=self.storage,
                                client_factory=Client, now=lambda: self.time, executor=self.executor)

    def configure(self):
        self.scheduler.save({'login_ids': ['a'], 'signin_enabled': True, 'signin_time': '08:00',
                             'flash_enabled': True, 'product_id': FLASH_PRODUCT})

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

    def test_status_lists_unconfigured_and_expired_without_fabricated_zero(self):
        status = self.scheduler.status()
        self.assertEqual(len(status['accounts']), 2)
        self.assertEqual(status['tasks'], [])
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
        self.assertFalse(next(iter(self.scheduler.data['tasks'].values()))['enabled'])


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


if __name__ == '__main__':
    unittest.main()
