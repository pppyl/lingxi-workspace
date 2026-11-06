from concurrent.futures import ThreadPoolExecutor
import json
import threading
import unittest
from unittest.mock import Mock

from recharge.device import APP_ID, SERVER, DeviceError, DeviceSession, decode_param, encode_param


def response(status=1, data='official-fixture-token', http_status=200):
    return Mock(status_code=http_status, json=Mock(return_value={'status': status, 'data': data}))


class DeviceProtocolTests(unittest.TestCase):
    def session(self, replies, *, state=None, **kwargs):
        http = Mock()
        http.get.side_effect = replies
        return DeviceSession('account-A', http=http, state=state, clock=lambda: 1800000000, **kwargs)

    def test_sdk_base64_alphabet_and_cesu8_fixture(self):
        # Independent fixed byte fixture: JSON.stringify({a:'中😀'}) encoded by
        # SDK utf8Encode uses ED A0 BD ED B8 80 for the two emoji code units.
        encoded = 'j6JmBCF2HP2QtUDLtM2SB_g='
        self.assertEqual(encode_param({'a': '中😀'}), encoded)
        self.assertEqual(decode_param(encoded), {'a': '中😀'})

    def test_lightweight_request_matches_activity_domain_and_payload(self):
        device = self.session([response()])
        self.assertEqual(device.ensure(), 'official-fixture-token')
        args, kwargs = device.http.get.call_args
        self.assertEqual(args, (SERVER,))
        self.assertEqual(kwargs['headers']['appId'], APP_ID)
        self.assertEqual(kwargs['timeout'], 1.5)
        self.assertFalse(kwargs['allow_redirects'])
        payload = decode_param(kwargs['headers']['param'])
        self.assertEqual(list(payload), ['lid', 'lidType', 'cache', 'appKey'])
        self.assertEqual(payload['lidType'], '0')
        self.assertTrue(payload['cache'])
        self.assertEqual(payload['appKey'], APP_ID)
        self.assertRegex(payload['lid'], r'^1800000000000[0-9a-zA-Z]{32}$')

    def test_unknown_expiry_reuses_official_token_after_restart(self):
        first = self.session([response()])
        first.ensure()
        state = first.export_state()
        self.assertIsNone(state['expires_at'])
        restored = self.session([], state=json.loads(json.dumps(state)))
        restored.clock = lambda: 9999999999
        self.assertEqual(restored.ensure(), 'official-fixture-token')
        restored.http.get.assert_not_called()

    def test_account_and_authentication_domain_cannot_share_state(self):
        state = self.session([response()])
        state.ensure()
        snapshot = state.export_state()
        for field, value in [('account_key', 'account-B'), ('app_id', 'wallet-app-id'),
                             ('server', 'https://touch.10086.cn/i/mobile/tfk/mgt/decision-engine/udid/c1')]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.session([], state={**snapshot, field: value})

    def test_explicit_invalidation_preserves_device_identity(self):
        device = self.session([response(data='first'), response(data='second')])
        device.ensure()
        lid = device.export_state()['lid']
        device.invalidate()
        self.assertEqual(device.ensure(), 'second')
        payload = decode_param(device.http.get.call_args.kwargs['headers']['param'])
        self.assertEqual(payload['lid'], lid)
        self.assertEqual(payload['lidType'], 1)

    def test_concurrent_initialization_is_merged(self):
        entered, finish = threading.Event(), threading.Event()
        device = self.session([])

        def request(*args, **kwargs):
            entered.set()
            self.assertTrue(finish.wait(3))
            return response()

        device.http.get.side_effect = request
        with ThreadPoolExecutor(max_workers=8) as pool:
            first = pool.submit(device.ensure)
            self.assertTrue(entered.wait(3))
            remaining = [pool.submit(device.ensure) for _ in range(7)]
            finish.set()
            self.assertEqual([item.result() for item in [first, *remaining]], ['official-fixture-token'] * 8)
        self.assertEqual(device.http.get.call_count, 1)

    def test_fingerprint_requirement_is_explicit_and_never_invented(self):
        device = self.session([response(status=0, data=None)])
        with self.assertRaises(DeviceError) as error:
            device.ensure()
        self.assertEqual(error.exception.code, 'DEVICE_FINGERPRINT_REQUIRED')
        self.assertEqual(device.http.get.call_count, 1)
        self.assertEqual(device.export_state()['const_id'], '')
        self.assertTrue(device.export_state()['lid'])

    def test_rejected_authentication_stops_without_detection(self):
        provider = Mock()
        device = self.session([response(status=-3, data=None)], fingerprint_provider=provider)
        with self.assertRaises(DeviceError) as error:
            device.ensure()
        self.assertEqual(error.exception.code, 'DEVICE_AUTH_REJECTED')
        provider.assert_not_called()
        self.assertEqual(device.http.get.call_count, 1)

    def test_server_lid_update_and_detect_response_are_bounded(self):
        device = self.session([response(status=-4, data='server-lid'), response(status=2, data='detected-token')],
                              fingerprint_provider=lambda: {'ua': 'Python fixture', 'collectTime': 1})
        self.assertEqual(device.ensure(), 'detected-token')
        payload = decode_param(device.http.get.call_args.kwargs['headers']['param'])
        self.assertEqual(payload['lid'], 'server-lid')
        self.assertEqual(payload['ua'], 'Python fixture')
        self.assertEqual(device.http.get.call_count, 2)

    def test_detect_does_not_accept_cache_only_response(self):
        device = self.session([response(status=0), response(status=1)], fingerprint_provider=lambda: {})
        with self.assertRaises(DeviceError) as error:
            device.ensure()
        self.assertEqual(error.exception.code, 'DEVICE_AUTH_REJECTED')
        self.assertEqual(device.http.get.call_count, 2)

    def test_rate_limit_stops_without_retry(self):
        provider = Mock()
        device = self.session([response(http_status=429)], fingerprint_provider=provider)
        with self.assertRaises(DeviceError) as error:
            device.ensure()
        self.assertEqual(error.exception.code, 'DEVICE_ACCESS_LIMIT')
        provider.assert_not_called()
        self.assertEqual(device.http.get.call_count, 1)

    def test_no_javascript_transport_fallback(self):
        device = self.session([response(status=-5)])
        with self.assertRaises(DeviceError) as error:
            device.ensure()
        self.assertEqual(error.exception.code, 'DEVICE_INTERACTION_REQUIRED')
        self.assertEqual(device.http.get.call_count, 1)


if __name__ == '__main__':
    unittest.main()
