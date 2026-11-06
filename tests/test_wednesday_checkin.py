from copy import deepcopy
from datetime import datetime,timedelta
from threading import RLock
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from recharge.checkin import ContinuousCheckin,sign_state
from recharge.cmcc import ActivityError,ActivityClient,SHANGHAI

NOW=datetime(2026,11,6,12,tzinfo=SHANGHAI)
TASKS=[{'order':n,'taskId':{1:'llym',3:'yqyh',5:'czhf'}.get(n,f'ljdk{n}'),'title':f'第{n}天任务'} for n in (7,6,1,5,4,2,3)]
def progress(n=0,today=False,rd=4,**extra):
    return {'code':'0','data':{'taskList':deepcopy(TASKS),'signTimes':n,'isTodaySign':str(int(today)),
             'rdStatus':rd,'isBroken':'0','isEndMonth':'0','prizeInfo':{'name':'充值折扣券','shortName':'8折'},**extra}}

class Store:
    def __init__(self):self.data=None
    def load_activity_data(self,key):return deepcopy(self.data)
    def save_activity_data(self,key,value):self.data=deepcopy(value)

class WednesdayCheckinTests(unittest.TestCase):
    def setUp(self):
        self.now=NOW;self.logins=[{'id':'a','status':'active','credential_revision':1}]
        self.client=SimpleNamespace(mode='app',operation_lock=RLock(),read=Mock(return_value=progress()),
            call=Mock(return_value={'code':'0'}),http=Mock(),credential_cache=None,check_result=ActivityClient.check_result)
        self.client.http.get.return_value=SimpleNamespace(status_code=200)
        self.account=SimpleNamespace(id='activity-a',pool_login_id='a',client=self.client)
        self.storage=Store();self.shutdown=Mock();self.shutdown.wait.return_value=False
        self.daily=ContinuousCheckin(lambda:self.logins,lambda login:self.account,storage=self.storage,now=lambda:self.now,shutdown=self.shutdown)
        self.daily.pool.shutdown();self.daily.pool=Mock()

    def test_shuffled_tasks_use_order_and_special_days_never_submit_fake_completion(self):
        for n,task_id in ((2,'yqyh'),(4,'czhf')):
            self.client.read.return_value=progress(n)
            result=self.daily.run('a',submit=True)
            self.assertEqual(result['action'],'manual');self.assertEqual(result['task_id'],task_id)
        self.client.call.assert_not_called();self.client.http.get.assert_not_called()

    def test_first_day_browses_and_waits_then_submits_only_its_task(self):
        self.client.read.side_effect=[progress(),progress(1,True)]
        result=self.daily.run('a',submit=True)
        self.assertTrue(result['today_signed']);self.client.http.get.assert_called_once()
        self.shutdown.wait.assert_called_once_with(5)
        self.client.call.assert_called_once_with('signFinishTask',{'taskId':'llym'})

    def test_normal_day_and_seventh_day_claim_are_distinct_confirmed_requests(self):
        self.client.read.side_effect=[progress(6),progress(7,True,2),progress(7,True,3)]
        self.assertTrue(self.daily.run('a',submit=True)['received'])
        self.assertEqual([call.args for call in self.client.call.call_args_list],[('signFinishTask',{'taskId':'ljdk7'}),('receiveSign',{})])
        self.assertIsNone(self.storage.data['records']['a']['pending'])

    def test_already_done_month_end_and_unknown_tasks_do_not_write(self):
        for value in (progress(2,True),progress(7,True,3),progress(0,isEndMonth='1'),progress(0,taskList=[{'order':1,'taskId':'new-paid-task'}])):
            self.client.read.return_value=value;self.daily.run('a',submit=True)
        self.client.call.assert_not_called()

    def test_uncertain_sign_is_only_queried_after_restart_then_resolved_by_official_state(self):
        self.client.read.return_value=progress(1)
        self.client.call.side_effect=ActivityError('网络中断','UPSTREAM_NETWORK',502)
        self.daily.run('a',submit=True)
        rebuilt=ContinuousCheckin(lambda:self.logins,lambda login:self.account,storage=self.storage,now=lambda:self.now)
        self.addCleanup(rebuilt.pool.shutdown)
        rebuilt.run('a',submit=True);self.client.call.assert_called_once()
        self.client.read.return_value=progress(2,True)
        self.assertTrue(rebuilt.run('a')['today_signed']);self.assertIsNone(self.storage.data['records']['a']['pending'])

    def test_read_only_query_does_not_defer_unfinished_auto_task_until_tomorrow(self):
        self.daily.run('a');self.client.call.assert_not_called()
        self.daily.tick();self.daily.pool.submit.assert_called_once()

    def test_completed_or_manual_progress_is_not_polled_again_but_next_day_resumes(self):
        for value in (progress(2,True),progress(2),progress(7,True,3)):
            self.client.read.return_value=value;self.daily.run('a');self.daily.next_scan=None;self.daily.tick()
        self.daily.pool.submit.assert_not_called()
        self.now+=timedelta(days=1);self.daily.tick();self.daily.pool.submit.assert_not_called()
        self.now=self.now.replace(month=12,day=1);self.daily.tick();self.daily.pool.submit.assert_called_once()

    def test_broken_streak_uses_day_one_not_the_previous_cycle_day(self):
        state=sign_state(progress(5,isBroken='1')['data'],NOW)
        self.assertEqual(state['day'],1);self.assertEqual(state['task_id'],'llym')

    def test_transient_failures_stop_at_five_with_five_minute_spacing(self):
        self.client.read.side_effect=ActivityError('服务暂不可用','UPSTREAM_HTTP',502)
        for _ in range(5):
            self.daily.run('a',submit=True)
            self.daily.next_scan=None;self.daily.tick();self.daily.pool.submit.assert_not_called()
            self.now+=timedelta(minutes=5)
        self.daily.next_scan=None;self.daily.tick();self.daily.pool.submit.assert_not_called()
        self.now+=timedelta(days=1);self.daily.tick();self.daily.pool.submit.assert_called_once()

    def test_ineligible_and_rate_limit_are_daily_blocks_not_changed_by_cookie_revision(self):
        for error in (ActivityError('不符合活动资格','NOT_ELIGIBLE'),ActivityError('请求频繁','-800')):
            self.client.read.side_effect=error;self.daily.run('a',submit=True)
            self.now+=timedelta(hours=1);self.logins[0]['credential_revision']+=1
            self.daily.next_scan=None;self.daily.tick()
        self.daily.pool.submit.assert_not_called()

    def test_newly_restored_login_rechecks_today_and_removed_accounts_are_deleted(self):
        self.client.read.side_effect=ActivityError('需要登录','APP_LOGIN_REQUIRED',401)
        self.daily.run('a',submit=True);self.logins[0]['credential_revision']=2
        self.daily.tick();self.daily.pool.submit.assert_called_once()
        self.logins=[];self.daily.snapshots();self.assertEqual(self.storage.data['records'],{})

    def test_settings_weekly_priority_and_feature_disable_do_not_submit(self):
        self.daily.configure({'enabled':True,'time':'13:00'});self.daily.tick();self.daily.pool.submit.assert_not_called()
        self.daily.configure({'enabled':True,'time':'08:00'});self.daily.weekly_busy=lambda _:True;self.daily.tick();self.daily.pool.submit.assert_not_called()
        self.daily.weekly_busy=lambda _:False;self.daily.next_scan=None
        with patch('recharge.checkin.feature_running',return_value=False):self.daily.tick()
        self.daily.pool.submit.assert_not_called()
        with self.assertRaises(ActivityError):self.daily.configure({'enabled':True,'time':'26:00'})

    def test_write_endpoints_cannot_use_read_recovery(self):
        for endpoint in ('signFinishTask','receiveSign'):
            with self.assertRaises(ActivityError):ActivityClient.read(self.client,endpoint)

if __name__=='__main__':unittest.main()
