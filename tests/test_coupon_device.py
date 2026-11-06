from concurrent.futures import ThreadPoolExecutor
from http.cookiejar import CookieJar
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from coupon_device import (APP_ID, DOMAIN, LID_COOKIE, SERVER, TOKEN_COOKIE,
                           CouponDevice, CouponDeviceError, decode_param, encode_param)
from http_transport import requests


class Client:
    def __init__(self, *responses):
        self.headers = {'User-Agent': 'Native wallet fixture', 'Referer': 'https://touch.10086.cn/i/mobile/mycoupons.html',
                        'payPhoneNo': 'DO_NOT_SEND', 'X-Requested-With': 'DO_NOT_SEND'}
        self.cookies = requests.Cookies(CookieJar())
        self.cookies.set('JSESSIONID_CMCC', 'BOUND_WALLET_SESSION', domain=DOMAIN, path='/')
        self.responses = list(responses)
        self.calls = []
        self.entered = None
        self.release = None

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs, dict(self.headers)))
        if self.entered:
            self.entered.set()
            self.release.wait(2)
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return SimpleNamespace(status_code=200, json=lambda: value)


def saved_state(token=''):
    return {'account_key': 'account-a', 'domain': DOMAIN, 'app_id': APP_ID,
            'lid': 'LOCAL_ONLY_FIXTURE', 'lid_type': 2, 'token': token}


