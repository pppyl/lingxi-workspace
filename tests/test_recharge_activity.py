import unittest
from datetime import datetime
from unittest.mock import Mock

from recharge.app import create_app
from recharge.cmcc import (ActivityClient, ActivityError, SHANGHAI, availability,
                  parse_server_time, validate_entry)


class AvailabilityTests(unittest.TestCase):
    def state(self, value, province='100', rd=1):
        return availability({'provinceCode': province}, {'currentTime': value, 'rdStatus': rd})

    def test_server_clock_controls_opening(self):
        self.assertFalse(self.state('2026-10-27 20:00:00')['can_claim'])
        self.assertFalse(self.state('2026-10-28 07:59:59')['can_claim'])
        self.assertTrue(self.state('2026-10-28 08:00:00')['can_claim'])

    def test_xinjiang_and_tibet_open_later(self):
        for province in ['991', '891']:
            self.assertFalse(self.state('2026-10-28 09:59:59', province)['can_claim'])
            self.assertTrue(self.state('2026-10-28 10:00:00', province)['can_claim'])

    def test_timezone_and_millisecond_input(self):
        expected = datetime(2026, 10, 28, 8, 0, tzinfo=SHANGHAI)
        for value in [expected.timestamp() * 1000, '20261028080000', '2026-10-28T00:00:00Z']:
            self.assertEqual(parse_server_time(value), expected)

    def test_unknown_time_or_status_never_authorizes(self):
        for value in [None, True, '', 'nonsense']:
            self.assertFalse(self.state(value)['can_claim'])
        for rd in [0, 2, 3, 4, 5, 6, 7, 10, None, 'bad']:
            self.assertFalse(self.state('2026-10-28 08:01:00', rd=rd)['can_claim'])

    def test_existing_prize_blocks_submission(self):
        result = availability({}, {'currentTime': '2026-10-28 08:01:00', 'rdStatus': 1,
                                   'getPrize': {'sendStatus': 'UNUSED'}})
        self.assertFalse(result['can_claim'])

    def test_entry_origin_is_fixed(self):
        for value in ['http://wx.10086.cn/', 'https://evil.test/',
                      'https://wx.10086.cn@evil.test/', 'https://wx.10086.cn/other',
                      'https://wx.10086.cn:443/hlwyxhdhub/act-wedrecharge/1024101716']:
            with self.assertRaises(ActivityError):
                validate_entry(value)


