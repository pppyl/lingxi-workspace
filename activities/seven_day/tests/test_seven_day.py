"""Campaign invariants with isolated official-state and transport doubles."""
from copy import deepcopy
from datetime import datetime, timedelta
import base64
import json
import unittest
from unittest.mock import Mock, patch

from cryptography.hazmat.primitives import padding, serialization
from cryptography.hazmat.primitives.asymmetric import padding as rsa_padding, rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from recharge.cmcc import SHANGHAI
from activities.seven_day.client import (SevenDayClient, SevenDayError, DEFAULT_CAMPAIGN,
                              SIGN_ID, DRAW_ID, REWARD_ID, REWARD_SKU,
                              REWARD_CODE, REWARD_RIGHT, PAGE_ID, CHANNEL,
                              _encrypted, _decode, _aes_encrypt, campaign_config)
from activities.seven_day.scheduler import SevenDayScheduler


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


class Storage:
    def __init__(self):
        self.saved = None

    def load_seven_day_data(self):
        return deepcopy(self.saved)

    def save_seven_day_data(self, data):
        self.saved = deepcopy(data)


class SevenDaySchedulerTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 1, 8, tzinfo=SHANGHAI)
        self.pool = [{'id': 'a', 'status': 'active', 'name': 'A', 'credential_revision': 1}]
        self.states = {}
        self.instances = []
        self.events = []
        self.queue, self.storage = Queue(), Storage()
        self.draw_error = None
        self.sign_error = None
        self.connect_error = None
        self.claim_error = None
        self.scheduler = self.create()

    def state(self, login_id):
        return self.states.setdefault(login_id, {'dates': [], 'claimed': False, 'stock': 600000,
            'draw_dates': set(), 'draw_prize': '1元话费兑换券'})

    def create(self):
        parent = self

        class Client:
            def __init__(self, provider):
                self.provider = provider
                self.id = provider()['id']
                self.closed = 0
                parent.instances.append(self)

            def connect(self):
                parent.events.append((self.id, 'connect'))
                if parent.connect_error:
                    raise parent.connect_error
                return self

            def close(self):
                self.closed += 1

            def inspect(self, now):
                parent.events.append((self.id, 'inspect'))
                state, today = parent.state(self.id), now.strftime('%Y%m%d')
                return {'date': now.date().isoformat(), 'days': len(state['dates']),
                    'dates': list(state['dates']), 'total_amount': len(state['dates']),
                    'checked_today': today in state['dates'], 'completed': len(state['dates']) >= 7,
                    'claimed': state['claimed'], 'chances': int(today not in state['draw_dates']),
                    'draw_prize': state['draw_prize'] if today in state['draw_dates'] else '',
                    'eligible': True, 'message': 'official state'}

            def stock(self):
                return parent.state(self.id)['stock']

            def entering(self, kind, before_submit):
                before_submit()
                record = parent.storage.saved['records'][self.id][parent.now.date().isoformat()][kind]
                parent.assertTrue(record['submitted'], 'Intent must be durable before an official mutation')
                parent.events.append((self.id, kind))

            def signin(self, before_submit):
                self.entering('signin', before_submit)
                if parent.sign_error and not parent.sign_error.uncertain:
                    raise parent.sign_error
                state = parent.state(self.id)
                today = parent.now.strftime('%Y%m%d')
                if today not in state['dates']:
                    state['dates'].append(today)
                if parent.sign_error:
                    raise parent.sign_error
                return {}

            def draw(self, before_submit):
                self.entering('draw', before_submit)
                state = parent.state(self.id)
                state['draw_dates'].add(parent.now.strftime('%Y%m%d'))
                if parent.draw_error:
                    raise parent.draw_error
                return {'skuCode': '130000010671', 'skuName': state['draw_prize']}

            def send_reward_sms(self):
                parent.events.append((self.id, 'sms'))

            def claim(self, code, before_submit):
                self.entering('claim', before_submit)
                parent.events.append((self.id, 'code', code))
                parent.state(self.id)['claimed'] = True
                if parent.claim_error:
                    raise parent.claim_error
                return {}

        return SevenDayScheduler(lambda login_id: {'id': login_id},
            lambda: deepcopy(self.pool), storage=self.storage, client_factory=Client,
            now=lambda: self.now, executor=self.queue,
            config_loader=lambda: dict(DEFAULT_CAMPAIGN))

    def actions(self, kind):
        return [event for event in self.events if event[1] == kind]

    def fill_seven_days(self, login_id='a'):
        self.state(login_id)['dates'] = [
            (self.now-timedelta(days=offset)).strftime('%Y%m%d') for offset in reversed(range(7))]

    def test_default_covers_all_active_accounts_without_account_configuration(self):
        self.pool.extend([{'id': 'b', 'status': 'active', 'credential_revision': 1},
                          {'id': 'expired', 'status': 'login_required', 'credential_revision': 1}])
        self.scheduler.tick()
        self.queue.drain()
        self.assertCountEqual(self.actions('signin'), [('a', 'signin'), ('b', 'signin')])
        self.assertCountEqual(self.actions('draw'), [('a', 'draw'), ('b', 'draw')])
        self.assertNotIn('expired', self.scheduler.data['observations'])
        self.assertTrue(self.scheduler.data['settings']['signin_enabled'])
        self.assertTrue(self.scheduler.data['settings']['draw_enabled'])

    def test_new_account_is_automatically_included(self):
        self.scheduler.tick()
        self.queue.drain()
        self.pool.append({'id': 'b', 'status': 'active', 'credential_revision': 1})
        self.scheduler.tick()
        self.queue.drain()
        self.assertEqual(self.actions('signin').count(('b', 'signin')), 1)
        self.assertEqual(self.actions('draw').count(('b', 'draw')), 1)

    def test_daily_success_is_not_repeated_by_poll_manual_or_restart(self):
        self.scheduler.tick()
        self.queue.drain()
        self.now += timedelta(minutes=10)
        self.scheduler.tick()
        self.queue.drain()
        self.scheduler.run('a', 'automatic')
        self.queue.drain()
        self.scheduler = self.create()
        self.scheduler.tick()
        self.queue.drain()
        self.assertEqual(len(self.actions('signin')), 1)
        self.assertEqual(len(self.actions('draw')), 1)

    def test_login_recovery_after_start_time_catches_up_today(self):
        self.pool[0]['status'] = 'login_required'
        self.scheduler.tick()
        self.queue.drain()
        self.assertFalse(self.actions('signin'))
        self.pool[0].update(status='active', credential_revision=2)
        self.scheduler.tick()
        self.queue.drain()
        self.assertEqual(self.actions('signin'), [('a', 'signin')])
        self.assertTrue(self.scheduler.data['observations']['a']['checked_today'])

    def test_ineligible_is_checked_once_daily_across_restart_and_credential_maintenance(self):
        self.connect_error = SevenDayError('不符合活动资格', 'INELIGIBLE', 409)
        self.scheduler.tick()
        self.queue.drain()
        self.now += timedelta(hours=1)
        self.pool[0]['credential_revision'] = 2
        self.scheduler = self.create()
        self.scheduler.update_settings({'burst_count': 6})
        self.scheduler.tick()
        self.queue.drain()
        self.assertEqual(len(self.actions('connect')), 1)
        next_run = datetime.fromisoformat(self.scheduler.status()['accounts'][0]['next_run'])
        self.assertGreater(next_run.date(), self.now.date())
        self.now += timedelta(days=1)
        self.connect_error = None
        self.scheduler.tick()
        self.queue.drain()
        self.assertEqual(len(self.actions('connect')), 2)
        self.assertTrue(self.scheduler.data['observations']['a']['checked_today'])

    def test_automatic_failures_stop_at_five_total_across_restart_refresh_and_revisions(self):
        self.connect_error=SevenDayError('活动连接暂未完成','APP_UID_FAILED')
        for count in range(1,6):
            self.scheduler.tick();self.queue.drain()
            self.assertEqual(len(self.actions('connect')),count)
            self.assertEqual(self.storage.saved['attempts']['a']['2026-11-01']['failed_attempts'],count)
            self.pool[0]['credential_revision']+=1
            self.now+=timedelta(minutes=4,seconds=59)
            self.scheduler.status();self.scheduler.tick();self.queue.drain()
            self.assertEqual(len(self.actions('connect')),count)
            self.now+=timedelta(seconds=1)
            self.scheduler=self.create()
            self.scheduler.update_settings({'burst_count':6})
        self.scheduler.tick();self.queue.drain()
        self.assertEqual(len(self.actions('connect')),5)
        row=self.scheduler.status()['accounts'][0]
        self.assertTrue(row['retry_exhausted'])
        self.assertEqual(datetime.fromisoformat(row['next_run']).date(),(self.now+timedelta(days=1)).date())
        self.now+=timedelta(days=1)
        self.connect_error=None
        self.scheduler.tick();self.queue.drain()
        self.assertEqual(len(self.actions('connect')),6)
        self.assertTrue(self.scheduler.data['observations']['a']['checked_today'])

    def test_custom_retry_interval_and_total_limit_are_persisted_and_manual_retry_remains_available(self):
        self.scheduler.update_settings({'retry_interval_minutes':2,'max_attempts':2})
        self.connect_error=SevenDayError('网络连接失败','NETWORK')
        self.scheduler.tick();self.queue.drain()
        self.now+=timedelta(minutes=1)
        self.scheduler.run('a','automatic');self.queue.drain()
        self.assertEqual(len(self.actions('connect')),1)
        self.now+=timedelta(minutes=1)
        self.scheduler=self.create()
        self.scheduler.tick();self.queue.drain()
        self.assertEqual(len(self.actions('connect')),2)
        self.now+=timedelta(hours=2)
        self.scheduler.tick();self.queue.drain()
        self.assertEqual(len(self.actions('connect')),2)
        self.assertEqual(self.scheduler.data['settings']['retry_interval_minutes'],2)
        self.assertEqual(self.scheduler.data['settings']['max_attempts'],2)
        self.connect_error=None
        self.scheduler.run('a','signin');self.queue.drain()
        self.assertEqual(len(self.actions('signin')),1)

    def test_a_successful_draw_does_not_consume_the_signin_failure_budget(self):
        self.scheduler.update_settings({'signin_enabled':False,'max_attempts':1})
        self.scheduler.tick();self.queue.drain()
        self.assertEqual(len(self.actions('draw')),1)
        self.assertEqual(self.storage.saved['attempts']['a']['2026-11-01']['failed_attempts'],0)
        self.scheduler.update_settings({'signin_enabled':True})
        self.scheduler.tick();self.queue.drain()
        self.assertEqual(len(self.actions('signin')),1)
        self.assertEqual(self.storage.saved['attempts']['a']['2026-11-01']['failed_attempts'],0)

    def test_a_reserved_attempt_survives_restart_before_the_worker_runs(self):
        self.scheduler.update_settings({'max_attempts':1})
        self.scheduler.run('a','automatic')
        self.assertEqual(self.storage.saved['attempts']['a']['2026-11-01']['failed_attempts'],1)
        self.queue.jobs.clear()
        self.scheduler=self.create()
        self.now+=timedelta(hours=1)
        self.scheduler.tick();self.queue.drain()
        self.assertFalse(self.actions('connect'))
        self.assertTrue(self.scheduler.status()['accounts'][0]['retry_exhausted'])

    def test_invalid_retry_settings_do_not_mutate_saved_state(self):
        before=deepcopy(self.scheduler.data)
        for field,value in [('retry_interval_minutes',0),('retry_interval_minutes',1.5),
                            ('retry_interval_minutes',1441),('max_attempts',0),('max_attempts',True),('max_attempts',101)]:
            with self.subTest(field=field,value=value),self.assertRaises(SevenDayError):
                self.scheduler.update_settings({field:value})
        self.assertEqual(self.scheduler.data,before)

    def test_legacy_empty_growth_rejection_is_rechecked_without_repeating_app_signin(self):
        self.scheduler._record('a','check',code='INELIGIBLE',message='官方未返回可参与的网龄资格，请在APP确认')
        self.state('a')['dates']=[self.now.strftime('%Y%m%d')]
        self.scheduler=self.create()
        self.scheduler.tick();self.queue.drain()
        self.assertEqual(len(self.actions('connect')),1)
        self.assertFalse(self.actions('signin'))
        self.assertTrue(self.scheduler.status()['accounts'][0]['observation']['checked_today'])

    def test_ineligible_status_is_explicit_and_successful_manual_query_can_restore_it(self):
        self.connect_error=SevenDayError('不符合活动资格','INELIGIBLE',409)
        self.scheduler.tick();self.queue.drain()
        row=self.scheduler.status()['accounts'][0]
        self.assertFalse(row['observation']['eligible'])
        self.assertIn('今日已暂停',row['retry_message'])
        self.connect_error=None
        self.scheduler.run('a','refresh');self.queue.drain()
        self.assertTrue(self.scheduler.status()['accounts'][0]['observation']['eligible'])

    def test_before_signin_clock_reads_progress_but_does_not_sign(self):
        self.now = self.now.replace(hour=0, minute=0, second=10)
        self.scheduler.tick()
        self.queue.drain()
        self.assertFalse(self.actions('signin'))
        self.assertEqual(self.actions('draw'), [('a', 'draw')])
        self.now = self.now.replace(minute=1)
        self.scheduler.due.clear()
        self.scheduler.tick()
        self.queue.drain()
        self.assertEqual(self.actions('signin'), [('a', 'signin')])

    def test_cross_month_keeps_official_seven_day_progress(self):
        self.state('a')['dates'] = ['20261031']
        self.scheduler.run('a', 'signin')
        self.queue.drain()
        observation = self.scheduler.data['observations']['a']
        self.assertEqual(observation['dates'], ['20261031', '20261101'])
        self.assertEqual(observation['days'], 2)

    def test_completed_cycle_is_not_restarted_but_draw_continues(self):
        self.fill_seven_days()
        self.state('a')['claimed'] = True
        self.scheduler.tick()
        self.queue.drain()
        self.assertFalse(self.actions('signin'))
        self.assertEqual(self.actions('draw'), [('a', 'draw')])

    def test_official_expiry_blocks_scheduler_and_manual_actions(self):
        self.now = datetime(2026, 12, 1, tzinfo=SHANGHAI)
        self.scheduler.tick()
        self.assertFalse(self.queue.jobs)
        self.assertTrue(self.scheduler.metadata()['ended'])
        with self.assertRaises(SevenDayError) as error:
            self.scheduler.run('a', 'signin')
        self.assertEqual(error.exception.code, 'ACTIVITY_CLOSED')
        self.assertFalse(self.events)

    def test_sold_out_ends_both_activities_and_survives_config_refresh(self):
        self.state('a')['stock'] = 0
        self.scheduler.tick()
        self.queue.drain()
        self.assertTrue(self.scheduler.metadata()['ended'])
        self.assertFalse(self.actions('signin'))
        self.assertFalse(self.actions('draw'))
        self.scheduler.config_due = 0
        self.scheduler.tick()
        self.assertFalse(self.queue.jobs)
        with self.assertRaises(SevenDayError):
            self.scheduler.run('a', 'draw')

    def test_forget_clears_persisted_account_and_pending_session(self):
        self.fill_seven_days()
        self.scheduler.send_sms('a')
        session = self.instances[-1]
        self.scheduler._record('a', 'refresh', message='test')
        self.scheduler.due['a'] = self.now
        self.scheduler.revisions['a'] = 1
        self.scheduler.prepared['a'] = self.now.date()
        self.scheduler.forget('a')
        self.assertNotIn('a', self.storage.saved['observations'])
        self.assertNotIn('a', self.storage.saved['records'])
        self.assertNotIn('a', self.scheduler.due)
        self.assertNotIn('a', self.scheduler.revisions)
        self.assertNotIn('a', self.scheduler.prepared)
        self.assertFalse(self.scheduler.busy_login('a'))
        self.assertEqual(session.closed, 1)

    def test_sms_wait_keeps_account_busy_and_claim_uses_same_client(self):
        self.fill_seven_days()
        self.scheduler.send_sms('a')
        session = self.instances[-1]
        self.assertTrue(self.scheduler.busy_login('a'))
        self.assertEqual(session.closed, 0)
        self.pool[0]['credential_revision'] = 2
        self.scheduler.tick()
        self.assertFalse(self.queue.jobs)
        self.scheduler.claim('a', '012345')
        self.assertEqual(len(self.instances), 1)
        self.assertEqual(self.actions('code'), [('a', 'code', '012345')])
        self.assertEqual(session.closed, 1)
        self.assertFalse(self.scheduler.busy_login('a'))
        self.assertTrue(self.scheduler.data['observations']['a']['claimed'])

    def test_sms_reissue_reuses_session_and_expired_session_is_closed(self):
        self.fill_seven_days()
        self.scheduler.send_sms('a')
        self.scheduler.send_sms('a')
        self.assertEqual(len(self.instances), 1)
        self.assertEqual(len(self.actions('sms')), 2)
        self.scheduler.pending['a']['expires'] = 0
        self.assertFalse(self.scheduler.busy_login('a'))
        self.assertEqual(self.instances[0].closed, 1)
        with self.assertRaises(SevenDayError) as error:
            self.scheduler.claim('a', '123456')
        self.assertEqual(error.exception.code, 'SMS_EXPIRED')

    def test_successful_draw_stops_the_burst_after_one_request(self):
        self.scheduler.update_settings({'burst_count': 40, 'burst_interval': .05})
        self.scheduler.run('a', 'draw')
        self.queue.drain()
        self.assertEqual(len(self.actions('draw')), 1)
        record = self.scheduler.history('a')['2026-11-01']['draw']
        self.assertEqual(record['state'], 'success')
        self.assertEqual(record['attempt_count'], 1)
        self.assertTrue(record['confirmed'])

    def test_unknown_draw_result_is_reconciled_before_any_further_request(self):
        self.draw_error = SevenDayError('connection lost', 'NETWORK', uncertain=True)
        self.scheduler.run('a', 'draw')
        self.queue.drain()
        self.assertEqual(len(self.actions('draw')), 1)
        position = self.events.index(('a', 'draw'))
        self.assertIn(('a', 'inspect'), self.events[position+1:])
        self.scheduler.run('a', 'draw')
        self.queue.drain()
        self.assertEqual(len(self.actions('draw')), 1)
        self.assertEqual(self.scheduler.data['observations']['a']['chances'], 0)

    def test_unknown_sign_result_is_queried_and_does_not_repeat_accepted_mark(self):
        self.sign_error = SevenDayError('connection lost', 'NETWORK', uncertain=True)
        self.scheduler.run('a', 'signin')
        self.queue.drain()
        self.assertTrue(self.scheduler.history('a')['2026-11-01']['signin']['uncertain'])
        self.sign_error = None
        self.scheduler.run('a', 'signin')
        self.queue.drain()
        self.assertEqual(len(self.actions('signin')), 1)
        self.assertTrue(self.scheduler.data['observations']['a']['checked_today'])
        record = self.scheduler.history('a')['2026-11-01']['signin']
        self.assertTrue(record['confirmed'])
        self.assertNotIn('uncertain', record)
        self.assertNotIn('code', record)

    def test_official_app_success_reconciles_previous_failure_without_resubmitting(self):
        self.scheduler._record('a', 'signin', submitted=True, code='10', message='领取失败！')
        self.state('a')['dates'] = [self.now.strftime('%Y%m%d')]
        self.scheduler.run('a', 'signin')
        self.queue.drain()
        self.assertFalse(self.actions('signin'))
        record = self.storage.saved['records']['a']['2026-11-01']['signin']
        self.assertTrue(record['confirmed'])
        self.assertNotIn('code', record)
        self.assertEqual(record['message'], '官方已确认今日打卡')

    def test_successful_retry_clears_failure_and_survives_restart(self):
        self.sign_error = SevenDayError('领取失败！', '10')
        self.scheduler.run('a', 'signin')
        self.queue.drain()
        self.sign_error = None
        self.scheduler.run('a', 'signin')
        self.queue.drain()
        self.scheduler = self.create()
        self.scheduler.run('a', 'signin')
        self.queue.drain()
        record = self.scheduler.history('a')['2026-11-01']['signin']
        self.assertTrue(record['confirmed'])
        self.assertNotIn('code', record)
        self.assertNotIn('uncertain', record)
        self.assertEqual(len(self.actions('signin')), 2)

    def test_official_refresh_replaces_obsolete_auth_and_sign_errors(self):
        self.scheduler._record('a', 'signin', confirmed=True, code='10', message='今日已打卡')
        self.scheduler._record('a', 'check', code='APP_UID_FAILED', message='认证未完成')
        self.now += timedelta(seconds=1)
        self.state('a')['dates'] = [self.now.strftime('%Y%m%d')]
        self.scheduler.run('a', 'signin')
        self.queue.drain()
        records = self.scheduler.history('a')['2026-11-01']
        self.assertNotIn('code', records['signin'])
        self.assertNotIn('code', records['check'])
        self.assertFalse(self.actions('signin'))
        self.assertEqual(self.scheduler.status()['accounts'][0]['message'], '已查询官方最新进度')

    def test_failed_signin_does_not_block_independent_daily_draw(self):
        self.sign_error = SevenDayError('官方打卡服务繁忙', 'TEMPORARY')
        self.scheduler.run('a', 'automatic')
        self.queue.drain()
        self.assertEqual(self.actions('signin'), [('a', 'signin')])
        self.assertEqual(self.actions('draw'), [('a', 'draw')])
        record = self.scheduler.history('a')['2026-11-01']
        self.assertEqual(record['signin']['code'], 'TEMPORARY')
        self.assertTrue(record['draw']['confirmed'])
        self.assertFalse(self.scheduler.data['observations']['a']['checked_today'])

    def test_unknown_signin_does_not_block_independent_daily_draw(self):
        self.sign_error = SevenDayError('打卡连接中断', 'NETWORK', uncertain=True)
        self.scheduler.run('a', 'automatic')
        self.queue.drain()
        self.assertEqual(len(self.actions('draw')), 1)
        record = self.scheduler.history('a')['2026-11-01']['signin']
        self.assertTrue(record['confirmed'])
        self.assertNotIn('uncertain', record)
        self.assertTrue(self.scheduler.data['observations']['a']['checked_today'])

    def test_uncertain_claim_releases_sms_session_and_requires_new_query(self):
        self.fill_seven_days()
        self.scheduler.send_sms('a')
        self.claim_error = SevenDayError('connection lost', 'NETWORK', uncertain=True)
        with self.assertRaises(SevenDayError):
            self.scheduler.claim('a', '123456')
        self.assertFalse(self.scheduler.busy_login('a'))
        with self.assertRaises(SevenDayError):
            self.scheduler.claim('a', '123456')
        self.scheduler.run('a', 'refresh')
        self.queue.drain()
        self.assertTrue(self.scheduler.data['observations']['a']['claimed'])
        self.assertEqual(len(self.actions('claim')), 1)

    def test_invalid_code_does_not_consume_or_close_pending_session(self):
        self.fill_seven_days()
        self.scheduler.send_sms('a')
        for code in ('123', '12345x', None, 123456):
            with self.assertRaises(SevenDayError):
                self.scheduler.claim('a', code)
        self.assertTrue(self.scheduler.busy_login('a'))
        self.assertFalse(self.actions('claim'))


