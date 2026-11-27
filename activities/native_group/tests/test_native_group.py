from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from unittest.mock import patch
import hashlib
import platform

from flask import Flask
from features.checkin.client import CheckinError
from recharge.cmcc import SHANGHAI
from activities.native_group.client import NativeGroupClient, ENTRY, invite_token
from activities.native_group.scheduler import GroupScheduler, team_sizes
from activities.native_group.plugin import register
from activities.native_group.device import GroupDevices, SCOPE as DEVICE_SCOPE
from recharge.device import APP_ID, SERVER
from workspace.accounts.credentials import CredentialCache
from workspace.http_transport import requests


class Storage:
    def __init__(self):
        self.data = None
    def load_activity_data(self, key):
        return deepcopy(self.data)
    def save_activity_data(self, key, data):
        self.data = deepcopy(data)


def observation(group=None):
    return {'identity': {'phone': '138****8000', 'channel': 'app'}, 'campaign': {
        'startTime': '2026-11-24 00:00:00', 'endTime': '2026-12-31 23:59:59',
        'showGroup': True, 'userGroupInfo': group, 'groupPrizeBtnStatus': None,
        'drawTimes': 0}, 'prizes': [], 'tasks': []}


class TeamPlanningTests(unittest.TestCase):
    def test_pool_prefers_ten_and_uses_remainder(self):
        for count, expected in [(10,[10]), (13,[10,3]), (15,[10,5]), (16,[10,3,3]),
                                (18,[10,5,3]), (20,[10,10]), (9,[3,3,3]), (2,[])]:
            with self.subTest(count=count):
                self.assertEqual(team_sizes(count), expected)

    def test_planning_excludes_existing_teams_and_failed_qualification(self):
        accounts = [{'id': str(index), 'status': 'active'} for index in range(15)]
        manager = GroupScheduler(lambda: accounts, Mock(), Storage())
        def refresh(key):
            if key == '14':
                raise CheckinError('不符合资格', '10002', 409)
            manager.data['observations'][key] = observation({'groupSize': 3} if key == '13' else None)
        manager.run = refresh
        plan = manager.plan()
        self.assertEqual([team['size'] for team in plan['teams']], [10,3])
        self.assertEqual(len(plan['excluded']), 2)
        self.assertEqual(len(set(key for team in plan['teams'] for key in team['members'])), 13)

    def test_duplicate_batch_members_are_rejected_before_any_business_operation(self):
        manager = GroupScheduler(lambda: [{'id': 'a','status':'active'}], Mock(), Storage())
        manager.run = Mock()
        with self.assertRaises(CheckinError):
            manager.assemble([{'size': 3, 'members': ['a','a','a']}])
        manager.run.assert_not_called()

    def test_partial_batch_keeps_original_team_and_resumes_only_missing_members(self):
        accounts = [{'id': key, 'status': 'active'} for key in ('a','b','c')]
        storage = Storage()
        manager = GroupScheduler(lambda: accounts, Mock(), storage)
        observations = {key: observation() for key in ('a','b','c')}
        for key, value in observations.items():
            value['identity']['phone'] = 'synthetic-' + key
        groups, calls = {}, []
        fail = [True]
        def operation(key, kind='refresh', **kwargs):
            calls.append((key,kind))
            if kind == 'create':
                groups['a'] = {'groupSize':3,'groupMembers':[{'phone':'synthetic-a'}]}
            elif kind == 'invite':
                return {'invitation': {'url':'synthetic-invitation'}}
            elif kind == 'join':
                if key == 'c' and fail[0]:
                    raise CheckinError('连接失败','UPSTREAM_NETWORK')
                groups['a']['groupMembers'].append({'phone':'synthetic-'+key})
                groups[key] = groups['a']
            current = deepcopy(observations[key])
            current['campaign']['userGroupInfo'] = deepcopy(groups.get(key))
            manager.data['observations'][key] = current
            return manager.status()
        manager.run = operation
        teams = [{'size':3,'members':['a','b','c']}]
        with self.assertRaises(CheckinError):
            manager.assemble(teams)
        self.assertEqual(manager.plan()['teams'],teams)
        self.assertTrue(manager.plan()['resume'])
        fail[0] = False
        manager.assemble(teams)
        self.assertEqual(calls.count(('a','create')),1)
        self.assertEqual(calls.count(('b','join')),1)
        self.assertEqual(calls.count(('c','join')),2)
        self.assertIsNone(manager.data['batch'])


