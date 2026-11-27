import copy
from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from features.checkin.beans import CheckinBeans, _task_support, CATALOG, TASK_LIST, TASK_INFO, FINISH, AWARD, STOCK, EXCHANGE
from features.checkin.client import CheckinError
from recharge.cmcc import SHANGHAI


NOW = datetime(2026, 11, 24, 10, tzinfo=SHANGHAI)


class MemoryStorage:
    def __init__(self):
        self.values = {}

    def load_activity_data(self, key):
        return copy.deepcopy(self.values.get(key))

    def save_activity_data(self, key, value):
        self.values[key] = copy.deepcopy(value)


class TaskClient:
    def __init__(self):
        self.status, self.balance = 0, 0
        self.writes = []
        self.uncertain = False
        self.province_task = '0'
        self.task = dict(taskId='new-id', taskName='查询我的套餐', taskClasifiName='功能体验区',
                         taskCategory='2', taskType='3', awardNum='3', awardType='1',
                         jumpUrl='https://wx.10086.cn/cmcc-service/app-pages/main/myPackage')

    @contextmanager
    def _session(self):
        yield self, {}

    def get(self, *_args, **_kwargs):
        return SimpleNamespace(status_code=200)

    def _post(self, _session, path, body=None, *, snapshot=None, before_submit=None):
        if path == CATALOG:
            data = dict(currentFee=self.balance, totalFee=self.balance, exchangeFee=0,
                        timeStamp=int(NOW.timestamp() * 1000), prizes=[], prizeStocks=[])
        elif path == TASK_LIST:
            data = {'tasks': [{**self.task, 'status': self.status}]}
        elif path == TASK_INFO:
            data = dict(taskId=self.task['taskId'], taskType=self.task['taskCategory'],
                        provinceTask=self.province_task, scanTime=5)
        elif path in (FINISH, AWARD):
            before_submit()
            self.writes.append((path, body))
            if self.uncertain:
                raise CheckinError('响应丢失', 'RESULT_UNCERTAIN', uncertain=True)
            if path == FINISH:
                self.status = 1
            else:
                self.status, self.balance = 2, 3
            data = True
        else:
            raise AssertionError(path)
        return dict(code='SUCCESS', data=data)