class ClaimTests(unittest.TestCase):
    def setUp(self):
        self.activity = ActivityClient()
        self.activity.mode = 'cookie'
        self.activity.const_id = 'official-device-id'
        self.user = {'phone': '13800138000', 'provinceCode': '100'}
        self.draw = {'currentTime': '2026-10-28 08:01:00', 'rdStatus': 1}
        self.submissions = []

        def call(endpoint, body=None):
            if endpoint == '_pub/rec_login':
                return {'code': '0', 'data': self.user}
            if endpoint == 'queryDrawPrizeInfo':
                return {'code': '0', 'data': self.draw}
            if endpoint == 'drawPrize':
                self.submissions.append(body)
                return {'code': '0', 'data': {'actPrizes': [{'prizeName': '充值券',
                        'shortName': '13元', 'cardNo': 'private-card-number'}]}}
            self.fail(endpoint)
        self.activity.call = Mock(side_effect=call)

    def test_exactly_one_official_submission_and_no_card_secret(self):
        result = self.activity.claim()
        self.assertEqual(self.submissions, [{'constId': 'official-device-id'}])
        self.assertNotIn('cardNo', result['prizes'][0])
        with self.assertRaises(ActivityError):
            self.activity.claim()
        self.assertEqual(len(self.submissions), 1)

    def test_device_and_time_gate_are_enforced_on_server(self):
        self.activity.const_id = ''
        with self.assertRaises(ActivityError):
            self.activity.claim()
        self.assertFalse(self.submissions)
        self.activity.const_id = 'official-device-id'
        self.draw['currentTime'] = '2026-10-28 07:59:59'
        with self.assertRaises(ActivityError):
            self.activity.claim()
        self.assertFalse(self.submissions)

    def test_auth_failure_never_calls_draw(self):
        self.activity.call = Mock(return_value={'code': 'GOTO_LOGIN', 'msg': '需要去登录'})
        with self.assertRaises(ActivityError):
            self.activity.claim()
        self.activity.call.assert_called_once_with('_pub/rec_login')

    def test_auth_failure_from_claim_itself_is_not_automatically_replayed(self):
        original = self.activity.call.side_effect
        def expired_at_submit(endpoint, body=None):
            if endpoint == 'drawPrize':
                self.submissions.append(body)
                return {'code':'GOTO_LOGIN','msg':'会话需要重连'}
            return original(endpoint, body)
        self.activity.call.side_effect = expired_at_submit
        self.activity.app_material = {'cookie':'JSESSIONID=private','phone':'19900000001','profile':{}}
        self.activity.renew_app_session = Mock()
        with self.assertRaises(ActivityError): self.activity.claim(automatic=True)
        self.assertEqual(len(self.submissions),1)
        self.activity.renew_app_session.assert_not_called()

    def test_timeout_is_uncertain_and_is_never_retried(self):
        original = self.activity.call.side_effect

        def fail_on_draw(endpoint, body=None):
            if endpoint == 'drawPrize':
                self.submissions.append(body)
                raise ActivityError('连接超时', 'UPSTREAM_NETWORK', 502)
            return original(endpoint, body)
        self.activity.call.side_effect = fail_on_draw
        with self.assertRaises(ActivityError):
            self.activity.claim()
        self.assertTrue(self.activity.uncertain)
        self.activity.last_claim -= 4
        with self.assertRaises(ActivityError):
            self.activity.claim()
        self.assertEqual(len(self.submissions), 1)

    def test_phone_is_masked(self):
        self.assertEqual(self.activity.identity()['phone'], '138****8000')

    def test_automatic_timeout_checks_result_before_another_post(self):
        original = self.activity.call.side_effect

        def timed_out(endpoint, body=None):
            if endpoint == 'drawPrize':
                self.submissions.append(body)
                self.draw.update(rdStatus=4, getPrize={'shortName': '13元', 'sendStatus': 'UNUSED'})
                raise ActivityError('结果超时', 'UPSTREAM_NETWORK', 502)
            return original(endpoint, body)
        self.activity.call.side_effect = timed_out
        with self.assertRaises(ActivityError):
            self.activity.claim(automatic=True)
        result = self.activity.claim(automatic=True)
        self.assertTrue(result['already_received'])
        self.assertEqual(len(self.submissions), 1)
        self.assertEqual(self.activity.total_submissions, 1)

    def test_automatic_retry_can_repeat_only_while_official_status_still_allows(self):
        original = self.activity.call.side_effect
        draws = 0

        def transient(endpoint, body=None):
            nonlocal draws
            if endpoint == 'drawPrize':
                draws += 1
                if draws == 1:
                    return {'code': '160002', 'msg': '暂时无库存'}
            return original(endpoint, body)
        self.activity.call.side_effect = transient
        with self.assertRaises(ActivityError):
            self.activity.claim(automatic=True)
        result = self.activity.claim(automatic=True)
        self.assertTrue(result['success'])
        self.assertEqual(draws, 2)
        self.assertEqual(self.activity.call.call_args_list[-2].args[0], 'queryDrawPrizeInfo')

    def test_automatic_stops_when_official_round_already_consumed(self):
        self.draw['rdStatus'] = 2
        with self.assertRaises(ActivityError) as raised:
            self.activity.claim(automatic=True)
        self.assertEqual(raised.exception.code, 'ROUND_COMPLETED')
        self.assertFalse(self.submissions)


class WebBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.activity = ActivityClient()
        self.app = create_app(self.activity)
        self.app.testing = True
        self.client = self.app.test_client()
        self.client.get('/')
        with self.client.session_transaction() as state:
            self.csrf = state['csrf']

    def test_unlogged_claim_is_not_forwarded(self):
        self.activity.http.post = Mock()
        response = self.client.post('/api/claim', json={}, headers={'X-CSRF-Token': self.csrf})
        self.assertEqual(response.status_code, 401)
        self.activity.http.post.assert_not_called()

    def test_csrf_is_required(self):
        response = self.client.post('/api/login', json={})
        self.assertEqual(response.status_code, 403)

    def test_cross_origin_and_dns_rebinding_rejected(self):
        self.assertEqual(self.client.get('/api/status', headers={'Host': 'evil.test'}).status_code, 403)
        self.assertEqual(self.client.get('/api/status', headers={'Origin': 'https://evil.test'}).status_code, 403)

    def test_json_object_is_required(self):
        response = self.client.post('/api/import', json=[], headers={'X-CSRF-Token': self.csrf})
        self.assertEqual(response.status_code, 400)

    def test_response_cannot_be_framed_or_cached(self):
        response = self.client.get('/')
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertIn("frame-ancestors 'none'", response.headers['Content-Security-Policy'])


if __name__ == '__main__':
    unittest.main()
