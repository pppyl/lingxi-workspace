from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest

from login_storage import DatabaseLoginStorage
from monthly_coupon_auth import MonthlyCouponAuthError
from monthly_coupon_scheduler import MonthlyCouponScheduler, MonthlyTaskError

from recharge.cmcc import SHANGHAI


class Queue:
    def __init__(self):
        self.jobs = []
    def submit(self, function, *args):
        self.jobs.append((function, args))
    def drain(self):
        while self.jobs:
            function, args = self.jobs.pop(0)
            function(*args)
    def shutdown(self, **kwargs):
        pass


class MonthlySchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.storage = DatabaseLoginStorage(Path(self.temp.name)/'account.sqlite3',
            protect=lambda v:b'encrypted:'+v, unprotect=lambda v:v.removeprefix(b'encrypted:'))
        self.now = datetime(2026, 10, 29, 7, tzinfo=SHANGHAI)
        self.pool = [{'id':'a','name':'test','phone_masked':'199****0001','status':'active'}]
        self.queue = Queue()
        self.requests = []
        self.fail_after_submit = False
        self.server = {}
        self.scheduler = self.create()

    def create(self):
        parent = self
        class Client:
            def __init__(self, credentials):
                self.credentials = credentials
            def inspect(self):
                month = parent.now.strftime('%Y%m')
                state = parent.server.setdefault(month, {'direct':False,'bonus':False,'punches':set(),'tasks':set()})
                return {'month':month,'date':parent.now.strftime('%Y%m%d'),'eligible':True,
                    'direct_state':'claimed' if state['direct'] else 'available',
                    'bonus_state':'claimed' if state['bonus'] else 'pending',
                    'punch_days':len(state['punches']),'punched_today':parent.now.strftime('%Y%m%d') in state['punches'],
                    'task_progress':20*len(state['tasks'])+(40 if len(state['punches'])>=3 else 0),
                    'tasks':[{'id':t,'name':t,'done':int(t in state['tasks']),'total':1} for t in ['bill','signin','age']],
                    'message':'official result'}
            def execute(self, *, before_submit, knowledge=None):
                snapshot = self.inspect()
                month, date = snapshot['month'], snapshot['date']
                server = parent.server[month]
                for action, period in [('direct',month),('bill',month),('signin',month),('age',month),('punch',date)]:
                    if not before_submit(action,period):continue
                    # Every upstream write sees a committed marker before it starts.
                    saved=parent.storage.load_monthly_coupon_data()
                    parent.assertTrue(saved['actions'][f'a:{action}:{period}']['submitted'])
                    parent.requests.append((action,period))
                    if parent.fail_after_submit:raise MonthlyCouponAuthError('request interrupted')
                    if action=='direct':server['direct']=True
                    elif action=='punch':server['punches'].add(date)
                    else:server['tasks'].add(action)
                if len(server['punches'])>=3 and before_submit('bonus',month):
                    parent.requests.append(('bonus',month));server['bonus']=True
                return self.inspect()
        return MonthlyCouponScheduler(lambda key:{'id':key},lambda:deepcopy(self.pool),Client,
            storage=self.storage, executor=self.queue, now=lambda:self.now)

    def enable(self):
        self.scheduler.settings({'login_ids':['a'],'enabled':True,'time':'08:05'})

    def test_direct_monthly_and_daily_punch_complete_bonus_on_third_day(self):
        for _ in range(3):
            self.scheduler.run(['a']);self.queue.drain()
            self.scheduler.run(['a']);self.queue.drain()
            self.now+=timedelta(days=1)
        self.assertEqual(sum(a=='direct' for a,_ in self.requests),1)
        self.assertEqual(sum(a=='punch' for a,_ in self.requests),3)
        self.assertEqual(sum(a=='bonus' for a,_ in self.requests),1)
        self.assertTrue(self.server['202610']['bonus'])

    def test_restart_does_not_repeat_submitted_action(self):
        self.fail_after_submit=True
        self.scheduler.run(['a']);self.queue.drain()
        self.scheduler=self.create()
        self.fail_after_submit=False
        self.scheduler.run(['a']);self.queue.drain()
        self.assertEqual(sum(a=='direct' for a,_ in self.requests),1)

    def test_new_month_has_separate_claims_and_clears_stale_view(self):
        self.scheduler.run(['a']);self.queue.drain()
        self.now=datetime(2026,11,1,7,tzinfo=SHANGHAI)
        status=self.scheduler.status()
        self.assertEqual(status['accounts'][0]['direct_state'],'unknown')
        self.assertIsNone(status['accounts'][0]['punch_days'])
        self.queue.drain()
        self.scheduler.run(['a']);self.queue.drain()
        self.assertEqual([period for action,period in self.requests if action=='direct'],['202610','202611'])

    def test_late_start_catches_up_and_future_start_only_queries(self):
        self.enable();self.scheduler.check_startup();self.queue.drain()
        self.assertEqual(self.requests,[])
        self.now=self.now.replace(hour=10)
        self.scheduler=self.create();self.scheduler.check_startup();self.queue.drain()
        self.assertEqual(sum(a=='punch' for a,_ in self.requests),1)
        self.scheduler=self.create();self.scheduler.check_startup();self.queue.drain()
        self.assertEqual(sum(a=='punch' for a,_ in self.requests),1)

    def test_daily_clock_and_disable_are_persisted(self):
        self.enable();self.scheduler.tick();self.assertFalse(self.queue.jobs)
        self.now=self.now.replace(hour=8,minute=5)
        self.scheduler.tick();self.queue.drain()
        self.assertEqual(sum(a=='punch' for a,_ in self.requests),1)
        self.scheduler.settings({'login_ids':['a'],'enabled':False,'time':'08:05'})
        self.scheduler=self.create();self.now+=timedelta(days=1)
        self.scheduler.tick();self.queue.drain()
        self.assertEqual(sum(a=='punch' for a,_ in self.requests),1)

    def test_unknown_counts_remain_unknown_until_query(self):
        status=self.scheduler.status()
        self.assertIsNone(status['accounts'][0]['punch_days'])
        self.assertIsNone(status['accounts'][0]['task_progress'])
        self.queue.drain()
        self.assertEqual(self.scheduler.status()['accounts'][0]['punch_days'],0)

    def test_cannot_remove_account_during_active_job(self):
        self.scheduler.run(['a'])
        with self.assertRaises(MonthlyTaskError):self.scheduler.forget('a')
        self.queue.drain();self.scheduler.forget('a')

    def test_invalid_requests_do_not_change_settings(self):
        for payload in [{'login_ids':['missing'],'enabled':True,'time':'08:05'},
                        {'login_ids':['a'],'enabled':True,'time':'25:00'},
                        {'login_ids':['a'],'enabled':'yes','time':'08:05'}]:
            with self.assertRaises(MonthlyTaskError):self.scheduler.settings(payload)
        self.assertEqual(self.scheduler.data['accounts'],{})

    def test_manual_execution_waits_for_active_status_query(self):
        self.scheduler.status()
        self.scheduler.run(['a'])
        self.queue.drain()
        self.assertEqual(sum(action=='direct' for action,_ in self.requests),1)
        self.assertEqual(sum(action=='punch' for action,_ in self.requests),1)

    def test_later_read_does_not_erase_confirmed_monthly_receipt(self):
        now=self.now
        class ReadOnlyClient:
            def __init__(self, provider):pass
            def inspect(self):
                return {'month':now.strftime('%Y%m'),'date':now.strftime('%Y%m%d'),
                        'direct_state':'unknown','bonus_state':'unknown','punched_today':None}
        self.scheduler.client_factory=ReadOnlyClient
        self.scheduler.data['accounts']['a']={'auto_enabled':False,'clock':'08:05','next_run_at':None,
            'observation':{'month':'202610','date':'20261029','direct_state':'claimed','direct_prize':'confirmed coupon','punched_today':True}}
        self.scheduler.status();self.queue.drain()
        value=self.scheduler.status()['accounts'][0]
        self.assertEqual(value['direct_state'],'claimed')
        self.assertEqual(value['direct_prize'],'confirmed coupon')
        self.assertTrue(value['punched_today'])


class MonthlyApiTests(unittest.TestCase):
    def setUp(self):
        from app import create_app
        self.app=create_app()
        self.client=self.app.test_client()
        self.addCleanup(self.app.extensions['monthly_coupon_scheduler'].shutdown)
        self.addCleanup(self.app.extensions['checkin_scheduler'].shutdown)

    def test_page_and_empty_status(self):
        self.assertEqual(self.client.get('/monthly-coupon').status_code,200)
        status=self.client.get('/api/monthly-coupon/status').get_json()
        self.assertEqual(status['accounts'],[])
        self.assertNotIn('cookie',str(status).lower())

    def test_unknown_accounts_and_invalid_clock_are_rejected(self):
        self.assertEqual(self.client.post('/api/monthly-coupon/run',json={'login_ids':['missing']}).status_code,400)
        self.assertEqual(self.client.post('/api/monthly-coupon/settings',json={'login_ids':[], 'enabled':True,'time':'25:00'}).status_code,400)


if __name__=='__main__':unittest.main()
