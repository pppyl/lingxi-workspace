import unittest
from unittest.mock import Mock

from flask import Flask

from coupon_captcha import APP_ID, OFFICIAL_ORIGIN, register_coupon_captcha
from http_transport import requests


class Upstream:
    def __init__(self):
        self.status_code = 200
        self.content = b'official public script bytes'
        self.headers = {'Content-Type': 'application/javascript', 'Set-Cookie': 'external=private'}


class Session:
    def __init__(self):
        self.calls = []
        self.response = Upstream()
        self.error = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if self.error:
            raise self.error
        return self.response


class CouponCaptchaTests(unittest.TestCase):
    def setUp(self):
        self.session = Session()
        app = Flask(__name__)
        self.wallet = Mock()
        self.wallet.wallet_sdk.return_value = self.session.response
        app.extensions['coupon_uses'] = self.wallet
        register_coupon_captcha(app, session_factory=lambda: self.session)
        self.client = app.test_client()

    def test_public_sdk_resource_keeps_bytes_query_real_origin_and_user_agent(self):
        response = self.client.get('/coupon-verification/mgt/decision-captcha/zw-captcha/libs/greenseer.js?t=123',
            base_url='http://127.0.0.1:5500', headers={'Origin': 'http://127.0.0.1:5500', 'User-Agent': 'Browser-Test'})
        self.assertEqual(response.data, self.session.response.content)
        method, url, options = self.session.calls[0]
        self.assertEqual(method, 'GET')
        self.assertEqual(url, OFFICIAL_ORIGIN+'/mgt/decision-captcha/zw-captcha/libs/greenseer.js?t=123')
        self.assertEqual(options['headers']['Origin'], 'http://127.0.0.1:5500')
        self.assertEqual(options['headers']['User-Agent'], 'Browser-Test')
        self.assertFalse(options['allow_redirects'])
        self.assertNotIn('Set-Cookie', response.headers)

    def test_verify_form_is_forwarded_verbatim_without_local_credentials(self):
        body = b'sid=example%2Bsession&token=human%20input&x=1&x=2'
        self.client.set_cookie('session', 'local-login')
        response = self.client.post('/coupon-verification/mgt/decision-captcha/api/v1?debug=0',
            base_url='http://localhost:5500', data=body,
            content_type='application/x-www-form-urlencoded', headers={'Authorization': 'local-secret'})
        self.assertEqual(response.status_code, 200)
        _, _, options = self.session.calls[0]
        self.assertEqual(options['data'], body)
        self.assertEqual(options['headers']['Origin'], 'http://localhost:5500')
        self.assertNotIn('Cookie', options['headers'])
        self.assertNotIn('Authorization', options['headers'])

    def test_unknown_paths_target_urls_and_traversal_never_contact_upstream(self):
        for path in ['https://other.example/file.js', 'mgt/decision-captcha/../admin.js',
                     'mgt/decision-captcha/%2e%2e/admin.js', 'mgt/decision-captcha/%252e%252e/admin.js',
                     'mgt/decision-captcha/zw-captcha/a\\b.js', 'mgt/decision-captcha/zw-captcha/a.html',
                     'mgt/decision-captcha/api/logo/not-the-app', 'mgt/decision-captcha/admin',
                     'mgt/decision-engine/udid/other']:
            with self.subTest(path=path):
                self.assertEqual(self.client.get('/coupon-verification/'+path).status_code, 404)
        self.assertEqual(self.session.calls, [])

    def test_wallet_sdk_preserves_actual_browser_origin_and_official_device_headers(self):
        self.client.set_cookie('_zw_kvani5r', 'real-browser-lid')
        self.client.set_cookie('session', 'private-local-login')
        response = self.client.get('/coupon-verification/wallet/udid/c1?operation_id=wallet-op&sdk_arg=preserved',
            base_url='http://localhost:5500', headers={'Origin': 'http://localhost:5500',
                'appId': APP_ID, 'param': 'real-sdk-output', 'User-Agent': 'Actual-Browser',
                'Cookie': 'session=private', 'Authorization': 'private'})
        self.assertEqual(response.status_code, 200)
        operation_id, method, query, headers, body, lid = self.wallet.wallet_sdk.call_args.args
        self.assertEqual((operation_id, method, query, lid), ('wallet-op', 'GET', 'sdk_arg=preserved', 'real-browser-lid'))
        self.assertEqual(headers['Origin'], 'http://localhost:5500')
        self.assertEqual(headers['appId'], APP_ID)
        self.assertEqual(headers['param'], 'real-sdk-output')
        self.assertNotIn('Cookie', headers)
        self.assertNotIn('Authorization', headers)
        self.assertEqual(self.session.calls, [])
        self.assertNotIn('Set-Cookie', response.headers)
        self.client.get('/coupon-verification/wallet/index-private-web_a54cc0c.js')
        self.assertEqual(self.session.calls[-1][1], 'https://touch.10086.cn/i/mobile/component/index-private-web_a54cc0c.js')
        self.assertEqual(self.client.post('/coupon-verification/wallet/index-private-web_a54cc0c.js').status_code, 405)

    def test_apply_verify_and_static_methods_are_separate(self):
        for name in ('a', 'a2', 'a3', 'p1'):
            self.assertEqual(self.client.get('/coupon-verification/mgt/decision-captcha/api/'+name).status_code, 200)
            self.assertEqual(self.client.post('/coupon-verification/mgt/decision-captcha/api/'+name).status_code, 405)
        for name in ('v1', 'v2', 'v3'):
            self.assertEqual(self.client.get('/coupon-verification/mgt/decision-captcha/api/'+name).status_code, 405)
            self.assertEqual(self.client.post('/coupon-verification/mgt/decision-captcha/api/'+name).status_code, 200)
        self.assertEqual(self.client.get('/coupon-verification/mgt/decision-captcha/api/logo/'+APP_ID).status_code, 200)
        self.assertEqual(self.client.post('/coupon-verification/mgt/decision-captcha/zw-captcha/index.js').status_code, 405)

    def test_upstream_rejection_is_preserved_and_redirect_is_never_followed(self):
        self.session.response.status_code = 403
        self.session.response.content = b'{"error":"origin rejected"}'
        response = self.client.get('/coupon-verification/mgt/decision-captcha/zw-captcha/index.js')
        self.assertEqual((response.status_code, response.data), (403, self.session.response.content))
        self.session.response.status_code = 302
        self.session.response.headers['Location'] = 'https://other.example/'
        response = self.client.get('/coupon-verification/mgt/decision-captcha/zw-captcha/index.js')
        self.assertEqual(response.status_code, 502)
        self.assertNotIn('Location', response.headers)

    def test_network_error_does_not_echo_challenge_parameters(self):
        self.session.error = requests.RequestsError('private challenge parameter')
        response = self.client.get('/coupon-verification/mgt/decision-captcha/api/a')
        self.assertEqual(response.status_code, 502)
        self.assertNotIn('private', response.get_data(as_text=True))


if __name__ == '__main__':
    unittest.main()
