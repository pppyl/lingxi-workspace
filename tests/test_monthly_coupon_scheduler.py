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
        self.client_entries = []
        self.fail_after_submit = False
        self.auth_error = False
        self.server = {}
        self.scheduler = self.create()

    def create(self):
        parent = self
        class Client:
            def __init__(self, credentials, *, entry_url):
                self.credentials = credentials
                parent.client_entries.append(entry_url)
            def inspect(self):
                if parent.auth_error:
                    raise MonthlyCouponAuthError('共享账号需要重新登录', 'APP_LOGIN_REQUIRED', 401)
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
                    parent.assertTrue(saved['actions'][f"{self.credentials()['id']}:{action}:{period}"]['submitted'])
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

    def test_global_settings_apply_to_all_valid_accounts_including_new_arrivals(self):
        self.scheduler.settings({'enabled':True,'time':'06:30'})
        self.pool.append({'id':'b','status':'active'})
        self.scheduler=self.create()
        self.scheduler.tick();self.queue.drain()
        self.assertEqual(set(self.scheduler.data['accounts']), {'a','b'})
        self.assertTrue(all(r['auto_enabled'] and r['clock']=='06:30' for r in self.scheduler.data['accounts'].values()))
        self.assertTrue(self.scheduler.data['actions']['b:punch:20261029']['submitted'])

    def test_default_automation_needs_no_per_account_setup(self):
        self.now=self.now.replace(hour=9)
        self.scheduler.tick();self.queue.drain()
        self.assertEqual(sum(action=='direct' for action,_ in self.requests),1)
        self.assertEqual(sum(action=='punch' for action,_ in self.requests),1)
        self.scheduler.tick();self.queue.drain()
        self.assertEqual(sum(action=='punch' for action,_ in self.requests),1)

    def test_login_recovery_after_clock_catches_up_same_day(self):
        self.pool[0]['status']='login_required'
        self.now=self.now.replace(hour=9)
        self.scheduler.tick();self.queue.drain()
        self.assertFalse(self.requests)
        self.pool[0].update(status='active',credential_revision=2)
        self.scheduler.tick();self.queue.drain()
        self.assertEqual(sum(action=='punch' for action,_ in self.requests),1)

    def test_recovery_after_failed_attempt_does_not_wait_until_tomorrow(self):
        self.now=self.now.replace(hour=9)
        self.pool[0]['credential_revision']=1
        self.fail_after_submit=True
        self.scheduler.tick();self.queue.drain()
        self.pool[0]['status']='login_required'
        self.scheduler.tick()
        self.pool[0].update(status='active',credential_revision=2)
        self.fail_after_submit=False
        self.now+=timedelta(minutes=5)
        self.scheduler.tick();self.queue.drain()
        self.assertEqual(sum(action=='direct' for action,_ in self.requests),1)
        self.assertEqual(sum(action=='punch' for action,_ in self.requests),1)

    def test_relogin_clears_exhausted_auth_failures_and_catches_up_without_waiting(self):
        self.now=self.now.replace(hour=9)
        self.auth_error=True
        for _ in range(6):
            self.scheduler.tick();self.queue.drain()
            self.now+=timedelta(minutes=5)
        self.assertTrue(self.scheduler.status()['accounts'][0]['retry_exhausted'])
        self.pool[0]['status']='login_required'
        self.scheduler.tick()
        self.scheduler=self.create()
        self.pool[0].update(status='active',credential_revision=2)
        self.auth_error=False
        self.scheduler.tick();self.queue.drain()
        record=self.scheduler.data['accounts']['a']
        self.assertEqual(record['attempt_count'],1)
        self.assertEqual(record['completed_date'],self.now.strftime('%Y%m%d'))
        self.assertNotIn('query_error',record)
        self.assertEqual(sum(action=='direct' for action,_ in self.requests),1)
        self.assertEqual(sum(action=='punch' for action,_ in self.requests),1)
        self.scheduler.tick();self.queue.drain()
        self.assertEqual(sum(action=='punch' for action,_ in self.requests),1)

    def test_successful_execution_and_manual_recheck_do_not_display_failure_retries(self):
        self.scheduler.run(['a']);self.queue.drain()
        self.scheduler.run(['a']);self.queue.drain()
        value=self.scheduler.status()['accounts'][0]
        self.assertEqual(self.scheduler.data['accounts']['a']['attempt_count'],2)
        self.assertTrue(value['daily_completed'])
        self.assertEqual(value['retry_count'],0)
        self.assertFalse(value['retry_exhausted'])
        self.assertEqual(value['task_progress'],60)
        self.assertIn('累计打卡 1/3 天',value['message'])
        self.assertEqual(value['bonus_state'],'pending')

    def test_relogin_preserves_qualification_cooldown_and_submission_markers(self):
        self.now=self.now.replace(hour=9)
        self.scheduler._ensure_accounts(self.pool)
        record=self.scheduler.data['accounts']['a']
        record.update(query_state='failed',error_code='NOT_ELIGIBLE',retry_at=self.scheduler._next('08:05'))
        self.scheduler.data['actions']['a:direct:202610']={'submitted':True,'time':self.now.isoformat()}
        self.pool[0]['status']='login_required';self.scheduler.tick()
        self.pool[0].update(status='active',credential_revision=2)
        self.scheduler.tick();self.queue.drain()
        self.assertFalse(self.client_entries)
        self.assertEqual(self.scheduler.data['accounts']['a']['error_code'],'NOT_ELIGIBLE')
        self.assertTrue(self.scheduler.data['actions']['a:direct:202610']['submitted'])

    def test_unfinished_claim_keeps_checking_without_repeating_uncertain_submission(self):
        self.now=self.now.replace(hour=9)
        self.fail_after_submit=True
        self.scheduler.tick();self.queue.drain()
        self.fail_after_submit=False
        self.now+=timedelta(minutes=5)
        self.scheduler.tick();self.queue.drain()
        self.assertNotIn('completed_date',self.scheduler.data['accounts']['a'])
        self.assertEqual(datetime.fromisoformat(self.scheduler.data['accounts']['a']['next_run_at']),self.now+timedelta(minutes=5))
        self.now+=timedelta(minutes=5)
        self.scheduler.tick();self.queue.drain()
        self.assertEqual(sum(action=='direct' for action,_ in self.requests),1)
        self.server['202610']['direct']=True
        self.now+=timedelta(minutes=5)
        self.scheduler.tick();self.queue.drain()
        self.assertEqual(self.scheduler.data['accounts']['a']['completed_date'],'20261029')

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

    def test_failure_retries_are_bounded_and_survive_restart_or_credential_rotation(self):
        self.now=self.now.replace(hour=9);self.fail_after_submit=True
        for number in range(1,7):
            self.scheduler.tick();self.queue.drain()
            self.assertEqual(self.scheduler.data['accounts']['a']['attempt_count'],number)
            self.pool[0]['credential_revision']=number
            self.scheduler.tick();self.queue.drain()
            self.assertEqual(self.scheduler.data['accounts']['a']['attempt_count'],number)
            self.now+=timedelta(minutes=5)
        self.assertEqual(self.scheduler.data['accounts']['a']['next_run_at'],'2026-10-30T08:05:00+08:00')
        entries=len(self.client_entries)
        self.scheduler=self.create();self.scheduler.status();self.queue.drain();self.scheduler.tick();self.queue.drain()
        self.assertEqual(len(self.client_entries),entries)
        account=self.scheduler.status()['accounts'][0]
        self.assertEqual(account['retry_count'],5);self.assertTrue(account['retry_exhausted'])
        self.now+=timedelta(days=1);self.scheduler.tick();self.queue.drain()
        self.assertEqual(self.scheduler.data['accounts']['a']['attempt_count'],1)

    def test_custom_retry_interval_and_limit_apply_to_all_automatic_paths(self):
        self.now=self.now.replace(hour=9);self.fail_after_submit=True
        self.scheduler.settings({'enabled':True,'time':'08:05','retry_interval_minutes':2,'max_retries':2})
        self.scheduler.tick();self.queue.drain()
        self.assertEqual(self.scheduler.data['accounts']['a']['next_run_at'],'2026-10-29T09:02:00+08:00')
        entries=len(self.client_entries);self.now+=timedelta(minutes=1)
        self.pool[0]['credential_revision']=9
        self.scheduler.status();self.scheduler.tick();self.queue.drain()
        self.assertEqual(len(self.client_entries),entries)
        for _ in range(2):
            self.now+=timedelta(minutes=2);self.scheduler.tick();self.queue.drain()
        self.assertEqual(self.scheduler.data['accounts']['a']['attempt_count'],3)
        self.assertEqual(self.scheduler.status()['automation']['max_retries'],2)
        self.assertEqual(self.create().data['automation']['retry_interval_minutes'],2)

    def test_zero_retry_stops_after_initial_failure_and_manual_run_is_still_available(self):
        self.now=self.now.replace(hour=9);self.fail_after_submit=True
        self.scheduler.settings({'enabled':True,'time':'08:05','retry_interval_minutes':5,'max_retries':0})
        self.scheduler.tick();self.queue.drain();self.now+=timedelta(hours=1)
        self.scheduler.tick();self.scheduler.status();self.queue.drain()
        self.assertEqual(self.scheduler.data['accounts']['a']['attempt_count'],1)
        self.scheduler.run(['a']);self.queue.drain()
        self.assertEqual(self.scheduler.data['accounts']['a']['attempt_count'],2)

    def test_changing_retry_settings_recalculates_wait_without_resetting_attempt_count(self):
        self.now=self.now.replace(hour=9);self.fail_after_submit=True
        self.scheduler.settings({'enabled':True,'time':'08:05','max_retries':0})
        self.scheduler.tick();self.queue.drain();self.now+=timedelta(minutes=1)
        self.scheduler.settings({'enabled':True,'time':'08:05','retry_interval_minutes':1,'max_retries':2})
        self.scheduler.tick();self.queue.drain()
        self.assertEqual(self.scheduler.data['accounts']['a']['attempt_count'],2)
        self.assertEqual(self.scheduler.data['accounts']['a']['next_run_at'],'2026-10-29T09:02:00+08:00')

    def test_known_ineligible_account_still_has_only_one_automatic_attempt_per_day(self):
        parent=self
        class Ineligible:
            def __init__(self,provider,*,entry_url):parent.client_entries.append(entry_url)
            def execute(self,**kwargs):raise MonthlyCouponAuthError('不符合资格','NOT_ELIGIBLE',409)
        self.now=self.now.replace(hour=9);self.scheduler.client_factory=Ineligible
        self.scheduler.tick();self.queue.drain()
        for _ in range(5):
            self.now+=timedelta(minutes=5);self.pool[0]['credential_revision']=8
            self.scheduler.tick();self.scheduler.status();self.queue.drain()
        self.assertEqual(len(self.client_entries),1)
        self.assertEqual(self.scheduler.data['accounts']['a']['attempt_count'],1)

    def test_invalid_retry_settings_are_rejected_before_saving(self):
        for field,value in [('retry_interval_minutes',0),('retry_interval_minutes',1.5),
                            ('max_retries',-1),('max_retries',True),('max_retries',101)]:
            with self.subTest(field=field,value=value),self.assertRaises(MonthlyTaskError):
                self.scheduler.settings({'enabled':True,'time':'08:05',field:value})
        self.assertEqual(self.scheduler.data['automation']['max_retries'],5)

    def test_later_task_failure_keeps_the_already_confirmed_coupon_claim(self):
        parent=self
        class PartialClient:
            observation=None
            def __init__(self,provider,*,entry_url):pass
            def execute(self,*,before_submit,knowledge=None):
                before_submit('direct',parent.now.strftime('%Y%m'))
                self.observation={'month':parent.now.strftime('%Y%m'),'date':parent.now.strftime('%Y%m%d'),
                    'direct_state':'claimed','direct_prize':'本月话费券'}
                raise MonthlyCouponAuthError('后续落地页访问失败','TASK_VISIT_FAILED')
        self.now=self.now.replace(hour=9);self.scheduler.client_factory=PartialClient
        self.scheduler.run(['a']);self.queue.drain()
        record=self.storage.load_monthly_coupon_data()['accounts']['a']
        self.assertEqual(record['observation']['direct_state'],'claimed')
        self.assertIn('落地页',record['query_error'])
        self.assertEqual(record['next_run_at'],'2026-10-29T09:05:00+08:00')

    def test_page_settings_persist_and_reach_new_clients_without_erasing_submission_markers(self):
        entry='https://wap.gz.10086.cn/activityWeb/static/activity/grc/index.html?referChannels=EB060&ChannelPosition=NEW'
        self.scheduler.data['actions']['a:direct:202610']={'submitted':True,'time':self.now.isoformat()}
        self.scheduler.update_page({'entry_url':entry})
        self.assertFalse(self.queue.jobs)
        self.scheduler=self.create()
        self.assertEqual(self.scheduler.page()['entry_url'],entry)
        self.assertTrue(self.scheduler.data['actions']['a:direct:202610']['submitted'])
        self.scheduler.status();self.queue.drain()
        self.assertEqual(self.client_entries,[entry])
        self.assertEqual(self.requests,[])

    def test_changing_page_is_rejected_while_an_existing_worker_uses_it(self):
        old=self.scheduler.page()
        self.scheduler.status()
        with self.assertRaises(MonthlyTaskError) as raised:
            self.scheduler.update_page({'entry_url':old['entry_url']+'?ChannelPosition=NEW'})
        self.assertEqual(raised.exception.status,409)
        self.assertEqual(self.scheduler.page(),old)
        self.queue.drain()

    def test_manual_execution_waits_for_active_status_query(self):
        self.scheduler.status()
        self.scheduler.run(['a'])
        self.queue.drain()
        self.assertEqual(sum(action=='direct' for action,_ in self.requests),1)
        self.assertEqual(sum(action=='punch' for action,_ in self.requests),1)

    def test_later_read_does_not_erase_confirmed_monthly_receipt(self):
        now=self.now
        class ReadOnlyClient:
            def __init__(self, provider, *, entry_url):pass
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

    def test_legacy_limit_label_is_migrated_without_inventing_a_prize_or_replaying(self):
        self.scheduler.run(['a']);self.queue.drain()
        changed=self.storage.load_monthly_coupon_data()
        observed=changed['accounts']['a']['observation']
        observed.update(direct_state='claimed',direct_message='本月已领取',direct_prize='')
        self.storage.save_monthly_coupon_data(changed)
        self.scheduler=self.create()
        self.scheduler.tick();self.queue.drain()
        row=self.scheduler.status()['accounts'][0]
        self.assertEqual(row['direct_state'],'participated')
        self.assertEqual(row['direct_prize'],'')
        self.assertIn('发券结果待核实',row['direct_message'])
        self.assertEqual(sum(action=='direct' for action,_ in self.requests),1)

    def test_legacy_real_prize_is_not_downgraded_when_name_is_present(self):
        self.scheduler.run(['a']);self.queue.drain()
        changed=self.storage.load_monthly_coupon_data()
        changed['accounts']['a']['observation'].update(
            direct_state='claimed',direct_message='本月已领取',direct_prize='4元话费满减券')
        self.storage.save_monthly_coupon_data(changed)
        row=self.create().status()['accounts'][0]
        self.assertEqual(row['direct_state'],'claimed')
        self.assertEqual(row['direct_prize'],'4元话费满减券')

    def test_verified_used_receipt_survives_refresh_and_restart_but_not_month_rollover(self):
        self.scheduler.run(['a']);self.queue.drain()
        changed=self.storage.load_monthly_coupon_data()
        receipt={'source':'wallet_history','status':'used','used_at':'2026-10-29T06:30:09+08:00'}
        changed['accounts']['a']['observation'].update(direct_prize='4元话费满减券',direct_receipt=receipt)
        self.storage.save_monthly_coupon_data(changed)
        self.scheduler=self.create()
        self.scheduler.status();self.queue.drain()
        row=self.scheduler.status()['accounts'][0]
        self.assertEqual(row['direct_receipt'],receipt)
        self.assertEqual(row['direct_prize'],'4元话费满减券')
        self.scheduler=self.create()
        self.assertEqual(self.scheduler.status()['accounts'][0]['direct_receipt'],receipt)
        self.queue.drain()
        self.now=datetime(2026,11,1,7,tzinfo=SHANGHAI)
        self.assertEqual(self.scheduler.status()['accounts'][0]['direct_receipt'],{})


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

    def test_page_settings_are_visible_and_can_be_saved_in_the_existing_page(self):
        entry='https://wap.gz.10086.cn/activityWeb/static/activity/grc/index.html?referChannels=EB060&ChannelPosition=TEST'
        response=self.client.post('/api/monthly-coupon/page',json={'entry_url':entry})
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.get_json()['page']['entry_url'],entry)
        status=self.client.get('/api/monthly-coupon/status').get_json()
        self.assertEqual(status['page']['entry_url'],entry)

    def test_invalid_page_is_rejected_without_replacing_working_address(self):
        old=self.client.get('/api/monthly-coupon/status').get_json()['page']
        for entry in ['https://example.com/activityWeb/static/index.html',
                      'http://wap.gz.10086.cn/activityWeb/static/index.html',
                      'https://wap.gz.10086.cn/activityWeb/static/../private.html',
                      'https://wap.gz.10086.cn/activityWeb/static/index.html\r\nX-Test: 1']:
            with self.subTest(entry=entry):
                response=self.client.post('/api/monthly-coupon/page',json={'entry_url':entry})
                self.assertEqual(response.status_code,400)
        self.assertEqual(self.client.get('/api/monthly-coupon/status').get_json()['page'],old)


if __name__=='__main__':unittest.main()