class BeanTaskTests(unittest.TestCase):
    def setUp(self):
        self.client, self.storage = TaskClient(), MemoryStorage()
        self.scheduler = SimpleNamespace(
            now=lambda: NOW, credentials=lambda _key: {},
            client_factory=lambda _provider: self.client,
            accounts=lambda: [dict(id='a', status='active')],
            busy_login=lambda _key: False,
            data={'automation': {'signin_time': '08:00'}},
        )
        self.query = Mock()
        self.manager = CheckinBeans(self.scheduler, self.storage, query=self.query)
        self.addCleanup(self.manager.executor.shutdown)

    def test_login_update_rejects_unknown_submission_and_running_tasks(self):
        self.manager._save('a',{'kind':'claim','id':'task','month':'2026-11'})
        self.assertFalse(self.manager.begin_login_update('a'))
        self.manager._save('a',{});self.manager.running.add('a')
        self.assertFalse(self.manager.begin_login_update('a'))
        self.manager.running.clear();self.assertTrue(self.manager.begin_login_update('a'))
        self.assertFalse(self.manager.begin_login_update('a'))
        self.manager.end_login_update('a');self.assertTrue(self.manager.begin_login_update('a'))

    def test_daily_work_is_deferred_during_login_update_without_marking_done(self):
        self.manager.executor=Mock()
        self.assertTrue(self.manager.begin_login_update('a'))
        self.assertEqual(self.manager.queue('a',automatic=True),0)
        self.assertEqual(self.manager.data,{})
        self.manager.end_login_update('a')
        self.assertEqual(self.manager.queue('a',automatic=True),1)
        self.manager.executor.submit.assert_called_once()

    def test_write_operations_wait_for_login_update_while_read_only_state_remains_available(self):
        self.manager.begin_login_update('a')
        with self.assertRaises(CheckinError) as error:self.manager.run('a',kind='claim',item_id='new-id')
        self.assertEqual(error.exception.code,'PARTNER_LOGIN_UPDATING')
        self.assertEqual(self.client.writes,[])
        self.assertEqual(self.manager.run('a')['balance'],'0')

    def test_partner_auth_failure_exposes_the_matching_local_update_entry(self):
        self.client.task.update(taskName='去百度地图领现金',taskCategory='6',taskType='1',jumpUrl='baidumap')
        self.manager._record('a',date=NOW.date().isoformat(),deferred_tasks={
            'new-id':{'code':'BAIDU_MAP_LOGIN_REQUIRED','message':'本人百度地图登录失效'}})
        row=self.manager.run('a')['tasks'][0]
        self.assertEqual(row['partner_service'],'baidu-map');self.assertTrue(row['partner_login_required'])
        self.assertEqual(row['reason'],'本人百度地图登录失效');self.assertEqual(self.client.writes,[])

    def test_kuaishou_delayed_callback_is_reconciled_after_restart_without_replaying_undertake(self):
        self.client.task.update(taskName='去快手极速版领金币', taskCategory='6', taskType='1', jumpUrl='kuaishou')
        self.client._current = Mock()
        adapter = Mock()
        def complete(_item, _info, *, before_submit):
            before_submit()
            return {'undertake_confirmed': True, 'awaiting_mobile_confirmation': True}
        adapter.complete.side_effect = complete
        self.manager.kuaishou_factory = Mock(return_value=adapter)
        self.manager._perform_tasks('a')
        self.assertTrue(self.manager.pending['a'])
        self.assertEqual(self.client.writes, [])
        restarted = CheckinBeans(self.scheduler, self.storage, kuaishou_factory=self.manager.kuaishou_factory)
        self.addCleanup(restarted.executor.shutdown)
        restarted._perform_tasks('a')
        adapter.complete.assert_called_once()
        self.client.status = 1
        restarted._perform_tasks('a')
        adapter.complete.assert_called_once()
        self.assertEqual([path for path, _ in self.client.writes], [AWARD])
        self.assertEqual(restarted.data['a']['balance'], '3')
        self.assertFalse(restarted.data['a']['kuaishou']['awaiting_mobile_confirmation'])
        self.assertTrue(restarted.data['a']['kuaishou']['business_callback_verified'])

    def test_completed_kuaishou_is_skipped_without_using_generic_finish(self):
        self.client.task.update(taskName='去快手极速版领金币', taskCategory='6', taskType='1', jumpUrl='kuaishou')
        self.client.status = 2
        factory = Mock()
        self.manager.kuaishou_factory = factory
        self.manager._perform_tasks('a')
        factory.assert_not_called()
        self.assertEqual(self.client.writes, [])

    @patch('features.checkin.beans.time.sleep')
    def test_query_then_finish_then_claim_and_no_replay(self, _sleep):
        self.manager._perform_tasks('a')
        self.query.assert_called_once_with('a', 'usage')
        self.assertEqual([path for path, _ in self.client.writes], [FINISH, AWARD])
        self.assertEqual(self.manager.data['a']['balance'], '3')
        self.manager._perform_tasks('a')
        self.assertEqual(len(self.client.writes), 2)

    @patch('features.checkin.beans.time.sleep')
    def test_failed_real_query_never_reports_completion(self, _sleep):
        self.query.side_effect = CheckinError('查询失败', 'UPSTREAM_NETWORK')
        self.manager._perform_tasks('a')
        self.assertEqual(self.client.writes, [])
        self.assertEqual(self.manager.data['a']['state'], 'failed')
        self.assertIsNotNone(self.manager.data['a']['retry_at'])

    @patch('features.checkin.beans.time.sleep')
    def test_uncertain_completion_survives_restart_and_blocks_resubmission(self, _sleep):
        self.client.uncertain = True
        self.manager._perform_tasks('a')
        restarted = CheckinBeans(self.scheduler, self.storage, query=self.query)
        self.addCleanup(restarted.executor.shutdown)
        restarted._perform_tasks('a')
        self.assertEqual(len(self.client.writes), 1)
        self.assertEqual(restarted.data['a']['code'], 'RESULT_UNCERTAIN')

    @patch('features.checkin.beans.time.sleep')
    def test_automatic_unknown_result_check_claims_confirmed_award_without_replaying_finish(self, _sleep):
        self.client.uncertain=True
        self.manager._perform_tasks('a')
        self.assertIsNotNone(self.manager.data['a']['retry_at'])
        # The upstream operation completed despite the lost acknowledgement.
        self.client.uncertain=False
        self.client.status=1
        self.scheduler.now=lambda:NOW+timedelta(minutes=5)
        with patch.object(self.manager.executor,'submit',side_effect=lambda fn,arg:fn(arg)):
            self.manager.tick()
        self.assertEqual([path for path,_ in self.client.writes],[FINISH,AWARD])
        self.assertEqual(self.manager.data['a']['state'],'completed')

    def test_changed_destination_or_category_is_unsupported(self):
        self.assertTrue(_task_support(self.client.task)[0])
        self.assertFalse(_task_support({**self.client.task, 'taskCategory': '4'})[0])
        self.assertFalse(_task_support({**self.client.task, 'jumpUrl': 'https://other.example/path'})[0])

    @patch('features.checkin.beans.time.sleep')
    def test_overview_is_a_real_query_before_reporting(self, _sleep):
        self.client.task.update(taskName='一键通查',
                                jumpUrl='https://wx.10086.cn/cmcc-service/app-pages/extra/queryPolymerize')
        self.manager._perform_tasks('a')
        self.query.assert_called_once_with('a', 'overview')
        self.assertEqual([path for path, _ in self.client.writes], [FINISH, AWARD])

    @patch('features.checkin.beans.time.sleep')
    def test_verified_activity_view_follows_official_task_contract(self, _sleep):
        self.client.task.update(taskName='去浏览好礼月月领', taskCategory='8', taskType='2',
            jumpUrl='https://dev.coc.10086.cn/coc3/canvas/rightsmarket-h5-canvas/online/26hlyyltybld')
        self.client.province_task = '1'
        self.assertTrue(_task_support(self.client.task)[0])
        self.manager._perform_tasks('a')
        self.query.assert_not_called()
        self.assertEqual([path for path, _ in self.client.writes], [FINISH, AWARD])
        self.assertEqual(self.manager.data['a']['balance'], '3')

    @patch('features.checkin.beans.time.sleep')
    def test_missing_task_readback_keeps_pending_marker(self, _sleep):
        original = self.client._post
        def response(*args, **kwargs):
            result = original(*args, **kwargs)
            if args[1] == TASK_LIST and self.client.writes:
                result['data']['tasks'] = []
            return result
        with patch.object(self.client, '_post', side_effect=response):
            self.manager._perform_tasks('a')
        self.assertEqual(self.manager.data['a']['code'], 'RESULT_UNCERTAIN')
        self.assertEqual([path for path, _ in self.client.writes], [FINISH])
        self.assertTrue(self.manager.pending['a'])

    @patch('features.checkin.beans.time.sleep')
    def test_official_balance_redirect_followed_before_real_query(self, _sleep):
        self.client.task.update(taskName='浏览话费余额',
                                jumpUrl='https://h.app.coc.10086.cn/cmcc-app/app-pages/balanceQuery.html')
        redirect = SimpleNamespace(status_code=302, headers={
            'location': 'https://wx.10086.cn/cmcc-service/app-pages/main/balanceQuery'})
        with patch.object(self.client, 'get', side_effect=[redirect, SimpleNamespace(status_code=200)]) as get:
            self.manager._perform_tasks('a')
        self.assertEqual(get.call_count, 2)
        self.query.assert_called_once_with('a', 'balance')
        self.assertEqual(self.manager.data['a']['balance'], '3')

    def test_automatic_queue_skips_completed_accounts_and_duplicate_workers(self):
        self.manager._record('a', date=NOW.date().isoformat(), state='completed', retry_at=None)
        with patch.object(self.manager.executor, 'submit') as submit:
            self.manager.tick()
            submit.assert_not_called()
            self.assertEqual(self.manager.queue('a'), 1)
            self.assertEqual(self.manager.queue('a'), 0)
            submit.assert_called_once()

    def test_restored_login_resumes_deferred_partner_authentication(self):
        self.manager._record('a',date=NOW.date().isoformat(),state='partial',code='',
                             deferred_tasks={'5621':{'code':'MIGU_LOGIN_REQUIRED'}})
        with patch.object(self.manager.executor,'submit') as submit:
            self.manager.tick(restored=['a'])
            submit.assert_called_once()

    def test_empty_official_task_list_preserves_recent_records_and_stops_execution(self):
        self.manager._perform_tasks('a')
        initial = copy.deepcopy(self.manager.data['a']['tasks'])
        original = self.client._post
        def response(*args, **kwargs):
            result = original(*args, **kwargs)
            if args[1] == TASK_LIST:
                result['data']['tasks'] = []
            return result
        with patch.object(self.client, '_post', side_effect=response):
            status = self.manager.run('a')
            self.assertEqual(status['tasks'], initial)
            self.assertFalse(status['tasks_available'])
            self.manager._perform_tasks('a')
        self.assertEqual(self.manager.data['a']['code'], 'UPSTREAM_TASKS_UNAVAILABLE')

    def test_yesterdays_claimed_tasks_are_not_shown_as_confirmed_today(self):
        self.manager._record('a', date='2026-11-23', tasks=[{'id':'old','state':'claimed'}])
        original = self.client._post
        def response(*args, **kwargs):
            result = original(*args, **kwargs)
            if args[1] == TASK_LIST:
                result['data']['tasks'] = []
            return result
        with patch.object(self.client, '_post', side_effect=response):
            self.manager._perform_tasks('a')
        self.assertEqual(self.manager.data['a']['tasks'], [])

    def test_task_submission_scope_rolls_over_daily_but_redemption_does_not(self):
        self.manager._save('a', {'kind':'finish','id':'new-id','month':'2026-11','date':'2026-11-23'})
        self.assertFalse(self.manager.run('a')['pending'])
        self.manager._save('a', {'kind':'redeem','id':'prize','month':'2026-11'})
        self.assertTrue(self.manager.run('a')['pending'])


