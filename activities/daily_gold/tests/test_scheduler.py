from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from flask import Flask

from feature_runtime import feature_scope
from recharge.cmcc import SHANGHAI
from activities.daily_gold.client import DailyGoldError, DEFAULT_CAMPAIGN
from activities.daily_gold.scheduler import DailyGoldScheduler, DEFAULT_SETTINGS, SCOPE
from activities.daily_gold.plugin import register


class InlineExecutor:
    def submit(self, function, *args):
        function(*args)

    def shutdown(self, **kwargs):
        pass


class MemoryStorage:
    def __init__(self):
        self.value = None

    def load_activity_data(self, scope):
        assert scope == SCOPE
        return deepcopy(self.value)

    def save_activity_data(self, scope, value):
        assert scope == SCOPE
        self.value = deepcopy(value)


class DailyGoldSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.clock = datetime(2026, 11, 6, 0, 0, 1, tzinfo=SHANGHAI)
        self.elapsed = 0
        self.accounts = [{'id':'a', 'name':'0314', 'phone_masked':'182****0314', 'status':'active', 'credential_revision':1}]
        self.storage = MemoryStorage()
        self.calls, self.outcomes, self.observations = [], [], []
        self.monotonic = patch('activities.daily_gold.scheduler.time.monotonic', side_effect=lambda:self.elapsed)
        self.monotonic.start()
        self.addCleanup(self.monotonic.stop)

    def progress(self, signed=False, remaining=1):
        return {'date':self.clock.date().isoformat(), 'signed':signed, 'remaining':remaining,
                'prizes':[{'name':'1元话费券', 'status':'已发放', 'received_at':self.clock.isoformat()}] if signed else []}

    def factory(self, provider):
        owner = self

        class Client:
            def connect(self):
                owner.calls.append(('connect', provider()['id']))
                return self

            def observe(self):
                owner.calls.append(('observe', provider()['id']))
                value = owner.observations.pop(0) if owner.observations else owner.progress()
                if isinstance(value, Exception):
                    raise value
                return value

            def signin(self):
                owner.calls.append(('signin', provider()['id'], owner.clock))
                value = owner.outcomes.pop(0) if owner.outcomes else owner.progress(True, 0)
                if callable(value):
                    value = value()
                if isinstance(value, Exception):
                    raise value
                return value

            def close(self):
                owner.calls.append(('close', provider()['id']))

        return Client()

    def wait(self, duration):
        self.elapsed += duration
        self.clock += timedelta(seconds=duration)
        return False

    def scheduler(self, **kwargs):
        scheduler = DailyGoldScheduler(lambda owner:{'id':owner, 'phone':'13800138000'},
            lambda:deepcopy(self.accounts), storage=self.storage, client_factory=self.factory,
            executor=InlineExecutor(), config_loader=lambda:deepcopy(DEFAULT_CAMPAIGN),
            now=lambda:self.clock, wait=self.wait, **kwargs)
        self.addCleanup(scheduler.shutdown)
        return scheduler

    def test_defaults_and_time_precision_include_late_start_without_early_submission(self):
        self.clock = self.clock.replace(second=0, microsecond=999999)
        scheduler = self.scheduler()
        self.assertEqual(scheduler.status()['settings'], DEFAULT_SETTINGS)
        scheduler.tick()
        self.assertFalse(self.calls)
        self.clock += timedelta(microseconds=1)
        scheduler.tick()
        self.assertEqual([call[0] for call in self.calls].count('signin'), 1)
        self.assertTrue(scheduler.status()['accounts'][0]['observation']['signed'])
        self.assertIn('1元话费券', str(scheduler.status()['accounts'][0]['observation']['prizes']))
        scheduler.tick()
        self.assertEqual([call[0] for call in self.calls].count('connect'), 1)

    def test_failure_retries_use_one_session_and_stop_immediately_on_success(self):
        self.outcomes = [DailyGoldError('暂未开始','NOT_STARTED'), DailyGoldError('稍后再试','BUSY'), self.progress(True,0)]
        scheduler = self.scheduler()
        scheduler.tick()
        sends = [call for call in self.calls if call[0] == 'signin']
        self.assertEqual(len(sends), 3)
        self.assertAlmostEqual((sends[1][2]-sends[0][2]).total_seconds(),1, places=4)
        self.assertAlmostEqual((sends[2][2]-sends[0][2]).total_seconds(),2, places=4)
        self.assertEqual([call[0] for call in self.calls].count('connect'),1)
        self.assertEqual(scheduler.data['attempts']['a']['2026-11-06']['count'],3)

    def test_daily_failure_budget_survives_restart_and_credential_revision_change(self):
        self.outcomes = [DailyGoldError('服务繁忙','BUSY') for _ in range(5)]
        scheduler = self.scheduler()
        scheduler.tick()
        self.assertEqual([call[0] for call in self.calls].count('signin'),5)
        self.accounts[0]['credential_revision'] += 1
        scheduler.tick()
        restarted = self.scheduler()
        restarted.tick()
        self.assertEqual([call[0] for call in self.calls].count('signin'),5)
        self.assertEqual(restarted.data['attempts']['a']['2026-11-06']['count'],5)
        self.assertIn('2026-11-07T00:00:01', restarted.status()['accounts'][0]['next_run'])

    def test_uncertain_submission_is_read_once_and_never_replayed_when_still_unconfirmed(self):
        self.outcomes = [DailyGoldError('提交超时','TIMEOUT',uncertain=True)]
        scheduler = self.scheduler()
        scheduler.tick()
        self.assertEqual([call[0] for call in self.calls].count('signin'),1)
        self.assertEqual([call[0] for call in self.calls].count('observe'),2)
        self.assertEqual(scheduler.data['attempts']['a']['2026-11-06']['state'],'uncertain')
        scheduler.tick()
        scheduler.run('a','signin')
        self.assertEqual([call[0] for call in self.calls].count('signin'),1)

    def test_uncertain_submission_is_resolved_by_official_confirmation(self):
        self.outcomes = [DailyGoldError('提交超时','TIMEOUT',uncertain=True)]
        self.observations = [self.progress(),self.progress(True,0)]
        scheduler = self.scheduler()
        scheduler.tick()
        self.assertEqual(scheduler.data['attempts']['a']['2026-11-06']['state'],'signed')
        self.assertEqual(scheduler.failure_count(),0)
        scheduler.tick()
        self.assertEqual([call[0] for call in self.calls].count('signin'),1)

    def test_interrupted_durable_intent_is_not_replayed_after_restart(self):
        scheduler = self.scheduler()
        scheduler._record('a','2026-11-06','running','提交中',submitted=True,uncertain=True,counted=True)
        restarted = self.scheduler()
        restarted.run('a','signin')
        self.assertEqual([call[0] for call in self.calls].count('signin'),0)
        self.assertEqual(restarted.data['attempts']['a']['2026-11-06']['state'],'uncertain')

    def test_ineligibility_and_rate_limit_stop_for_the_day_even_after_manual_failed_query(self):
        for code in ('INELIGIBLE','RATE_LIMITED'):
            with self.subTest(code=code):
                self.storage.value=None;self.calls=[]
                self.outcomes=[DailyGoldError('请求受限',code)]
                scheduler=self.scheduler();scheduler.tick()
                self.observations=[DailyGoldError('临时故障','BUSY')]
                scheduler.run('a','refresh');scheduler.login_restored('a');scheduler.tick()
                self.assertEqual([call[0] for call in self.calls].count('signin'),1)
                self.assertEqual(scheduler.data['attempts']['a']['2026-11-06']['blocked'],
                                 'ineligible' if code=='INELIGIBLE' else 'rate_limited')

    def test_completed_daily_progress_prevents_authentication_on_tick_or_login_restore(self):
        scheduler=self.scheduler();scheduler._observe('a',self.progress(True,0))
        scheduler.login_restored('a');scheduler.tick();scheduler.run('a','signin')
        self.assertFalse(self.calls)
        self.assertEqual(scheduler.failure_count(),0)

    def test_all_active_accounts_are_covered_and_late_restoration_is_caught_up(self):
        self.clock=self.clock.replace(hour=15)
        self.accounts.append({'id':'b','name':'1621','phone_masked':'182****1621','status':'login_required'})
        scheduler=self.scheduler();scheduler.tick()
        self.assertEqual([call[1] for call in self.calls if call[0]=='signin'],['a'])
        self.accounts[1]['status']='active';scheduler.tick()
        self.assertEqual([call[1] for call in self.calls if call[0]=='signin'],['a','b'])

    def test_login_notification_received_during_refresh_is_processed_afterward(self):
        scheduler=self.scheduler()
        original=self.factory
        def factory(provider):
            client=original(provider);observe=client.observe
            def read():
                scheduler.login_restored('a')
                return observe()
            client.observe=read
            return client
        scheduler.client_factory=factory
        scheduler.run('a','refresh')
        scheduler.client_factory=original;scheduler.tick()
        self.assertEqual([call[0] for call in self.calls].count('signin'),1)

    def test_status_and_metadata_never_construct_clients_or_call_config_loader(self):
        scheduler=self.scheduler();scheduler.client_factory=Mock(side_effect=AssertionError('network'))
        scheduler.config_loader=Mock(side_effect=AssertionError('network'))
        for _ in range(3):
            scheduler.status();scheduler.metadata();scheduler.failure_count()
        scheduler.client_factory.assert_not_called();scheduler.config_loader.assert_not_called()

    def test_expiry_and_disabled_automation_prevent_business_work(self):
        scheduler=self.scheduler();scheduler.update_settings({**DEFAULT_SETTINGS,'enabled':False})
        scheduler.tick();self.assertFalse(self.calls)
        self.clock=datetime.fromisoformat(DEFAULT_CAMPAIGN['end_at'])+timedelta(seconds=1)
        scheduler.tick();self.assertFalse(self.calls)
        with self.assertRaises(DailyGoldError) as caught:
            scheduler.run('a','signin')
        self.assertEqual(caught.exception.code,'ACTIVITY_CLOSED')

    def test_feature_switch_off_between_rounds_stops_later_requests(self):
        scheduler=self.scheduler();permission=SimpleNamespace(allowed=lambda:permission.enabled,enabled=True)
        def first():
            permission.enabled=False
            return DailyGoldError('服务繁忙','BUSY')
        self.outcomes=[first]
        with feature_scope(permission):
            scheduler.tick()
        self.assertEqual([call[0] for call in self.calls].count('signin'),1)

    def test_no_chance_does_not_invent_signed_or_prize_success(self):
        self.observations=[self.progress(False,0)]
        scheduler=self.scheduler();scheduler.tick()
        row=scheduler.status()['accounts'][0]
        self.assertFalse(row['observation']['signed'])
        self.assertEqual(row['observation']['prizes'],[])
        self.assertEqual(row['last_result']['state'],'completed')
        self.assertEqual([call[0] for call in self.calls].count('signin'),0)

    def test_removed_account_data_is_deleted_from_storage_and_status(self):
        scheduler=self.scheduler();scheduler.tick();self.accounts=[];scheduler.tick()
        for key in ('observations','records','attempts'):
            self.assertNotIn('a',self.storage.value[key])
        self.assertEqual(scheduler.status()['accounts'],[])

    def test_setting_validation_rejects_non_finite_or_wrong_types_without_erasing_budget(self):
        scheduler=self.scheduler();scheduler.tick();before=deepcopy(scheduler.data['attempts'])
        for payload in ({**DEFAULT_SETTINGS,'interval_seconds':float('nan')},
                        {**DEFAULT_SETTINGS,'repeat_count':True}, {**DEFAULT_SETTINGS,'start_time':'00:00'}):
            with self.assertRaises(DailyGoldError):scheduler.update_settings(payload)
        self.assertEqual(scheduler.data['attempts'],before)

    def test_routes_use_existing_cache_and_get_status_is_read_only(self):
        app=Flask('daily-gold-tests');cache=Mock();app.extensions['credential_cache']=cache
        controller=register(app,lambda owner:{'id':owner,'phone':'13800138000'},lambda:deepcopy(self.accounts),None)
        self.addCleanup(controller.stop)
        self.assertIs(controller.cache,cache)
        controller.scheduler.client_factory=Mock(side_effect=AssertionError('GET cannot call network'))
        controller.scheduler.config_loader=Mock(side_effect=AssertionError('GET cannot call network'))
        response=app.test_client().get('/api/daily-gold/status')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json['settings'],DEFAULT_SETTINGS)
        self.assertEqual(app.test_client().post('/api/daily-gold/run',json=[]).status_code,400)
        controller.deleted=True
        self.assertEqual(app.test_client().get('/api/daily-gold/status').status_code,410)


if __name__ == '__main__':
    unittest.main()