class ProtocolTests(unittest.TestCase):
    def test_invitation_uses_risk_verified_generator_and_separate_share_configuration(self):
        snapshot = {'phone':'13800138000'}
        client = NativeGroupClient(lambda:snapshot,credential_cache=Mock(),
                                   device_provider=Mock(return_value='synthetic-risk-ticket'))
        token = 'synthetic+/invite='
        client._call = Mock(side_effect=[{'title':'官方邀请文案','img':'https://example.test/poster.png'},
                                        {'inviteToken':token}])
        state = observation({'groupSize':10,'groupMembers':[{'phone':'138****8000'}]})
        result = client.operate('session',snapshot,'invite',state,before_submit=Mock())
        self.assertEqual(invite_token(result['url']),token)
        self.assertEqual(result['title'],'官方邀请文案')
        self.assertEqual(result['image'],'https://example.test/poster.png')
        self.assertEqual([call.args[1] for call in client._call.call_args_list],
                         ['shareTextInfo','generateInviteToken'])
        self.assertEqual(client._call.call_args.args[2],{'riskConstId':'synthetic-risk-ticket'})

    def test_share_configuration_token_cannot_substitute_for_missing_generated_invitation(self):
        snapshot = {'phone':'13800138000'}
        client = NativeGroupClient(lambda:snapshot,credential_cache=Mock(),device_provider=Mock(return_value='risk'))
        client._call = Mock(side_effect=[{'title':'官方配置','inviteToken':'wrong-field'},{}])
        with self.assertRaises(CheckinError) as raised:
            client.operate('session',snapshot,'invite',observation({'groupSize':3}),before_submit=Mock())
        self.assertEqual(raised.exception.code,'INVITE_RESPONSE_INVALID')

    def test_invite_requires_existing_group_before_risk_registration(self):
        device = Mock()
        client = NativeGroupClient(Mock(),credential_cache=Mock(),device_provider=device)
        client._call = Mock()
        with self.assertRaises(CheckinError):
            client.operate(Mock(),{},'invite',observation(),before_submit=Mock())
        device.assert_not_called()
        client._call.assert_not_called()

    def test_activity_session_expiry_uses_the_observed_hlwhd_cookie(self):
        cache = Mock()
        client = NativeGroupClient(lambda: {'id':'a','phone':'13800138000','cookie':'JSESSIONID=synthetic'},
                                   credential_cache=cache)
        with client.connect():
            pass
        self.assertEqual(cache.connect.call_args.kwargs['expiry_cookies'],('HLWHD_SESSION_TOKEN',))

    def test_real_invitation_parsing_preserves_base64_plus_and_slash(self):
        from urllib.parse import urlencode
        token = 'synthetic+/token='
        direct = ENTRY + '?' + urlencode({'inviteToken': token})
        outer = 'https://h.app.coc.10086.cn/activity/transit/transferDownload.html?' + urlencode({'targetUrl': direct})
        self.assertEqual(invite_token(direct), token)
        self.assertEqual(invite_token(outer), token)
        with self.assertRaises(CheckinError):
            invite_token('https://other.example/?inviteToken=invalid')

    def test_call_or_feedback_completion_is_not_an_accepted_write(self):
        client = NativeGroupClient(Mock(), credential_cache=Mock())
        with self.assertRaises(ValueError):
            client._call(Mock(), 'finishDrawTask', {'taskId': 'mrthrw'})

    def test_incomplete_team_cannot_receive_group_award(self):
        client = NativeGroupClient(Mock(), credential_cache=Mock())
        state = observation({'groupSize': 10, 'groupMembers': [{'phone':'138****8000'}], 'groupTaskStatus': 1})
        state['campaign']['groupPrizeBtnStatus'] = '1'
        client._call = Mock()
        with self.assertRaises(CheckinError):
            client.operate(Mock(), {}, 'claim', state, before_submit=Mock())
        client._call.assert_not_called()

    def test_an_existing_team_is_not_replaced_by_create(self):
        client = NativeGroupClient(Mock(), credential_cache=Mock())
        client._call = Mock()
        with self.assertRaises(CheckinError):
            client.operate(Mock(), {}, 'create', observation({'groupSize': 3}), size=10, before_submit=Mock())
        client._call.assert_not_called()

    def test_no_chance_does_not_initialize_device_or_submit_draw(self):
        device = Mock()
        client = NativeGroupClient(Mock(), credential_cache=Mock(), device_provider=device)
        client._call = Mock()
        with self.assertRaises(CheckinError) as raised:
            client.operate(Mock(), {}, 'draw', observation(), before_submit=Mock())
        self.assertEqual(raised.exception.code, 'DRAW_UNAVAILABLE')
        device.assert_not_called()
        client._call.assert_not_called()

    def test_draw_uses_the_official_device_ticket_and_preserves_submission_callback(self):
        device = Mock(return_value='synthetic-official-ticket')
        client = NativeGroupClient(Mock(), credential_cache=Mock(), device_provider=device)
        client._call = Mock(return_value={'prizeName':'synthetic-prize'})
        state = observation()
        state['campaign'].update(drawTimes=1, todayAlreadyDrawTimes=0)
        before = Mock()
        client.operate('session', {'id':'a'}, 'draw', state, before_submit=before)
        client._call.assert_called_once_with('session','doDraw',{'riskConstId':'synthetic-official-ticket'},
                                            snapshot={'id':'a'},before_submit=before)

    def test_daily_draw_limit_is_checked_before_device_registration(self):
        device = Mock()
        client = NativeGroupClient(Mock(), credential_cache=Mock(), device_provider=device)
        state = observation()
        state['campaign'].update(drawTimes=2, todayAlreadyDrawTimes=2)
        with self.assertRaises(CheckinError):
            client.operate(Mock(), {}, 'draw', state, before_submit=Mock())
        device.assert_not_called()

    def test_nonzero_draw_reply_is_reconciled_instead_of_assuming_no_chance_was_consumed(self):
        client = NativeGroupClient(lambda:{'phone':'13800138000'}, credential_cache=Mock())
        session = Mock()
        session.post.return_value = SimpleNamespace(status_code=200,json=lambda:{'code':'TEST_NO_WIN','msg':'未中奖'})
        before = Mock()
        with self.assertRaises(CheckinError) as raised:
            client._call(session,'doDraw',{'riskConstId':'synthetic'},snapshot={'phone':'13800138000'},before_submit=before)
        before.assert_called_once()
        self.assertTrue(raised.exception.uncertain)
        self.assertFalse(raised.exception.definitive_rejection)


