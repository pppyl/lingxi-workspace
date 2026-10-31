from copy import deepcopy
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
from threading import RLock
import unittest
from unittest.mock import Mock, patch

from recharge.accounts import AccountManager
from recharge.app import create_app
from recharge.cmcc import ActivityError, SHANGHAI
from recharge.scheduler import ClaimScheduler, LOGIN_REQUIRED_MESSAGE, LOGIN_RECOVERED_MESSAGE


class Client:
    def __init__(self, owner, profile_dir=None):
        self.owner, self.profile_dir = owner, profile_dir
        self.mode, self.app_material = 'none', None
        self.operation_lock = RLock()
        self.clock_anchor, self.uncertain, self.total_submissions = None, False, 0
        self.bridge=Mock()
        self.refreshes=self.writes=self.renewals=0
        self.done=False
        self.failures=0
    def server_now(self):return self.owner.now
    def renew_app_session(self):self.mode='app';self.renewals+=1
    def refresh(self,**kwargs):
        self.refreshes+=1
        self.uncertain=False
        return {'authenticated':True,'device_ready':True,'start_hour':8,'server_time':self.owner.now.isoformat(),
                'rd_status':4 if self.done else 1,'prize':{'name':'fixture'} if self.done else None}
    def claim(self,**kwargs):
        if self.done:raise ActivityError('本场已有结果','ROUND_COMPLETED',409,retryable=False)
        self.writes+=1;self.total_submissions+=1
        if self.failures:
            self.failures-=1
            raise ActivityError('临时繁忙','TEMPORARY',502,retryable=True)
        self.done=True
        return {'success':True,'message':'领取成功','prizes':[]}
    def logout(self):self.mode='none'


class Event:
    def __init__(self,cancel=False):self.cancel=cancel;self.waited=False
    def is_set(self):return self.cancel and self.waited
    def wait(self,seconds):self.waited=True;return self.cancel


class AutomaticWednesdayTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.now=datetime(2026,10,28,7,54,59,tzinfo=SHANGHAI)
        self.pool=[{'id':'a','name':'主号','phone_masked':'138****0001','status':'active','credential_revision':1},
                   {'id':'b','name':'备用','phone_masked':'138****0002','status':'login_required','credential_revision':1}]
        self.prepared=[]
        self.manager=AccountManager(default_client=Client(self),client_factory=lambda profile_dir=None:Client(self,profile_dir))
        self.path=Path(self.temp.name)/'schedules.json'
        self.scheduler=ClaimScheduler(self.manager,storage=self.path,now=lambda:self.now)
        self.app=create_app(manager=self.manager,scheduler=self.scheduler,
            app_accounts_provider=lambda:deepcopy(self.pool),app_login_provider=lambda key:None,
            app_credentials_provider=lambda key:{},app_prepare_provider=self.prepare)
        self.http=self.app.test_client();self.http.get('/')
        with self.http.session_transaction() as session:self.headers={'X-CSRF-Token':session['csrf']}
    def prepare(self,account):self.prepared.append(account.pool_login_id)
    def account(self,key='a'):return next(a for a in self.manager.accounts.values() if a.pool_login_id==key)
    def sync(self):self.scheduler.sync_automatic()
    def execute(self,key='a'):
        account=self.account(key);target=datetime(2026,10,28,8,tzinfo=SHANGHAI)
        self.scheduler._worker(account.id,target,Event())

    def test_default_all_valid_accounts_need_no_manual_binding_or_scheduling(self):
        self.sync();account=self.account()
        self.assertEqual(len(self.scheduler.jobs),1)
        job=self.scheduler.jobs[account.id]
        self.assertTrue(job['managed'] and job['weekly'] and job['enabled'])
        self.assertEqual(job['next_run_at'],'2026-10-28T08:00:00+08:00')
        self.assertEqual(self.prepared,[]);self.assertEqual(account.client.refreshes,0)
        self.pool[1].update(status='active',credential_revision=2)
        self.pool.append({'id':'c','name':'新号','phone_masked':'138****0003','status':'active'})
        self.sync();self.assertEqual(len(self.scheduler.jobs),3)
        self.sync();self.assertEqual(len(self.manager.accounts),3)

    def test_prepare_dispatch_starts_at_0755_for_all_valid_accounts(self):
        self.pool[1]['status']='active'
        with patch('recharge.scheduler.threading.Thread') as thread:
            self.scheduler.tick();thread.assert_not_called()
            self.now=self.now.replace(minute=55,second=0)
            self.scheduler.tick();self.assertEqual(thread.call_count,2)
            self.assertTrue(all(call.kwargs['args'][1].hour==8 for call in thread.call_args_list))

    def test_prepare_validates_cookie_before_waiting_and_does_not_claim_early(self):
        self.sync();self.now=self.now.replace(minute=55,second=0)
        self.scheduler._worker(self.account().id,self.now.replace(hour=8,minute=0),Event(cancel=True))
        self.assertEqual(self.prepared,['a']);self.assertEqual(self.account().client.renewals,1)
        self.assertEqual(self.account().client.refreshes,1);self.assertEqual(self.account().client.writes,0)

    def test_at_eight_success_rolls_to_next_week_without_resubmitting(self):
        self.sync();self.now=self.now.replace(hour=8,minute=0,second=0);self.execute()
        job=self.scheduler.jobs[self.account().id]
        self.assertEqual(job['completed_week'],'2026-10-28')
        self.assertEqual(job['next_run_at'],'2026-11-04T08:00:00+08:00')
        self.assertEqual(self.account().client.writes,1)
        with patch('recharge.scheduler.threading.Thread') as thread:
            self.scheduler.tick();thread.assert_not_called()

    def test_late_start_checks_official_result_and_does_not_duplicate_a_claim(self):
        self.now=self.now.replace(hour=13)
        self.sync();self.account().client.done=True
        with patch('recharge.scheduler.threading.Thread') as thread:
            self.scheduler.tick();thread.assert_called_once()
        self.execute()
        self.assertEqual(self.account().client.writes,0)
        self.assertEqual(self.scheduler.jobs[self.account().id]['state'],'completed')

    def test_failed_login_waits_for_recovery_then_rejoins_the_same_wednesday(self):
        self.sync();self.now=self.now.replace(hour=8,minute=1)
        def expired(account):raise ActivityError('需要重新登录','APP_LOGIN_REQUIRED',401,retryable=False)
        self.scheduler.prepare_account=expired;self.execute()
        job=self.scheduler.jobs[self.account().id]
        self.assertFalse(job['enabled'])
        self.sync();self.assertFalse(job['enabled'])
        self.pool[0]['credential_revision']=2;self.scheduler.prepare_account=self.prepare
        self.sync();self.assertTrue(job['enabled']);self.execute()
        self.assertEqual(self.account().client.writes,1)

    def test_temporary_preparation_failure_retries_after_one_minute(self):
        self.sync();self.now=self.now.replace(hour=8,minute=0,second=0)
        def failed(account):raise ActivityError('网络断开','UPSTREAM_NETWORK',502,retryable=True)
        self.scheduler.prepare_account=failed;self.execute();job=self.scheduler.jobs[self.account().id]
        self.sync();self.assertFalse(job['enabled'])
        self.now+=timedelta(seconds=61);self.sync();self.assertTrue(job['enabled'])

    def test_twenty_attempt_cap_is_not_reset_by_background_account_refresh(self):
        self.sync();self.now=self.now.replace(hour=8,minute=0,second=0)
        self.account().client.failures=30;self.execute()
        self.assertEqual(self.account().client.writes,20)
        self.pool[0]['credential_revision']+=1;self.sync()
        self.assertEqual(self.scheduler.jobs[self.account().id]['next_run_at'],'2026-11-04T08:00:00+08:00')

    def test_stop_only_skips_this_round_and_automatically_resumes_next_week(self):
        self.sync();account=self.account();self.scheduler.cancel([account.id]);self.sync()
        self.assertEqual(self.scheduler.jobs[account.id]['skip_week'],'2026-10-28')
        self.assertEqual(self.scheduler.jobs[account.id]['next_run_at'],'2026-11-04T08:00:00+08:00')
        self.now+=timedelta(days=7);self.sync()
        self.assertTrue(self.scheduler.jobs[account.id]['enabled'])

    def test_exited_account_has_no_task_left_in_storage(self):
        self.sync();self.pool.clear();self.sync()
        self.assertFalse(self.scheduler.jobs)
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8'))['jobs'],[])

    def test_legacy_manual_time_migrates_to_eight_without_erasing_history(self):
        self.sync();job=self.scheduler.jobs[self.account().id]
        job.update(managed=False,next_run_at='2026-10-28T08:35:00+08:00',history=[{'message':'旧记录'}])
        self.sync();self.assertEqual(job['next_run_at'],'2026-10-28T08:00:00+08:00')
        self.assertEqual(job['history'],[{'message':'旧记录'}])

    def test_expired_login_stops_preparation_and_recovery_without_revision_change_rejoins(self):
        self.sync();account=self.account();self.pool[0]['status']='login_required';self.sync()
        self.assertFalse(self.scheduler.jobs[account.id]['enabled'])
        self.assertEqual(self.scheduler.jobs[account.id]['message'],LOGIN_REQUIRED_MESSAGE)
        self.pool[0]['status']='active';self.sync()
        self.assertTrue(self.scheduler.jobs[account.id]['enabled'])
        self.assertEqual(self.scheduler.jobs[account.id]['message'],LOGIN_RECOVERED_MESSAGE)
        self.assertFalse(self.scheduler.jobs[account.id]['blocked_login'])
        self.assertEqual(self.account().id,account.id)

    def test_legacy_recovered_job_repairs_only_stale_notice_and_persists_it_once(self):
        self.sync();account=self.account();job=self.scheduler.jobs[account.id]
        history=[{'time':self.now.isoformat(),'message':'原登录过期记录'}]
        job.update(enabled=True,state='waiting',blocked_login=False,attempt_status='login_required',
                   message=LOGIN_REQUIRED_MESSAGE,history=history,attempts=3,submissions=2)
        self.scheduler._save()
        with patch.object(self.scheduler,'_save',wraps=self.scheduler._save) as save:
            self.sync();self.sync();self.assertEqual(save.call_count,1)
        self.assertEqual(job['message'],LOGIN_RECOVERED_MESSAGE)
        self.assertEqual(job['history'],history)
        self.assertEqual((job['attempts'],job['submissions']),(3,2))
        saved=json.loads(self.path.read_text(encoding='utf-8'))['jobs'][0]
        self.assertEqual(saved['message'],LOGIN_RECOVERED_MESSAGE)
        response=self.http.get('/api/accounts').json
        self.assertEqual(response['jobs'][account.id]['message'],LOGIN_RECOVERED_MESSAGE)
        self.assertEqual(self.prepared,[]);self.assertEqual(account.client.refreshes,0)

    def test_actual_expired_login_keeps_warning_and_completed_or_skipped_weeks_are_not_reset(self):
        self.pool[1]['status']='active';self.sync();one=self.account('a');two=self.account('b')
        self.pool[1]['status']='login_required';self.sync()
        job=self.scheduler.jobs[one.id]
        job.update(completed_week='2026-10-28',blocked_login=False,message=LOGIN_REQUIRED_MESSAGE)
        self.sync()
        self.assertEqual(job['message'],LOGIN_RECOVERED_MESSAGE)
        self.assertEqual(job['next_run_at'],'2026-11-04T08:00:00+08:00')
        expired=self.scheduler.jobs[two.id]
        self.assertFalse(expired['enabled']);self.assertEqual(expired['message'],LOGIN_REQUIRED_MESSAGE)
        self.assertTrue(expired['blocked_login'])

    def test_restart_checks_a_possibly_submitted_round_before_resuming(self):
        self.sync();account=self.account();self.now=self.now.replace(hour=8,minute=0,second=0)
        self.scheduler.jobs[account.id].update(state='running',submissions=0)
        self.scheduler._save();account.client.done=True
        restored=ClaimScheduler(self.manager,storage=self.path,now=lambda:self.now)
        self.assertTrue(restored.jobs[account.id]['uncertain'])
        restored.configure_automatic(lambda:deepcopy(self.pool),self.app.extensions['scheduler'].ensure_shared_account,self.prepare)
        restored.sync_automatic();restored._worker(account.id,self.now,Event())
        self.assertEqual(account.client.writes,0)
        self.assertEqual(restored.jobs[account.id]['state'],'completed')
        self.assertFalse(restored.jobs[account.id]['uncertain'])

    def test_one_page_has_both_tabs_no_manual_time_or_account_selection_form(self):
        page=self.http.get('/').text
        self.assertIn('wed-tab-activity',page);self.assertIn('wed-tab-tasks',page)
        self.assertNotIn('datetime-local',page);self.assertNotIn('新建预约',page)
        self.assertNotIn('create-account-list',page)
        old=self.http.get('/static/tasks.html?detail=fixture')
        self.assertEqual(old.status_code,302);self.assertEqual(old.location,'/?tab=tasks&detail=fixture')
        result=self.http.get('/api/accounts').json
        self.assertTrue(result['automatic']);self.assertEqual(result['prepare_time'],'07:55')
        self.assertEqual(result['run_time'],'08:00');self.assertEqual(len(result['jobs']),1)
        denied=self.http.post('/api/schedules',json={},headers=self.headers)
        self.assertEqual(denied.status_code,409);self.assertEqual(denied.json['code'],'AUTOMATIC_SCHEDULE')


if __name__=='__main__':unittest.main()