class CouponDeviceTests(unittest.TestCase):
    def test_retained_sdk_known_wire_vectors_cover_cesu8_and_json_numbers(self):
        self.assertEqual(encode_param({'x': '中😀'}), 'j6J4BCF2HP2QtUDLtM2SB_g=')
        self.assertEqual(encode_param({'lid': 'LOCAL_ONLY_FIXTURE', 'lidType': 1, 'cache': True, 'appKey': APP_ID}),
            'j6JTUvz2q2JV3g-03NLc3kelhglJvNRYoko2PDJTUvRojh0EBCFePDJCGv-FlaBAdbJ8law2Gh0wafYHBCF2l3N5q3Bf-30kVCS6-C-kVpmkG5opVCdE-pBp-fG8V3G2Wz==')
        self.assertEqual(encode_param({'n': 1.0, 'small': 0.000001, 'tiny': 1e-7, 'large': 1e21, 'minus': -0.0}),
            'j6JXBCFePDJpMvNTMDBAVD4wVOSwVOZTB_RuM_k2qCNEP3rTB5emr5dEBCFelaT6Vaw2MvEXdhV2qC0L')
        self.assertEqual(decode_param('j6J4BCF2HP2QtUDLtM2SB_g='), {'x': '中😀'})

    def test_cache_true_preflight_returns_only_real_c1_data_in_same_wallet_session(self):
        for status in (1, 2):
            with self.subTest(status=status):
                client = Client({'status': status, 'data': 'SERVER_DATA_FIXTURE'})
                headers = client.headers
                device = CouponDevice('account-a', client, state=saved_state())
                self.assertEqual(device.prepare(), 'SERVER_DATA_FIXTURE')
                method, url, options, sdk_headers = client.calls[0]
                self.assertEqual((method, url), ('GET', SERVER))
                self.assertEqual(decode_param(sdk_headers['param']),
                    {'lid': 'LOCAL_ONLY_FIXTURE', 'lidType': 2, 'cache': True, 'appKey': APP_ID})
                self.assertFalse(options['allow_redirects'])
                self.assertNotIn('payPhoneNo', sdk_headers)
                self.assertNotIn('X-Requested-With', sdk_headers)
                self.assertNotIn('Origin', sdk_headers)
                self.assertIs(client.headers, headers)
                self.assertEqual(client.cookies.get('JSESSIONID_CMCC'), 'BOUND_WALLET_SESSION')
                cookie = next(item for item in client.cookies.jar if item.name == TOKEN_COOKIE)
                self.assertEqual((cookie.value, cookie.domain, cookie.path, cookie.secure),
                    ('SERVER_DATA_FIXTURE', DOMAIN, '/i', True))

    def test_stored_token_survives_app_cookie_changes_and_restart_without_guessing_expiry(self):
        client = Client()
        device = CouponDevice('account-a', client, state=saved_state('SERVER_DATA_FIXTURE'))
        self.assertEqual(device.prepare(), 'SERVER_DATA_FIXTURE')
        client.cookies.set('APP_COOKIE_VERSION', 'new', domain=DOMAIN, path='/')
        restored = CouponDevice('account-a', client, state=device.export_state())
        self.assertEqual(restored.prepare(), 'SERVER_DATA_FIXTURE')
        self.assertEqual(client.calls, [])

    def test_foreign_account_and_authentication_domain_are_rejected(self):
        for field, value in [('account_key', 'another-account'), ('domain', 'yxfk.market.chinamobile.com:8081'), ('app_id', 'another-app')]:
            state = {**saved_state('SERVER_DATA_FIXTURE'), field: value}
            with self.subTest(field=field), self.assertRaises(ValueError):
                CouponDevice('account-a', Client(), state=state)

    def test_existing_lid_cookie_is_reused_and_decoded_using_official_cookie_rules(self):
        client = Client({'status': 1, 'data': 'SERVER_DATA_FIXTURE'})
        client.cookies.set(LID_COOKIE, 'EXISTING%2BLID', domain=DOMAIN, path='/')
        device = CouponDevice('account-a', client)
        device.prepare()
        payload = decode_param(client.calls[0][3]['param'])
        self.assertEqual((payload['lid'], payload['lidType']), ('EXISTING+LID', 1))
        self.assertEqual(client.cookies.get(LID_COOKIE), 'EXISTING%2BLID')

    def test_new_local_id_follows_sdk_format_but_is_never_used_as_token(self):
        client = Client({'status': -6})
        device = CouponDevice('account-a', client, clock=lambda: 1790000000.125)
        with self.assertRaises(CouponDeviceError) as caught:
            device.prepare()
        self.assertEqual(caught.exception.code, 'FINGERPRINT_REQUIRED')
        payload = decode_param(client.calls[0][3]['param'])
        self.assertRegex(payload['lid'], r'^1790000000125[0-9a-zA-Z]{32}$')
        self.assertEqual(payload['lidType'], '0')
        self.assertEqual(device.token, '')
        self.assertFalse(any(cookie.name == TOKEN_COOKIE for cookie in client.cookies.jar))

    def test_detect_requires_genuine_provider_and_uses_exact_second_stage_status(self):
        client = Client({'status': -4, 'data': 'SERVER_CORRECTED_LID'}, {'status': 2, 'data': 'REAL_SECOND_STAGE_FIXTURE'})
        provider = Mock(return_value={'ua': 'Native wallet fixture', 'ct': 3})
        device = CouponDevice('account-a', client, state=saved_state(), fingerprint_provider=provider)
        self.assertEqual(device.prepare(), 'REAL_SECOND_STAGE_FIXTURE')
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(decode_param(client.calls[1][3]['param']), {'lid': 'SERVER_CORRECTED_LID',
            'lidType': 1, 'cache': True, 'ua': 'Native wallet fixture', 'ct': 3, 'appKey': APP_ID})
        provider.assert_called_once_with()
        client = Client({'status': -6}, {'status': 1, 'data': 'NOT_A_DETECT_SUCCESS'})
        device = CouponDevice('account-a', client, state=saved_state(), fingerprint_provider=provider)
        with self.assertRaises(CouponDeviceError):
            device.prepare()
        self.assertEqual(device.token, '')

    def test_rate_limit_and_explicit_authentication_failure_do_not_start_detect(self):
        for status in (-800, -3, -5):
            provider = Mock()
            client = Client({'status': status})
            device = CouponDevice('account-a', client, state=saved_state(), fingerprint_provider=provider)
            with self.subTest(status=status), self.assertRaises(CouponDeviceError):
                device.prepare()
            provider.assert_not_called()
            self.assertEqual(len(client.calls), 1)

    def test_network_failure_restores_headers_and_never_generates_fallback_token(self):
        client = Client(RuntimeError('offline fixture'))
        headers = client.headers
        device = CouponDevice('account-a', client, state=saved_state())
        with self.assertRaisesRegex(RuntimeError, 'offline fixture'):
            device.prepare()
        self.assertIs(client.headers, headers)
        self.assertEqual(device.token, '')
        self.assertEqual(len(client.calls), 1)

    def test_invalid_success_data_and_boolean_status_never_become_token(self):
        for response in ({'status': 1, 'data': 'bad;cookie'}, {'status': 2}, {'status': True, 'data': 'fixture'}):
            with self.subTest(response=response), self.assertRaises(CouponDeviceError):
                CouponDevice('account-a', Client(response), state=saved_state()).prepare()

    def test_simultaneous_preparation_merges_one_c1_request(self):
        client = Client({'status': 1, 'data': 'ONE_SERVER_DATA_FIXTURE'})
        client.entered, client.release = Event(), Event()
        device = CouponDevice('account-a', client, state=saved_state())
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(device.prepare) for _ in range(4)]
            self.assertTrue(client.entered.wait(2))
            client.release.set()
            self.assertEqual([future.result() for future in futures], ['ONE_SERVER_DATA_FIXTURE']*4)
        self.assertEqual(len(client.calls), 1)

    def test_explicit_invalidation_retains_lid_and_never_replays_submission(self):
        client = Client({'status': 1, 'data': 'NEW_SERVER_DATA_FIXTURE'})
        device = CouponDevice('account-a', client, state=saved_state('OLD_SERVER_DATA_FIXTURE'))
        device.prepare()
        device.invalidate()
        self.assertEqual((device.token, device.lid), ('', 'LOCAL_ONLY_FIXTURE'))
        self.assertEqual(device.prepare(), 'NEW_SERVER_DATA_FIXTURE')
        self.assertEqual(len(client.calls), 1)


if __name__ == '__main__':
    unittest.main()
