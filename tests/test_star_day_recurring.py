from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest

from login_storage import DatabaseLoginStorage
from recharge.cmcc import SHANGHAI
from star_day_client import StarDayError
from star_day_scheduler import StarDayScheduler
from tests.test_star_day import Queue


class RecurringTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.storage=DatabaseLoginStorage(Path(self.temp.name)/'accounts.sqlite3',protect=lambda b:b,unprotect=lambda b:b)
        self.now=datetime(2026,11,1,9,59,40,tzinfo=SHANGHAI)
        self.pool=[{'id':i,'status':'active','name':i,'phone_masked':'138****0001'} for i in ('a','b')]
        self.queue=Queue();self.sent=[];self.claimed=set();self.failed_accounts=set();self.unknown=set()
        self.auth_failures={};self.ineligible=set();self.reads=[];self.calibrations=0;self.stop_during=None;self.gift_targets=[];self.preparations=[]
        parent=self
        class Client:
            def __init__(self,credentials):self.id=credentials()['id']
            def inspect(self):
                parent.reads.append(self.id)
                if parent.auth_failures.get(self.id,0):
                    parent.auth_failures[self.id]-=1
                    raise StarDayError('活动登录失败','AUTH_FAILED')
                if self.id in parent.ineligible:
                    return {'date':parent.now.strftime('%Y%m%d'),'eligible':False,'today_state':'ineligible',
                            'tier':'7','state':'ineligible','message':'官方确认不符合活动资格'}
                return {'date':parent.now.strftime('%Y%m%d'),'eligible':True,'today_state':'signed',
                        'month_claimed':(self.id,parent.now.strftime('%Y%m')) in parent.claimed,
                        'state':'ready','message':'官方状态'}
            def signin(self,*,before_submit):return self.inspect()
            def products(self):return [{'id':'123','phase':1,'name':'会员权益','stock':100,'transferable':True}]
            def prepare(self):
                parent.preparations.append(self.id)
                return {'state':'ready','message':'抢购凭据已准备'}
            def gift_options(self):return {'can_send_other':False,'family':[{'id':'f'*64,'name':'亲友','phone_masked':'138****0099'}]}
            def calibrate(self):
                parent.calibrations+=1
                return {'state':'ready','at':parent.now.isoformat(),'offset_ms':0,'uncertainty_ms':10}
            def rush(self,product_id,phase,*,target_at,burst,before_submit,cancelled,**kwargs):
                if cancelled():return {'state':'stopped','message':'已停止','uncertain':False}
                if 'recipient' in kwargs:parent.gift_targets.append(deepcopy(kwargs['recipient']))
                if not before_submit('benefit',target_at.strftime('%Y%m')):
                    return {'state':'unknown','message':'未确认','uncertain':True}
                parent.sent.append((self.id,target_at.date()))
                if self.id in parent.failed_accounts:return {'state':'skipped','message':'官方库存不足','uncertain':False}
                if self.id in parent.unknown:return {'state':'failed','message':'结果未知','uncertain':True}
                parent.claimed.add((self.id,target_at.strftime('%Y%m')))
                if parent.stop_during:parent.scheduler.stop(parent.stop_during)
                return {'state':'success','month_claimed':True,'date':target_at.strftime('%Y%m%d'),
                        'benefit_name':'会员权益','benefit_id':product_id,'message':'成功'}
        self.factory=Client
        self.scheduler=self.create()
        self.scheduler.settings({'signin_enabled':False,'signin_time':'00:01'})

    def create(self):
        return StarDayScheduler(lambda i:{'id':i},lambda:deepcopy(self.pool),storage=self.storage,
            client_factory=self.factory,executor=self.queue,now=lambda:self.now)

    def job(self,ids=None):
        return self.scheduler.schedule({'product_id':'123','phase':1,'login_ids':ids or ['a','b']})['id']

    def cycle(self):
        for _ in range(3):self.scheduler.tick();self.queue.drain()

    def test_daily_at_ten_retries_only_unsuccessful_accounts_then_resumes_next_month(self):
        self.failed_accounts={'b'};job=self.job();self.cycle()
        self.assertCountEqual(self.sent,[('a',self.now.date()),('b',self.now.date())])
        self.assertEqual(self.scheduler.data['jobs'][job]['at'],'2026-11-02T10:00:00+08:00')
        self.now+=timedelta(days=1);self.failed_accounts.clear();self.cycle()
        self.assertCountEqual([i for i,d in self.sent],['a','b','b'])
        self.assertEqual(self.scheduler.data['jobs'][job]['state'],'monthly_completed')
        self.assertEqual(self.scheduler.data['jobs'][job]['at'],'2026-12-01T10:00:00+08:00')
        self.now+=timedelta(days=5);self.cycle();self.assertEqual(len(self.sent),3)
        self.now=datetime(2026,12,1,9,59,40,tzinfo=SHANGHAI);self.cycle()
        self.assertEqual(len(self.sent),5)
        self.assertEqual(self.scheduler.data['jobs'][job]['at'],'2027-01-01T10:00:00+08:00')

    def test_definite_failures_continue_across_restart_without_resetting_to_one_shot(self):
        self.failed_accounts={'a','b'};job=self.job();self.cycle()
        self.scheduler=self.create();self.now+=timedelta(days=1);self.cycle()
        self.assertEqual(len(self.sent),4);self.assertTrue(self.scheduler.data['jobs'][job]['enabled'])
        self.assertEqual(self.scheduler.data['jobs'][job]['at'],'2026-11-03T10:00:00+08:00')

    def test_late_start_schedules_next_ten_instead_of_becoming_missed_forever(self):
        job=self.job();self.now=self.now.replace(hour=11);self.cycle()
        self.assertFalse(self.sent)
        self.assertEqual(self.scheduler.data['jobs'][job]['at'],'2026-11-02T10:00:00+08:00')
        self.now+=timedelta(days=1);self.now=self.now.replace(hour=9,minute=59,second=40);self.cycle()
        self.assertEqual(len(self.sent),2)

    def test_uncertain_submission_is_queried_next_day_before_retry(self):
        self.unknown={'b'};self.job();self.cycle()
        self.assertIn('b:benefit:202611',self.scheduler.data['actions'])
        self.unknown.clear();self.now+=timedelta(days=1);self.cycle()
        self.assertIn('b',self.reads);self.assertCountEqual([i for i,d in self.sent],['a','b','b'])

    def test_uncertain_submission_officially_confirmed_does_not_submit_again(self):
        self.unknown={'b'};self.job();self.cycle()
        self.claimed.add(('b','202611'));self.now+=timedelta(days=1);self.cycle()
        self.assertEqual(len(self.sent),2)
        self.assertTrue(self.scheduler.data['actions']['b:benefit:202611']['confirmed'])

    def test_stop_removes_database_record_and_cancels_already_queued_workers(self):
        job=self.job();self.scheduler.calibrate();self.scheduler.tick()
        self.assertTrue(self.queue.jobs);self.scheduler.stop(job);self.queue.drain();self.scheduler.tick()
        self.assertFalse(self.sent);self.assertNotIn(job,self.scheduler.data['jobs'])
        self.assertNotIn(job,self.storage.load_star_day_data()['jobs'])
        self.assertFalse(self.scheduler.running)

    def test_stop_while_request_is_returning_never_recreates_task_and_preserves_monthly_success(self):
        job=self.job(['a']);self.stop_during=job;self.cycle()
        self.assertNotIn(job,self.scheduler.data['jobs'])
        self.assertTrue(self.scheduler.data['actions']['a:benefit:202611']['confirmed'])
        self.assertFalse(self.scheduler.running)

    def test_old_stopped_rows_are_removed_on_load(self):
        job=self.job();data=deepcopy(self.scheduler.data);data['jobs'][job].update(state='stopped',enabled=False)
        self.storage.save_star_day_data(data);restored=self.create()
        self.assertNotIn(job,restored.data['jobs']);self.assertNotIn(job,self.storage.load_star_day_data()['jobs'])

    def test_two_tasks_share_calibration_and_monthly_deduplication(self):
        self.job(['a']);self.job(['a','b']);self.cycle()
        self.assertCountEqual([i for i,d in self.sent],['a','b'])
        self.assertEqual(self.calibrations,1)

    def test_daily_rush_gift_uses_confirmed_recipient_for_each_day_until_monthly_success(self):
        self.failed_accounts={'a'}
        job=self.scheduler.schedule({'product_id':'123','phase':1,'login_ids':['a'],
                                     'recipient':{'kind':'family','id':'f'*64}})['id']
        self.cycle();self.now+=timedelta(days=1);self.failed_accounts.clear();self.cycle()
        self.assertEqual(self.gift_targets,[{'kind':'family','id':'f'*64}]*2)
        self.assertEqual(self.scheduler.data['jobs'][job]['state'],'monthly_completed')
        self.assertEqual(self.scheduler.data['jobs'][job]['recipient_label'],'亲友 · 138****0099')

    def test_campaign_auth_failure_stops_after_one_attempt_without_primary_refresh(self):
        self.auth_failures={'a':1};self.scheduler.run(['a'],inspect=True);self.queue.drain()
        self.assertEqual(self.reads,['a'])
        self.assertEqual(self.scheduler.data['accounts']['a']['query_state'],'auth_failed')
        self.assertEqual(self.scheduler.data['accounts']['a']['retry_at'],'2026-11-02T09:30:00+08:00')
        self.assertFalse(self.sent)

    def test_persistent_campaign_auth_failure_keeps_confirmed_state_and_is_not_ineligible(self):
        self.auth_failures={'a':4}
        self.scheduler.data['accounts']['a']={'observation':{'date':'20261101','today_state':'signed','month_claimed':True,
            'benefit_id':'123','benefit_name':'会员权益'},'history':[]}
        self.scheduler.run(['a'],inspect=True);self.queue.drain()
        record=self.scheduler.data['accounts']['a']
        self.assertEqual(self.reads,['a']);self.assertEqual(record['query_state'],'auth_failed')
        self.assertEqual(record['error_code'],'AUTH_FAILED')
        self.assertTrue(record['observation']['month_claimed'])
        self.assertEqual(record['observation']['benefit_name'],'会员权益')
        self.assertFalse(self.sent)

    def test_ineligible_account_is_checked_once_a_day_despite_polling_and_rotation(self):
        self.pool=self.pool[:1];self.ineligible={'a'}
        self.scheduler.settings({'signin_enabled':True,'signin_time':'00:01'})
        self.cycle();self.assertEqual(self.reads,['a'])
        for _ in range(8):
            self.now+=timedelta(minutes=10)
            self.pool[0]['credential_revision']=self.pool[0].get('credential_revision',0)+1
            self.scheduler.status();self.cycle()
        self.assertEqual(self.reads,['a']);self.assertFalse(self.sent)
        account=self.scheduler.status()['accounts'][0]
        self.assertEqual(account['retry_reason'],'ineligible')
        self.assertEqual(account['next_run_at'],'2026-11-02T09:30:00+08:00')
        self.scheduler=self.create();self.scheduler.status();self.cycle()
        self.assertEqual(self.reads,['a'])
        self.now=datetime(2026,11,2,0,0,tzinfo=SHANGHAI)
        self.scheduler.status();self.cycle();self.assertEqual(self.reads,['a'])
        self.now=self.now.replace(hour=9,minute=30);self.cycle()
        self.assertEqual(self.reads,['a','a'])

    def test_auth_failure_cooldown_survives_restart_and_user_can_explicitly_recheck(self):
        self.pool=self.pool[:1];self.auth_failures={'a':2}
        self.scheduler.run(['a'],inspect=True);self.queue.drain()
        self.now+=timedelta(minutes=30);self.scheduler=self.create()
        for _ in range(5):self.scheduler.status();self.cycle()
        self.assertEqual(self.reads,['a'])
        self.scheduler.run(['a'],inspect=True);self.queue.drain()
        self.assertEqual(self.reads,['a','a'])
        self.scheduler.run(['a'],inspect=True);self.queue.drain()
        self.assertEqual(self.scheduler.data['accounts']['a']['query_state'],'ready')
        self.assertNotIn('retry_at',self.scheduler.data['accounts']['a'])

    def test_deferred_account_does_not_block_healthy_accounts_or_change_primary_login(self):
        self.now=self.now.replace(hour=7,minute=0)
        self.auth_failures={'a':4}
        self.scheduler.settings({'signin_enabled':True,'signin_time':'00:01'})
        self.cycle()
        self.assertEqual(self.scheduler.data['accounts']['a']['query_state'],'auth_failed')
        self.assertEqual(self.scheduler.data['accounts']['b']['observation']['today_state'],'signed')
        self.assertTrue(all(a['status']=='active' for a in self.pool))
        self.now+=timedelta(minutes=30);self.scheduler.status();self.queue.drain();self.cycle()
        self.assertEqual(self.reads.count('a'),1)

    def test_deferred_accounts_skip_rush_and_calibration_without_upstream_calls(self):
        self.ineligible={'a','b'}
        self.scheduler.run(inspect=True);self.queue.drain();self.job();self.cycle()
        self.assertCountEqual(self.reads,['a','b'])
        self.assertFalse(self.sent);self.assertEqual(self.calibrations,0)
        job=next(iter(self.scheduler.data['jobs'].values()))
        self.assertEqual(job['at'],'2026-11-02T10:00:00+08:00')
        self.assertEqual({r['state'] for r in job['results'].values()},{'skipped'})

    def test_next_day_rush_rechecks_previous_ineligible_account_before_dispatch(self):
        self.pool=self.pool[:1];self.ineligible={'a'}
        self.scheduler.run(inspect=True);self.queue.drain();self.job(['a']);self.cycle()
        self.now+=timedelta(days=1);self.cycle()
        self.assertEqual(self.reads,['a','a']);self.assertFalse(self.sent)
        self.ineligible.clear();self.now+=timedelta(days=1);self.cycle()
        self.assertEqual(self.reads,['a','a','a']);self.assertEqual(len(self.sent),1)

    def test_daily_qualification_recheck_does_not_hold_up_healthy_rush_workers(self):
        self.ineligible={'a'}
        self.scheduler.run(['a'],inspect=True);self.queue.drain()
        self.now+=timedelta(days=1);self.job()
        self.scheduler.calibrate();self.scheduler.tick()
        self.assertEqual(len(self.queue.jobs),2)
        self.queue.drain();self.cycle()
        self.assertEqual(self.reads.count('a'),2)
        self.assertEqual([ident for ident,_ in self.sent],['b'])

    def test_old_ineligible_records_are_throttled_once_on_load(self):
        data=deepcopy(self.scheduler.data)
        data['accounts']['a']={'observation':{'date':'20261101','eligible':False,'today_state':'ineligible'},
                               'query_state':'auth_failed','history':[]}
        self.storage.save_star_day_data(data);self.scheduler=self.create()
        retry=self.scheduler.data['accounts']['a']['retry_at']
        self.now+=timedelta(minutes=30);self.scheduler=self.create()
        self.assertEqual(self.scheduler.data['accounts']['a']['retry_at'],retry)
        self.scheduler.status();self.queue.drain()
        self.assertNotIn('a',self.reads)

    def test_0930_preparation_is_once_per_selected_account_across_tasks_and_restart(self):
        self.now=self.now.replace(hour=9,minute=29,second=50)
        self.job(['a']);self.job(['a','b'])
        self.scheduler.tick();self.queue.drain()
        self.assertFalse(self.preparations)
        self.now=self.now.replace(minute=30,second=0)
        self.scheduler.tick();self.queue.drain()
        self.assertCountEqual(self.preparations,['a','b'])
        for _ in range(3):self.scheduler.tick();self.queue.drain()
        self.scheduler=self.create();self.scheduler.tick();self.queue.drain()
        self.assertCountEqual(self.preparations,['a','b'])

    def test_preparation_skips_claimed_expired_and_checks_ineligible_only_once(self):
        self.now=self.now.replace(hour=9,minute=30,second=0)
        self.pool.append({'id':'c','status':'login_required'})
        self.job(['a','b'])
        self.claimed.add(('a','202611'))
        self.scheduler.data['accounts']['a']={'observation':{'date':'20261101','month_claimed':True},'history':[]}
        self.scheduler.data['accounts']['b']={'observation':{'eligible':False},'history':[]}
        self.scheduler.tick();self.queue.drain()
        self.assertEqual(self.preparations,['b'])

    def test_preparation_time_is_configurable_by_minute_from_eight_to_ten(self):
        for clock in ('08:00','08:47','09:59','10:00'):
            self.scheduler.settings({'signin_enabled':True,'signin_time':'00:01','session_prepare_time':clock})
            self.assertEqual(self.scheduler.data['settings']['session_prepare_time'],clock)
        for clock in ('07:59','10:01','09:30:10','invalid'):
            with self.assertRaises(StarDayError):
                self.scheduler.settings({'signin_enabled':True,'signin_time':'00:01','session_prepare_time':clock})

    def test_configured_preparation_runs_even_without_a_rush_job_for_daily_signin(self):
        self.now=self.now.replace(hour=8,minute=46,second=0)
        self.scheduler.settings({'signin_enabled':True,'signin_time':'00:01','session_prepare_time':'08:47'})
        self.cycle();self.assertFalse(self.preparations)
        self.now+=timedelta(minutes=1);self.cycle()
        self.assertCountEqual(self.preparations,['a','b'])
        self.now+=timedelta(minutes=10);self.scheduler=self.create();self.cycle()
        self.assertCountEqual(self.preparations,['a','b'])


    def test_late_start_never_catches_up_background_login_outside_configured_minute(self):
        self.now=self.now.replace(hour=9,minute=31,second=0)
        self.scheduler.settings({'signin_enabled':True,'signin_time':'00:01','session_prepare_time':'09:30'})
        self.cycle();self.assertFalse(self.preparations)

    def test_rate_limit_is_persisted_until_next_preparation_and_status_is_local_only(self):
        self.now=self.now.replace(hour=7,minute=0)
        def limited(*args,**kwargs):raise StarDayError('请求频繁','-800')
        self.factory.inspect=limited
        self.scheduler.run(['a'],inspect=True);self.queue.drain()
        record=self.scheduler.data['accounts']['a']
        self.assertEqual(record['retry_reason'],'rate_limited')
        self.assertEqual(record['retry_at'],'2026-11-02T09:30:00+08:00')
        self.now=self.now.replace(hour=9,minute=30)
        self.cycle();self.scheduler=self.create()
        for _ in range(8):self.scheduler.status()
        self.assertFalse(self.queue.jobs)
        self.assertNotIn('a',self.preparations)


if __name__=='__main__':unittest.main()