class VideoExecutionTests(unittest.TestCase):
    def setUp(self):
        from features.checkin.video import CheckinVideo
        self.video = CheckinVideo(Mock(), Mock(), player=Mock(return_value={'frames':125, 'played_seconds':5}))
        @contextmanager
        def session():
            yield Mock(), {'phone':'MDEyMzQ1Njc4OWFiY2RlZg==','provinceCode':'851','cityCode':'851'}
        self.video._session = session
        self.task = {'taskName':'看精彩视频','jumpUrl':'https://v.cmcc-cs.cn/cmlive/live/feed?collectId=514&cmliveChannelId=30&belongChannelId=30'}
        self.info = {'taskToken':'task-token'}
        self.calls = []
        def call(_client, _method, path, *args, **kwargs):
            self.calls.append(path)
            if path == 'advert/signIn/getOneTaskInfo':
                return {'data':{'status':'0','scanTime':'5','cToken':'finish-token'}}
            if path == 'video/video/mixFeedList':
                return {'feedVideoList':[{'id':i,'videoSource':0,'videoUrl':f'https://activity.cmcc-cs.cn/{i}.mp4'} for i in range(4)]}
            return {'data':True}
        self.video._call = call

    def test_no_completion_report_if_actual_playback_fails(self):
        self.video.player.side_effect = CheckinError('视频播放失败', 'PLAYBACK_INCOMPLETE')
        before = Mock()
        with self.assertRaises(CheckinError):
            self.video.complete(self.task, self.info, before_submit=before)
        before.assert_not_called()
        self.assertNotIn('advert/signIn/openFinish', self.calls)

    def test_completion_report_follows_four_actual_playbacks(self):
        before = Mock()
        result = self.video.complete(self.task, self.info, before_submit=before)
        self.assertEqual(self.video.player.call_count,4)
        self.assertEqual(len(result),4)
        before.assert_called_once()
        self.assertEqual(self.calls[-1], 'advert/signIn/openFinish')

    def test_live_replay_requires_actual_playback_and_room_cleanup(self):
        self.task.update(taskName='看直播0元领会员',
                         jumpUrl='https://v.cmcc-cs.cn/cmlive/live/room?id=147687&cmliveChannelId=243')
        original = self.video._call
        def call(client, method, path, *args, **kwargs):
            if path == 'room/front/roomBeat':
                return {'status':6,'playbackUrl':'https://activity.cmcc-cs.cn/replay.mp4'}
            return original(client, method, path, *args, **kwargs)
        self.video._call = call
        playback = self.video.complete(self.task, self.info, before_submit=Mock())
        self.assertEqual(self.video.player.call_count,1)
        self.assertTrue(playback[0]['replay'])
        self.assertIn('room/front/leaveRoom',self.calls)