class DeviceTests(unittest.TestCase):
    def test_concurrent_preparation_shares_one_registration(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event
        cache = CredentialCache()
        devices = GroupDevices(cache)
        snapshot = {'id':'a','phone':'13800138000'}
        entered, release = Event(), Event()
        def register(payload):
            entered.set()
            release.wait(2)
            return {'status':2,'data':'synthetic-concurrent-token'}
        try:
            with patch('recharge.device.DeviceSession._request',side_effect=register) as send:
                with ThreadPoolExecutor(max_workers=5) as executor:
                    first = executor.submit(devices.ensure,snapshot)
                    self.assertTrue(entered.wait(1))
                    others = [executor.submit(devices.ensure,snapshot) for _ in range(4)]
                    release.set()
                    self.assertEqual(first.result(),'synthetic-concurrent-token')
                    self.assertTrue(all(item.result()=='synthetic-concurrent-token' for item in others))
                send.assert_called_once()
        finally:
            release.set()
            devices.close()

    def test_cached_token_survives_client_restart_and_primary_revision_updates(self):
        cache = CredentialCache()
        snapshot = {'id':'a','phone':'13800138000','revision':1}
        key = hashlib.sha256((snapshot['phone']+'|'+APP_ID).encode()).hexdigest()[:24]
        state = {'account_key':key,'app_id':APP_ID,'server':SERVER,'lid':'synthetic-existing-lid',
                 'const_id':'synthetic-token','issued_at':1,'expires_at':None}
        with requests.Session() as http:
            cache.install(DEVICE_SCOPE,snapshot,http,{'state':state},expiry_cookies=())
        with patch('recharge.device.DeviceSession._request') as send:
            first = GroupDevices(cache)
            second = GroupDevices(cache)
            try:
                self.assertEqual(first.ensure(snapshot),'synthetic-token')
                self.assertEqual(second.ensure({**snapshot,'revision':2}),'synthetic-token')
                self.assertFalse(second.ready({'id':'b','phone':'13900139000'}))
                send.assert_not_called()
            finally:
                first.close()
                second.close()

    def test_server_requested_metadata_uses_real_python_host_and_keeps_lid(self):
        cache = CredentialCache()
        devices = GroupDevices(cache)
        snapshot = {'id':'a','phone':'13800138000'}
        try:
            with patch('recharge.device.DeviceSession._request',side_effect=[{'status':-6},{'status':2,'data':'synthetic-accepted-token'}]) as send:
                self.assertEqual(devices.ensure(snapshot),'synthetic-accepted-token')
            first, second = [call.args[0] for call in send.call_args_list]
            self.assertEqual(first['lid'],second['lid'])
            self.assertEqual(second['np'],platform.system())
            self.assertIn('Python/',second['ua'])
            self.assertNotIn('can',second)
            self.assertTrue(devices.ready(snapshot))
        finally:devices.close()

    def test_rejected_registration_keeps_device_identity_without_a_success_token(self):
        cache = CredentialCache()
        devices = GroupDevices(cache)
        snapshot = {'id':'a','phone':'13800138000'}
        try:
            with patch('recharge.device.DeviceSession._request',return_value={'status':-3}):
                with self.assertRaises(CheckinError):devices.ensure(snapshot)
            saved = cache.peek(DEVICE_SCOPE,snapshot)['data']['state']
            self.assertTrue(saved['lid'])
            self.assertFalse(saved['const_id'])
            self.assertFalse(devices.ready(snapshot))
        finally:devices.close()


class DurableSubmissionTests(unittest.TestCase):
    def setUp(self):
        self.storage = Storage()
        self.client = Mock()
        @contextmanager
        def connect():
            yield Mock(), {'phone':'13800138000'}
        self.client.connect = connect
        self.client.inspect.return_value = observation()
        self.client.rules.return_value = '官方规则'
        self.manager = GroupScheduler(lambda: [{'id':'a','status':'active'}],
            lambda key: self.client, self.storage, now=lambda: datetime(2026,11,27,tzinfo=SHANGHAI))

    def test_unknown_create_survives_restart_and_is_reconciled_without_replay(self):
        def submit(*args, **kwargs):
            kwargs['before_submit']()
            self.assertEqual(self.storage.data['pending']['a']['kind'], 'create')
            raise CheckinError('连接中断', 'RESULT_UNCERTAIN', uncertain=True)
        self.client.operate.side_effect = submit
        with self.assertRaises(CheckinError):
            self.manager.run('a', 'create', size=10)
        restarted = GroupScheduler(self.manager.accounts, self.manager.factory, self.storage, now=self.manager.now)
        with self.assertRaises(CheckinError):
            restarted.run('a', 'create', size=10)
        self.client.operate.assert_called_once()
        self.client.inspect.return_value = observation({'groupSize':10,'groupMembers':[{'phone':'138****8000'}]})
        restarted.run('a')
        self.assertFalse(restarted.data['pending'])
        self.client.operate.assert_called_once()

    def test_readback_failure_preserves_submission_and_reports_unknown_result(self):
        def submit(*args, **kwargs):
            kwargs['before_submit']()
        self.client.operate.side_effect = submit
        self.client.inspect.side_effect = [observation(), CheckinError('暂时无法查询', 'UPSTREAM_NETWORK')]
        with self.assertRaises(CheckinError) as raised:
            self.manager.run('a', 'create', size=3)
        self.assertTrue(raised.exception.uncertain)
        self.assertTrue(self.manager.data['pending']['a'])

    def test_draw_consumption_is_reconciled_after_restart_without_replaying_draw(self):
        initial = observation()
        initial['campaign'].update(drawTimes=1,todayAlreadyDrawTimes=0)
        self.client.inspect.return_value = initial
        def submit(*args, **kwargs):
            kwargs['before_submit']()
            raise CheckinError('响应中断','RESULT_UNCERTAIN',uncertain=True)
        self.client.operate.side_effect = submit
        with self.assertRaises(CheckinError):self.manager.run('a','draw')
        after = observation()
        after['campaign'].update(drawTimes=0,todayAlreadyDrawTimes=1)
        self.client.inspect.return_value = after
        restarted = GroupScheduler(self.manager.accounts,self.manager.factory,self.storage,now=self.manager.now)
        restarted.run('a')
        self.assertFalse(restarted.data['pending'])
        self.client.operate.assert_called_once()

    def test_forget_removes_only_that_accounts_activity_state(self):
        self.manager.data['observations'] = {'a':observation(),'b':observation()}
        self.manager.forget('a')
        self.assertEqual(set(self.manager.data['observations']), {'b'})


class RouteTests(unittest.TestCase):
    def test_confirmation_is_required_for_membership_and_batch_changes(self):
        app = Flask(__name__)
        controller = register(app, Mock(), lambda: [], Storage())
        controller.scheduler.run = Mock()
        controller.scheduler.assemble = Mock()
        browser = app.test_client()
        page = browser.get('/native-group')
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'class="workspace-app"', page.data)
        self.assertEqual(browser.post('/api/native-group/run', json={'login_id':'a','kind':'create','size':10}).status_code,400)
        self.assertEqual(browser.post('/api/native-group/assemble',json={'teams':[]}).status_code,400)
        controller.scheduler.run.assert_not_called()
        controller.scheduler.assemble.assert_not_called()