class SevenDayClientContractTests(unittest.TestCase):
    def client(self):
        client = SevenDayClient(lambda: {'phone': '18200000001'})
        client.snapshot = {'phone': '18200000001'}
        client.user = {'provinceCode': '851', 'userId': 'official-user'}
        client.post = Mock(return_value={})
        return client

    def test_signin_and_draw_use_distinct_activity_ids_on_confirmed_official_route(self):
        client = self.client()
        before = object()
        client.signin(before)
        client.post.assert_called_once_with('activities/prize/templatePrize',
            {'activityId': SIGN_ID}, mutation=True, before_submit=before)
        client.post.reset_mock()
        client.draw(before)
        client.post.assert_called_once_with('activities/prize/templatePrize',
            {'activityId': DRAW_ID, 'touchCode': CHANNEL}, mutation=True, before_submit=before)

    def test_signin_records_intent_before_plain_official_request(self):
        material = {'phone': '18200000001', 'cookie': 'UID=u; JSESSIONID=s', 'credential_revision': 1}
        client = SevenDayClient(lambda: dict(material))
        client.snapshot = dict(material)
        client.client = Mock()
        client.client.post.return_value.status_code = 200
        client.client.post.return_value.json.return_value = {'code': '0', 'data': {'prizeCode': 'daily'}}
        before = Mock()
        events = Mock()
        events.attach_mock(before, 'intent')
        events.attach_mock(client.client.post, 'post')
        # Metadata maintenance may advance the revision without replacing login.
        material['credential_revision'] = 2
        self.assertEqual(client.signin(before), {'prizeCode': 'daily'})
        self.assertEqual([call[0] for call in events.mock_calls], ['intent', 'post', 'post().json'])
        client.client.post.assert_called_once()
        args, kwargs = client.client.post.call_args
        self.assertEqual(args, ('https://dev.coc.10086.cn/coc/activities/prize/templatePrize',))
        self.assertEqual(kwargs['json'], {'activityId': SIGN_ID})
        self.assertEqual(kwargs['headers'], {})
        material['cookie'] = 'UID=new; JSESSIONID=new'
        with self.assertRaises(SevenDayError) as error:
            client.signin(before)
        self.assertEqual(error.exception.code, 'APP_CREDENTIALS_CHANGED')
        before.assert_called_once()
        client.client.post.assert_called_once()

    def test_growth_source_stays_distinct_from_observed_business_channel(self):
        # Official entry query channelId is P00000129418; index bundle's
        # growth query uses its own fixed touchCode P00000001090.
        material = {'phone': '18200000001', 'cookie': 'UID=official-uid; JSESSIONID=session',
                    'credential_revision': 1}
        client = SevenDayClient(lambda: dict(material), session_factory=Mock)
        client.post = Mock(side_effect=[{'userId': 'u', 'provinceCode': '851'}, {'level': 'V3'}])
        flow = Mock()
        flow.call.return_value = (None, {'retCode': '000000'}, None)
        with patch('activities.seven_day.client.AppLoginSession.from_credentials', return_value=flow), \
                patch('activities.seven_day.client.encrypt_uid', return_value='official-encrypted-uid'):
            client.connect()
        self.assertEqual(CHANNEL, 'P00000129418')
        args, _ = client.post.call_args
        self.assertEqual(args, ('activities/growth/query',
            {'touchCode': 'P00000001090', 'provinceCode': '851'}))
        self.assertNotEqual(args[1]['touchCode'], CHANNEL)
        client.post.reset_mock(side_effect=True)
        client.draw()
        self.assertEqual(client.post.call_args.args[1]['touchCode'], 'P00000129418')

    def test_successful_empty_growth_level_is_not_a_local_eligibility_rejection(self):
        material={'phone':'18200000001','cookie':'UID=official-uid; JSESSIONID=session','credential_revision':1}
        client=SevenDayClient(lambda:dict(material),session_factory=Mock)
        client.post=Mock(side_effect=[{'userId':'u','provinceCode':'851'},{'level':''}])
        flow=Mock();flow.call.return_value=(None,{'retCode':'000000'},None)
        with patch('activities.seven_day.client.AppLoginSession.from_credentials',return_value=flow), \
                patch('activities.seven_day.client.encrypt_uid',return_value='official-encrypted-uid'):
            self.assertIs(client.connect(),client)
        self.assertEqual(client.post.call_count,2)
        self.assertEqual(client.post.call_args.args[0],'activities/growth/query')

    def test_seventh_day_claim_uses_dynamic_sms_with_fixed_official_reward_ids(self):
        client = self.client()
        client.claim('012345')
        args, kwargs = client.post.call_args
        self.assertEqual(args[0], 'activities/prize/templatePrize/new')
        self.assertEqual(args[1]['lotteryId'], REWARD_ID)
        self.assertEqual(args[1]['provinceCode'], '851')
        self.assertEqual(json.loads(args[1]['jsonExtParam']), {
            'activitySkuId': REWARD_SKU, 'smsCode': '012345', 'productCode': REWARD_CODE,
            'touchCode': CHANNEL, 'rightSkuId': REWARD_RIGHT})
        self.assertEqual(kwargs['encryption'], 'wheel')
        self.assertTrue(kwargs['mutation'])

    def test_reward_sms_is_specific_to_this_reward_and_page(self):
        client = self.client()
        client.send_reward_sms()
        client.post.assert_called_once_with('user/smsCode', {
            'mobile': '18200000001', 'userId': 'official-user', 'smsCodeTypeEnum': '505',
            'productCode': REWARD_CODE, 'pageId': PAGE_ID, 'touchCode': CHANNEL},
            encryption='aurora', mutation=True)

    def test_meteor_and_aurora_crypto_package_round_trip(self):
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public = base64.b64encode(private.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)).decode()
        payload = {'amount': '5.0', 'name': '七日活动', 'pageId': PAGE_ID}
        with patch('activities.seven_day.client.PUBLIC_KEY', public):
            for kind in ('meteor', 'aurora'):
                with self.subTest(kind=kind):
                    body, headers, key = _encrypted(payload, kind)
                    self.assertRegex(key, rb'^[0-9a-f]{16}$')
                    if kind == 'meteor':
                        wrapped = private.decrypt(base64.b64decode(headers['coc-meteor']), rsa_padding.PKCS1v15())
                        self.assertEqual(base64.b64decode(wrapped), key)
                        self.assertEqual(headers['coc-meteor-v'], '2')
                        ciphertext = base64.b64decode(base64.b64decode(body['cocEnContent']))
                    else:
                        wrapped = private.decrypt(base64.b64decode(headers['coc-aurora']), rsa_padding.PKCS1v15())
                        self.assertEqual(wrapped, key)
                        self.assertEqual(headers['coc-aurora-v'], '2')
                        self.assertEqual(headers['Coc-En-Version'], '1486234121417629696')
                        ciphertext = base64.b64decode(body['cocEnContent'])
                    decrypt = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
                    raw = decrypt.update(ciphertext)+decrypt.finalize()
                    unpad = padding.PKCS7(128).unpadder()
                    self.assertEqual(json.loads(unpad.update(raw)+unpad.finalize()), payload)
        reply = {'code': '0', 'data': {'valid': True}}
        self.assertEqual(_decode({'returnData': _aes_encrypt(reply, key)}, key), {'valid': True})

    def test_campaign_uses_current_official_config_instead_of_rule_text_end_date(self):
        session = Mock()
        response = session.get.return_value
        response.text = 'var configData = '+json.dumps({'configItemList': [
            {'configContent_0': '2026/10/31 09:00:00'},
            {'configContent_1': '2026/11/30 23:59:59'}]})+'\n'
        result = campaign_config(session=session)
        self.assertEqual(result['end_at'], '2026-11-30T23:59:59+08:00')
        self.assertIn('v', session.get.call_args.kwargs['params'])
        session.close.assert_not_called()


if __name__ == '__main__':
    unittest.main()
