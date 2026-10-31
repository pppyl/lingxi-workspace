from contextlib import contextmanager
from copy import deepcopy
import unittest
from unittest.mock import patch
from urllib.parse import quote

from curl_cffi import requests

from monthly_coupon_client import ENTRY, MonthlyCouponClient, MonthlyCouponError, TASKS


class Response:
    def __init__(self, value, *, date='Thu, 29 Oct 2026 15:37:52 GMT', status=200):
        self.value, self.status_code = value, status
        self.headers = {'Date': date} if date else {}

    def json(self):
        return self.value


class Session:
    def __init__(self):
        self.data = {'signNum': 1, 'couponStock': 431212, 'expCouponStock': 122060,
                     'grcExpTask': {'taskOne': '0', 'taskTwo': '0', 'taskThree': '0', 'taskFour': '0'}}
        self.calls, self.failure, self.clock = [], None, 'Thu, 29 Oct 2026 15:37:52 GMT'
        self.response_codes = {}
        self.agreement_response = Response({'code':200})
        self.clear_agreement = True

    def get(self, url, **kwargs):
        path = url.rsplit('/', 1)[-1]
        self.calls.append((path, kwargs))
        if path in ('receiveCoupon', 'receiveExpCoupon', 'saveExpTask') and self.failure:
            raise self.failure
        if path in self.response_codes:
            return Response(self.response_codes[path], date=self.clock)
        if path == 'initInfo':
            return Response({'code': 200, 'data': deepcopy(self.data)}, date=self.clock)
        if path == 'prizeList':
            return Response({'code': 200, 'data': [{'resourceName': '2元话费券', 'createTime': None,
                                                  'type': None, 'cycleType': None, 'resourceId': '108982'}]}, date=self.clock)
        if path == 'saveExpTask':
            task_type = kwargs['params']['taskType']
            if task_type == '4':
                self.data['signNum'] = min(3, self.data['signNum'] + 1)
                if self.data['signNum'] == 3:
                    self.data['grcExpTask']['taskFour'] = '1'
            else:
                self.data['grcExpTask'][{'1': 'taskOne', '2': 'taskTwo', '3': 'taskThree'}[task_type]] = '1'
            return Response({'code': 200, 'data': {}}, date=self.clock)
        if path in ('receiveCoupon', 'receiveExpCoupon'):
            return Response({'code': 200, 'data': {'directionsForUse': '<b>2元话费满减券</b> 充满20元可用'}}, date=self.clock)
        raise AssertionError(path)

    def post(self, url, **kwargs):
        assert url.endswith('/grcNew/signAgr')
        self.calls.append(('signAgr', kwargs))
        if isinstance(self.agreement_response, Exception):
            raise self.agreement_response
        if self.clear_agreement and self.agreement_response.value.get('code') == 200:
            for path,value in list(self.response_codes.items()):
                agreement=value.get('data',{})
                if isinstance(agreement.get('data'),dict):agreement=agreement['data']
                if agreement.get('cfmLogId') == kwargs['json']['cfmLogId']:
                    self.response_codes.pop(path)
        return self.agreement_response


class MonthlyCouponClientTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = {'cookie': 'JSESSIONID=test', 'phone': '19900000001', 'credential_revision': 1}
        self.session, self.visits, self.submissions = Session(), [], []
        @contextmanager
        def provider(credentials, entry):
            self.assertEqual(entry, ENTRY)
            yield self.session
        self.client = MonthlyCouponClient(lambda: deepcopy(self.snapshot), session_provider=provider,
                                          visitor=lambda url: self.visits.append(url))

    def before(self, action, period):
        self.submissions.append((action, period))
        return True

    def writes(self):
        return [name for name, _ in self.session.calls if name in ('receiveCoupon', 'saveExpTask', 'receiveExpCoupon')]

    def test_read_only_does_not_claim_or_invent_current_month_from_history(self):
        result = self.client.inspect()
        self.assertEqual(result['month'], '202610')
        self.assertEqual(result['date'], '20261029')
        self.assertEqual(result['direct_state'], 'unknown')
        self.assertEqual(result['bonus_state'], 'pending')
        self.assertEqual(result['task_progress'], 0)
        self.assertEqual(result['punch_days'], 1)
        self.assertIsNone(result['punched_today'])
        self.assertEqual(self.writes(), [])

    def test_configured_entry_and_channel_are_shared_by_authentication_and_activity_requests(self):
        entry = ENTRY.split('?', 1)[0]+'?ChannelPosition=NEW_POSITION&referChannels=NEW_CHANNEL&actId=campaign'
        @contextmanager
        def provider(credentials, target):
            self.assertEqual(target, entry)
            yield self.session
        client = MonthlyCouponClient(lambda:deepcopy(self.snapshot), entry_url=entry, session_provider=provider)
        client.inspect()
        self.assertEqual(self.writes(), [])
        for _, kwargs in self.session.calls:
            self.assertEqual(kwargs['params']['refCnl'],'NEW_CHANNEL')
            self.assertEqual(kwargs['params']['cnlPos'],'NEW_POSITION')
            self.assertEqual(kwargs['params']['actId'],'campaign')
            self.assertEqual(kwargs['params']['fullUrl'],quote(entry,safe="~()*!.'-"))

    def test_executes_official_task_types_and_real_landing_pages(self):
        result = self.client.execute(before_submit=self.before)
        self.assertEqual(self.submissions, [('direct', '202610'), ('bill', '202610'), ('signin', '202610'),
                                           ('age', '202610'), ('punch', '20261029')])
        self.assertEqual(self.visits, [row[4] for row in TASKS])
        self.assertEqual(result['direct_state'], 'claimed')
        self.assertEqual(result['task_progress'], 60)
        self.assertEqual(result['punch_days'], 2)
        self.assertTrue(result['punched_today'])
        self.assertNotIn('receiveExpCoupon', self.writes())

    def test_callback_refusal_prevents_every_duplicate_write(self):
        result = self.client.execute(before_submit=lambda action, period: False)
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.visits, [])
        self.assertIn('未重复发送', result['message'])
        self.assertEqual(result['direct_state'], 'unknown')

    def test_confirmed_monthly_receipt_and_today_punch_survive_task_updates(self):
        result = self.client.execute(before_submit=self.before, knowledge={
            'month':'202610','date':'20261029','direct_state':'claimed','direct_prize':'confirmed coupon',
            'punched_today':True})
        self.assertEqual([action for action,_ in self.submissions],['bill','signin','age'])
        self.assertEqual(result['direct_state'],'claimed')
        self.assertEqual(result['direct_prize'],'confirmed coupon')
        self.assertTrue(result['punched_today'])
        self.assertEqual(self.visits,[row[4] for row in TASKS])

    def test_old_unknown_bonus_does_not_override_current_waiting_task_progress(self):
        self.session.data['grcExpTask'].update(taskOne='1',taskTwo='1',taskThree='1')
        result=self.client.execute(before_submit=lambda *args:False,knowledge={
            'month':'202610','date':'20261029','bonus_state':'unknown'})
        self.assertEqual(result['task_progress'],60)
        self.assertEqual(result['bonus_state'],'pending')
        self.assertEqual(self.writes(),[])

    def test_old_pending_bonus_does_not_hide_newly_completed_task_progress(self):
        self.session.data.update(signNum=3,grcExpTask={k:'1' for k in ['taskOne','taskTwo','taskThree','taskFour']})
        result=self.client.execute(before_submit=lambda *args:False,knowledge={
            'month':'202610','date':'20261029','bonus_state':'pending'})
        self.assertEqual(result['task_progress'],100)
        self.assertEqual(result['bonus_state'],'unknown')
        self.assertEqual(self.writes(),[])

    def test_bonus_only_after_three_days_and_complete_official_progress(self):
        self.session.data.update(signNum=3, grcExpTask={k: '1' for k in ['taskOne', 'taskTwo', 'taskThree', 'taskFour']})
        result = self.client.execute(before_submit=self.before)
        self.assertEqual(self.submissions, [('direct', '202610'), ('bonus', '202610')])
        self.assertEqual(result['task_progress'], 100)
        self.assertEqual(result['bonus_state'], 'claimed')
        self.assertNotIn('saveExpTask', self.writes())

    def test_last_punch_rechecks_progress_before_bonus(self):
        self.session.data.update(signNum=2, grcExpTask={'taskOne': '1', 'taskTwo': '1', 'taskThree': '1', 'taskFour': '0'})
        result = self.client.execute(before_submit=self.before)
        self.assertEqual(self.submissions, [('direct', '202610'), ('punch', '20261029'), ('bonus', '202610')])
        self.assertEqual(result['bonus_state'], 'claimed')

    def test_empty_stock_does_not_dispatch_any_mutation(self):
        self.session.data.update(couponStock=0, expCouponStock=0)
        self.client.execute(before_submit=self.before)
        self.assertEqual(self.writes(), [])

    def test_unknown_stock_does_not_authorize_a_write(self):
        self.session.data.pop('couponStock')
        self.session.data.pop('expCouponStock')
        self.client.execute(before_submit=self.before)
        self.assertEqual(self.writes(), [])

    def test_missing_official_time_prevents_writes(self):
        self.session.clock = ''
        with self.assertRaises(MonthlyCouponError) as error:
            self.client.execute(before_submit=self.before)
        self.assertEqual(error.exception.code, 'SERVER_TIME_MISSING')
        self.assertEqual(self.writes(), [])

    def test_rotation_before_network_write_uses_no_stale_credentials(self):
        original = self.session.get
        def rotate_on_read(url, **kwargs):
            response = original(url, **kwargs)
            if url.endswith('/prizeList'):
                self.snapshot['credential_revision'] += 1
                self.snapshot['cookie'] = 'JSESSIONID=new-login'
            return response
        self.session.get = rotate_on_read
        with self.assertRaises(MonthlyCouponError) as error:
            self.client.execute(before_submit=self.before)
        self.assertEqual(error.exception.code, 'APP_CREDENTIALS_CHANGED')
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.submissions, [])

    def test_metadata_only_maintenance_does_not_block_an_authorized_task(self):
        get = self.session.get
        def maintain_on_read(url, **kwargs):
            result = get(url, **kwargs)
            if url.endswith('/prizeList'):
                self.snapshot['credential_revision'] += 1
            return result
        self.session.get = maintain_on_read
        result = self.client.execute(before_submit=self.before)
        self.assertEqual(result['direct_state'],'claimed')
        self.assertIn('receiveCoupon',self.writes())

    def test_incomplete_agreement_is_not_submitted(self):
        self.session.response_codes['receiveCoupon'] = {'code': 900, 'msg': '未签署协议'}
        with self.assertRaises(MonthlyCouponError) as error:
            self.client.execute(before_submit=self.before)
        self.assertEqual(error.exception.code, 'AGREEMENT_FORMAT')
        self.assertEqual(error.exception.rejected_action, 'direct')
        self.assertEqual(error.exception.rejected_period, '202610')
        self.assertEqual(self.writes(), ['receiveCoupon'])

    def require_agreement(self, stage):
        self.session.response_codes[stage]={'code':901 if stage=='initInfo' else 900,
            'data':{'code':200,'data':{'agreementName':'话费券活动协议','agreementContent':'<p>免费参与本活动，领取话费券。</p>',
                    'cfmLogId':'log-'+stage}}}

    def test_direct_claim_accepts_official_agreement_in_same_session_then_continues_tasks(self):
        self.require_agreement('receiveCoupon')
        result=self.client.execute(before_submit=self.before)
        signs=[kwargs for path,kwargs in self.session.calls if path=='signAgr']
        self.assertEqual(len(signs),1)
        self.assertEqual(signs[0]['json']['cfmLogId'],'log-receiveCoupon')
        self.assertEqual(signs[0]['json']['id'],'')
        self.assertEqual(signs[0]['json']['bizName'],'首页')
        self.assertEqual(self.submissions.count(('direct','202610')),1)
        self.assertEqual(self.writes().count('receiveCoupon'),2)
        self.assertEqual(result['direct_state'],'claimed')
        self.assertTrue(result['punched_today'])
        self.assertIn('活动协议已自动确认',result['message'])

    def test_entry_agreement_is_accepted_only_during_execution_not_status_read(self):
        self.require_agreement('initInfo')
        with self.assertRaises(MonthlyCouponError) as caught:self.client.inspect()
        self.assertEqual(caught.exception.code,'AGREEMENT_REQUIRED')
        self.assertFalse(any(path=='signAgr' for path,_ in self.session.calls))
        result=self.client.execute(before_submit=self.before)
        self.assertEqual(result['direct_state'],'claimed')
        self.assertEqual(sum(path=='signAgr' for path,_ in self.session.calls),1)

    def test_bonus_claim_can_accept_its_own_agreement(self):
        self.session.data.update(signNum=3,grcExpTask={k:'1' for k in ('taskOne','taskTwo','taskThree','taskFour')})
        self.require_agreement('receiveExpCoupon')
        result=self.client.execute(before_submit=self.before)
        self.assertEqual(result['bonus_state'],'claimed')
        self.assertEqual(self.submissions.count(('bonus','202610')),1)
        self.assertEqual(sum(path=='signAgr' for path,_ in self.session.calls),1)

    def test_protocol_still_rejected_after_acceptance_never_loops(self):
        self.require_agreement('receiveCoupon');self.session.clear_agreement=False
        with self.assertRaises(MonthlyCouponError) as caught:self.client.execute(before_submit=self.before)
        self.assertEqual(caught.exception.code,'AGREEMENT_FAILED')
        self.assertEqual(sum(path=='signAgr' for path,_ in self.session.calls),1)
        self.assertEqual(self.writes(),['receiveCoupon','receiveCoupon'])

    def test_uncertain_agreement_does_not_send_another_claim_and_releases_rejected_claim_intent(self):
        self.require_agreement('receiveCoupon')
        self.session.agreement_response=requests.RequestsError('timeout')
        with self.assertRaises(MonthlyCouponError) as caught:self.client.execute(before_submit=self.before)
        self.assertEqual(caught.exception.code,'AGREEMENT_UNCERTAIN')
        self.assertEqual(caught.exception.rejected_action,'direct')
        self.assertFalse(caught.exception.uncertain)
        self.assertEqual(self.writes(),['receiveCoupon'])

    def test_confirmed_claim_remains_available_when_a_later_task_fails(self):
        self.client.visitor=lambda url:(_ for _ in ()).throw(MonthlyCouponError('落地页访问失败','TASK_VISIT_FAILED'))
        with self.assertRaises(MonthlyCouponError):self.client.execute(before_submit=self.before)
        self.assertEqual(self.client.observation['direct_state'],'claimed')
        self.assertEqual(self.writes().count('receiveCoupon'),1)

    def test_monthly_limit_response_does_not_claim_a_coupon_was_issued(self):
        self.session.response_codes['receiveCoupon'] = {'code': 901, 'msg': '已参与过'}
        self.session.data['expCouponStock'] = 0
        result = self.client.execute(before_submit=self.before)
        self.assertEqual(result['direct_state'], 'participated')
        self.assertEqual(result['direct_prize'], '')
        self.assertEqual(result['direct_receipt'], {'source':'claim_response','code':901,'month':'202610'})
        self.assertIn('奖品名称与发券结果待核实', result['message'])
        self.assertNotIn('本月已领取', result['message'])
        self.assertNotIn('领取成功', result['message'])

    def test_confirmed_monthly_limit_is_not_resubmitted_on_the_next_execution(self):
        self.session.response_codes['receiveCoupon']={'code':901,'msg':'已参与过'}
        self.session.data['expCouponStock']=0
        first=self.client.execute(before_submit=self.before)
        self.client.execute(before_submit=self.before,knowledge=first)
        self.assertEqual(self.writes().count('receiveCoupon'),1)

    def test_successful_claim_keeps_actual_prize_and_success_receipt(self):
        result=self.client.execute(before_submit=self.before)
        self.assertEqual(result['direct_state'],'claimed')
        self.assertIn('2元话费满减券',result['direct_prize'])
        self.assertEqual(result['direct_receipt']['code'],200)

    def test_network_timeout_never_replays_the_mutation(self):
        self.session.failure = requests.RequestsError('timeout')
        with self.assertRaises(MonthlyCouponError) as error:
            self.client.execute(before_submit=self.before)
        self.assertTrue(error.exception.uncertain)
        self.assertEqual(self.writes(), ['receiveCoupon'])

    def test_already_punched_today_does_not_increase_counter(self):
        self.session.data.update(grcExpTask={'taskOne': '1', 'taskTwo': '1', 'taskThree': '1', 'taskFour': '0'})
        self.session.response_codes['saveExpTask'] = {'code': 500, 'msg': '今日已签到'}
        result = self.client.execute(before_submit=self.before)
        self.assertTrue(result['punched_today'])
        self.assertEqual(result['punch_days'], 1)

    def test_beijing_month_boundary_uses_official_date(self):
        self.session.clock = 'Sat, 31 Oct 2026 16:00:01 GMT'
        result = self.client.execute(before_submit=self.before)
        self.assertEqual(result['month'], '202611')
        self.assertIn(('direct', '202611'), self.submissions)
        self.assertIn(('punch', '20261101'), self.submissions)

    def test_landing_visitor_has_no_guizhou_auth_and_rejects_foreign_redirect(self):
        class Visitor:
            def __init__(self):
                self.headers, self.calls = {}, []
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def get(self, url, **kwargs):
                self.calls.append(url)
                response = Response({}, status=302)
                response.headers['Location'] = 'https://unrelated.example/task'
                return response
        visitor = Visitor()
        with patch('monthly_coupon_client.requests.Session', return_value=visitor):
            with self.assertRaises(MonthlyCouponError) as error:
                MonthlyCouponClient._visit(TASKS[0][4])
        self.assertEqual(error.exception.code, 'TASK_REDIRECT')
        self.assertEqual(visitor.calls, [TASKS[0][4]])
        self.assertNotIn('AuthorizationAweb', visitor.headers)

    def test_no_protocol_method_can_sign_an_agreement(self):
        with self.assertRaises(ValueError):
            self.client._get(self.session, 'signAgr')
        self.assertEqual(self.session.calls, [])


if __name__ == '__main__':
    unittest.main()