class CloudExecutionTests(unittest.TestCase):
    def setUp(self):
        from features.checkin.cloud import CheckinCloud
        self.cloud = CheckinCloud(Mock(), Mock())
        self.client = SimpleNamespace(_credential_data={'hwToken':'cloud-hw-ticket'})
        @contextmanager
        def session(*_args):
            yield self.client
        self.cloud._session = session
        import base64
        import json
        params = base64.b64encode(json.dumps({'linkUrl':
            'https://m.mcloud.139.com/portal/hcyview/index.html?token=#ssoToken#'}).encode()).decode()
        self.calls = []
        def call(client, path, body, **kwargs):
            self.calls.append((path, body))
            return {
                'appInfo/appManage/query': {'canPlay':1,'marketConfig':{'browseTaskDuration':12}},
                'appInfo/appAuthLogin/query': {'checkAuth':False, 'schemePath':'mcloud://main/webView?params='+params},
                'user/getRcsTokenHW': {'ssoToken':'landing-ticket'},
                'cloudDisk/getOneTaskInfo': {'data':{'status':'0','scanTime':'5','ctoken':'derived-task-ticket'}},
                'cloudDisk/openFinish': {'finished':True},
            }[path]
        self.cloud._call = Mock(side_effect=call)
        self.task = dict(taskName='赢10元红包等好礼',taskCategory='10',taskType='3',
                         jumpUrl='https://capp.139.com/#/?i=bG6ZWOmupgY=&appEnv=cmccApp')
        self.page = Mock()
        self.page.get.return_value.status_code = 200
        self.http = patch('features.checkin.cloud.requests.Session')
        self.http_session = self.http.start()
        self.addCleanup(self.http.stop)
        self.http_session.return_value.__enter__.return_value = self.page

    def test_published_encrypted_app_id_and_changed_destination(self):
        from features.checkin.cloud import des_text, supported_task
        self.assertEqual(des_text('bG6ZWOmupgY=', decrypt=True), '14086')
        self.assertEqual(des_text('7pU/bbnhGUI=', decrypt=True), '13340')
        self.assertTrue(supported_task(self.task))
        self.assertFalse(supported_task({**self.task,'jumpUrl':'https://other.example/#/?i=bG6ZWOmupgY='}))
        self.assertFalse(supported_task({**self.task,'taskType':'2'}))

    @patch('features.checkin.cloud.time.sleep')
    def test_real_landing_and_configured_browse_time_before_derived_callback(self, sleep):
        before = Mock()
        self.cloud.complete(self.task, {'taskToken':'parent-ticket'}, before_submit=before)
        self.page.get.assert_called_once_with(
            'https://m.mcloud.139.com/portal/hcyview/index.html?token=landing-ticket',
            timeout=(8,20),allow_redirects=False)
        # Only the public UA is sent to the other host; cloud auth stays scoped.
        self.assertEqual(set(self.http_session.call_args.kwargs['headers']), {'User-Agent'})
        self.assertGreater(sleep.call_args.args[0],11)
        before.assert_called_once()
        self.assertEqual(self.calls[-1], ('cloudDisk/openFinish',{'token':'derived-task-ticket'}))

    def test_failed_landing_never_sends_completion(self):
        self.page.get.return_value.status_code = 403
        before = Mock()
        with self.assertRaises(CheckinError):
            self.cloud.complete(self.task, {'taskToken':'parent-ticket'}, before_submit=before)
        before.assert_not_called()
        self.assertNotIn('cloudDisk/openFinish',[path for path,_ in self.calls])

    def test_write_auth_failure_never_refreshes_or_replays(self):
        from features.checkin.cloud import CheckinCloud
        client = Mock(headers={'token':'old-token'})
        client.post.return_value = SimpleNamespace(status_code=200,
            json=lambda:{'header':{'status':'1024'}})
        cloud = CheckinCloud(Mock(),Mock())
        with self.assertRaises(CheckinError) as raised:
            cloud._call(client,'cloudDisk/openFinish',{'token':'task-ticket'},writing=True)
        self.assertTrue(raised.exception.uncertain)
        cloud.cache.recover.assert_not_called()
        client.post.assert_called_once()


