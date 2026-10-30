from contextlib import contextmanager
from copy import deepcopy
import unittest
from unittest.mock import patch

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
        self.assertEqual(result['bonus_state'], 'unknown')
        self.assertEqual(result['task_progress'], 0)
        self.assertEqual(result['punch_days'], 1)
        self.assertIsNone(result['punched_today'])
        self.assertEqual(self.writes(), [])

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
            return response
        self.session.get = rotate_on_read
        with self.assertRaises(MonthlyCouponError) as error:
            self.client.execute(before_submit=self.before)
        self.assertEqual(error.exception.code, 'APP_CREDENTIALS_CHANGED')
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.submissions, [])

    def test_agreement_is_reported_and_never_signed_automatically(self):
        self.session.response_codes['receiveCoupon'] = {'code': 900, 'msg': '未签署协议'}
        with self.assertRaises(MonthlyCouponError) as error:
            self.client.execute(before_submit=self.before)
        self.assertEqual(error.exception.code, 'AGREEMENT_REQUIRED')
        self.assertEqual(error.exception.rejected_action, 'direct')
        self.assertEqual(error.exception.rejected_period, '202610')
        self.assertEqual(self.writes(), ['receiveCoupon'])

    def test_explicit_already_received_is_a_confirmed_monthly_result(self):
        self.session.response_codes['receiveCoupon'] = {'code': 901, 'msg': '已参与过'}
        self.session.data['expCouponStock'] = 0
        result = self.client.execute(before_submit=self.before)
        self.assertEqual(result['direct_state'], 'claimed')
        self.assertIn('本月已领取', result['message'])

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
