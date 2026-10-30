import copy
import json
import unittest
from unittest.mock import Mock, patch

from curl_cffi import requests

from monthly_coupon_auth import APP_TOKEN_PATH, PLATFORM, MonthlyCouponAuthError, guizhou_session


ENTRY = PLATFORM + '/static/activity/fixture/index.html?ChannelPosition=TEST'
SNAPSHOT = {'phone': '13800000000', 'cookie': 'JSESSIONID=fake', 'app_context': {}, 'credential_revision': 1}


class Response:
    status_code = 200

    def __init__(self, data):
        self.data = data

    def json(self):
        return self.data


class Session:
    def __init__(self):
        self.headers, self.calls, self.closed = {}, [], False
        self.cookies = Mock()
        self.response = Response({'code': 200, 'data': {'token': 'activity-token', 'serviceNum': SNAPSHOT['phone']}})

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response

    def close(self):
        self.closed = True


class MonthlyCouponAuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.current = copy.deepcopy(SNAPSHOT)
        self.flow, self.session = Mock(), Session()
        self.flow.call.return_value = (200, {'retCode': '000000', 'rspBody': {'token': 'app-token'}}, {})
        self.from_credentials = patch('monthly_coupon_auth.AppLoginSession.from_credentials', return_value=self.flow).start()
        self.addCleanup(patch.stopall)

    def context(self, entry=ENTRY):
        return guizhou_session(lambda: copy.deepcopy(self.current), entry, session_factory=lambda: self.session)

    def test_app_token_is_exchanged_only_with_guizhou_login(self):
        with self.context() as session:
            self.assertIs(session, self.session)
            self.assertEqual(session.headers['AuthorizationAweb'], 'Bearer activity-token')
        self.assertTrue(self.session.closed)
        self.flow.call.assert_called_once_with(APP_TOKEN_PATH, {'sourceId': '030011', 'url': ENTRY, 'cellNum': SNAPSHOT['phone']})
        self.assertEqual(self.session.calls[0][0], PLATFORM+'/login')
        body = self.session.calls[0][1]['json']
        self.assertEqual((body['type'], body['key']), (4, 'app-token'))
        self.assertEqual(body['refParamRecord']['cnlPos'], 'TEST')
        self.assertFalse(self.session.calls[0][1]['allow_redirects'])
        self.assertNotIn('JSESSIONID', json.dumps(body))

    def test_json_string_app_response_is_supported(self):
        self.flow.call.return_value = (200, {'retCode': '000000', 'rspBody': '{"token":"app-token"}'}, {})
        with self.context():
            pass

    def test_cross_origin_or_outside_platform_entry_is_rejected_before_authentication(self):
        for entry in ['http://wap.gz.10086.cn/activityWeb/index.html', 'https://elsewhere.test/activityWeb/index.html',
                      'https://wap.gz.10086.cn.other.test/activityWeb/x', 'https://wap.gz.10086.cn/account',
                      'https://wap.gz.10086.cn/activityWeb/../account']:
            with self.subTest(entry=entry), self.assertRaises(MonthlyCouponAuthError):
                with self.context(entry):
                    pass
        self.from_credentials.assert_not_called()

    def test_missing_shared_login_never_requests_app_token(self):
        self.current = None
        with self.assertRaises(MonthlyCouponAuthError):
            with self.context():
                pass
        self.from_credentials.assert_not_called()

    def test_rotation_during_token_request_prevents_provincial_login(self):
        def rotate(*args):
            self.current['credential_revision'] += 1
            return 200, {'retCode': '000000', 'rspBody': {'token': 'app-token'}}, {}
        self.flow.call.side_effect = rotate
        with self.assertRaises(MonthlyCouponAuthError) as raised:
            with self.context():
                pass
        self.assertEqual(raised.exception.code, 'APP_CREDENTIALS_CHANGED')
        self.assertFalse(self.session.calls)
        self.assertTrue(self.session.closed)

    def test_wrong_account_is_rejected_without_exposing_token(self):
        self.session.response.data['data']['serviceNum'] = '13900000000'
        with self.assertRaises(MonthlyCouponAuthError) as raised:
            with self.context():
                pass
        self.assertEqual(raised.exception.code, 'ACCOUNT_MISMATCH')
        self.assertNotIn('activity-token', str(raised.exception))
        self.assertTrue(self.session.closed)

    def test_app_failure_is_not_sent_to_provincial_login(self):
        self.flow.call.return_value = (200, {'retCode': '410000', 'rspBody': {}}, {})
        with self.assertRaises(MonthlyCouponAuthError):
            with self.context():
                pass
        self.assertFalse(self.session.calls)

    def test_provincial_network_failure_closes_session(self):
        self.session.post = Mock(side_effect=requests.RequestsError('offline'))
        with self.assertRaises(MonthlyCouponAuthError) as raised:
            with self.context():
                pass
        self.assertEqual(raised.exception.code, 'UPSTREAM_NETWORK')
        self.assertTrue(self.session.closed)

    def test_provincial_rejection_does_not_reveal_server_message(self):
        self.session.response.data = {'code': 401, 'msg': 'secret app-token'}
        with self.assertRaises(MonthlyCouponAuthError) as raised:
            with self.context():
                pass
        self.assertNotIn('app-token', str(raised.exception))


if __name__ == '__main__':
    unittest.main()