class CloudLoginTests(unittest.TestCase):
    def test_encrypted_phone_is_verified_and_session_reused_after_revision_changes(self):
        from features.checkin.cloud import CheckinCloud, des_text, SCOPE
        from workspace.accounts.credentials import CredentialCache
        snapshot = {'id':'a','phone':'13800138000','revision':1}
        cache = CredentialCache()
        cloud = CheckinCloud(lambda: snapshot,cache)
        cloud._call = Mock(return_value={'phone':des_text(snapshot['phone']), 'token':'opaque-cloud-token',
                                         'hwToken':'hw-ticket','expireTime':2592000})
        with patch('features.checkin.cloud.AppLoginSession.from_credentials') as login:
            login.return_value.call.return_value = (None,{'retCode':'000000','rspBody':{'token':'app-ticket'}},None)
            with cloud._session('https://capp.139.com/','14086') as client:
                self.assertEqual(client._credential_data['phone'],snapshot['phone'])
            snapshot['revision'] = 2
            with cloud._session('https://capp.139.com/','13340') as client:
                self.assertEqual(client.headers['token'],'opaque-cloud-token')
            login.assert_called_once()
        self.assertIsNone(cache.load(SCOPE,snapshot)['expires_at'])

    def test_different_account_is_rejected_before_cache_or_business_submission(self):
        from features.checkin.cloud import CheckinCloud, des_text, SCOPE
        from workspace.accounts.credentials import CredentialCache
        snapshot = {'id':'a','phone':'13800138000'}
        cache = CredentialCache()
        cloud = CheckinCloud(lambda: snapshot,cache)
        cloud._call = Mock(return_value={'phone':des_text('13900139000'), 'token':'other-token'})
        with patch('features.checkin.cloud.AppLoginSession.from_credentials') as login:
            login.return_value.call.return_value = (None,{'retCode':'000000','rspBody':{'token':'app-ticket'}},None)
            with self.assertRaises(CheckinError) as error:
                with cloud._session('https://capp.139.com/','14086'):
                    self.fail('A different account must not reach business code')
        self.assertEqual(error.exception.code,'ACCOUNT_MISMATCH')
        self.assertIsNone(cache.load(SCOPE,snapshot))


class PrizeClient(TaskClient):
    def __init__(self):
        super().__init__()
        self.balance, self.stock, self.fresh_stock, self.prize_status = 200, 1000, 1000, 'WITHOUT'

    def _post(self, session, path, body=None, *, snapshot=None, before_submit=None):
        if path == CATALOG:
            return {'code':'SUCCESS','data':dict(currentFee=self.balance,totalFee=200,
                exchangeFee=200-self.balance,timeStamp=int(NOW.timestamp()*1000),
                prizes=[dict(prizeType='1',id='p',name='1元话费券',cost='100',status=self.prize_status)],
                prizeStocks=[{'id':'p','stock':self.stock}])}
        if path == STOCK:
            return {'code':'SUCCESS','data':{'stock':self.fresh_stock}}
        if path == EXCHANGE:
            before_submit()
            self.writes.append((path,body))
            if self.uncertain:
                raise CheckinError('兑换响应丢失','RESULT_UNCERTAIN',uncertain=True)
            self.balance-=100
            self.prize_status='UNUSED'
            return {'code':'SUCCESS','data':True}
        return super()._post(session,path,body,snapshot=snapshot,before_submit=before_submit)


class BeanRedemptionTests(unittest.TestCase):
    def setUp(self):
        self.client,self.storage=PrizeClient(),MemoryStorage()
        self.scheduler=SimpleNamespace(client_factory=lambda _provider:self.client,credentials=lambda _key:{})
        self.manager=CheckinBeans(self.scheduler,self.storage)
        self.addCleanup(self.manager.executor.shutdown)

    def test_confirmation_cost_must_match_fresh_official_prize(self):
        with self.assertRaises(CheckinError):
            self.manager.run('a',kind='redeem',item_id='p',cost='99')
        self.assertEqual(self.client.writes,[])

    def test_stock_is_rechecked_before_exchange(self):
        self.client.fresh_stock=100
        with self.assertRaises(CheckinError):
            self.manager.run('a',kind='redeem',item_id='p',cost='100')
        self.assertEqual(self.client.writes,[])

    def test_exchange_requires_official_readback_and_updates_balance(self):
        result=self.manager.run('a',kind='redeem',item_id='p',cost='100')
        self.assertEqual(result['prizes'][0]['state'],'redeemed')
        self.assertEqual(result['balance'],'100')
        self.assertFalse(result['pending'])
        with self.assertRaises(CheckinError):
            self.manager.run('a',kind='redeem',item_id='p',cost='100')
        self.assertEqual(len(self.client.writes),1)

    def test_unknown_exchange_result_is_not_replayed_after_restart(self):
        self.client.uncertain=True
        with self.assertRaises(CheckinError):
            self.manager.run('a',kind='redeem',item_id='p',cost='100')
        restarted=CheckinBeans(self.scheduler,self.storage)
        self.addCleanup(restarted.executor.shutdown)
        with self.assertRaises(CheckinError):
            restarted.run('a',kind='redeem',item_id='p',cost='100')
        self.assertEqual(len(self.client.writes),1)


class ActivityBrowseTests(unittest.TestCase):
    def setUp(self):
        self.session=Mock()
        self.before=Mock()
        self.session.post.side_effect=[
            SimpleNamespace(status_code=200,json=lambda:{'code':'SUCCESS','data':{
                'status':'0','scanTime':'5','taskType':'10','cToken':'derived-token'}}),
            SimpleNamespace(status_code=200,json=lambda:{'code':'SUCCESS','data':True}),
        ]

    @patch('features.checkin.activity_tasks.time.sleep')
    def test_activity_callback_uses_derived_token_and_correct_namespace(self,sleep):
        from features.checkin.activity_tasks import complete_activity_browse,OPEN_API
        complete_activity_browse(self.session,{'taskToken':'parent-token'},before_submit=self.before)
        self.assertEqual(self.session.post.call_args_list[0].args[0],OPEN_API+'getOneTaskInfo')
        self.assertEqual(self.session.post.call_args_list[0].kwargs['json'],{'jtToken':'parent-token'})
        self.assertEqual(self.session.post.call_args_list[1].args[0],OPEN_API+'openFinish')
        self.assertEqual(self.session.post.call_args_list[1].kwargs['json'],{'cToken':'derived-token'})
        self.assertGreater(sleep.call_args.args[0],4)
        self.before.assert_called_once()

    def test_rejected_task_details_do_not_send_completion(self):
        from features.checkin.activity_tasks import complete_activity_browse
        self.session.post.side_effect=[SimpleNamespace(status_code=403)]
        with self.assertRaises(CheckinError):
            complete_activity_browse(self.session,{'taskToken':'parent-token'},before_submit=self.before)
        self.before.assert_not_called()
        self.session.post.assert_called_once()

    @patch('features.checkin.activity_tasks.time.sleep')
    def test_callback_server_error_remains_uncertain_and_is_not_replayed(self,_sleep):
        from features.checkin.activity_tasks import complete_activity_browse
        self.session.post.side_effect=[
            SimpleNamespace(status_code=200,json=lambda:{'code':'SUCCESS','data':{
                'status':'0','scanTime':'5','taskType':'10','cToken':'derived-token'}}),
            SimpleNamespace(status_code=500),
        ]
        with self.assertRaises(CheckinError) as error:
            complete_activity_browse(self.session,{'taskToken':'parent-token'},before_submit=self.before)
        self.assertTrue(error.exception.uncertain)
        self.assertEqual(self.session.post.call_count,2)


if __name__ == '__main__':
    unittest.main()
