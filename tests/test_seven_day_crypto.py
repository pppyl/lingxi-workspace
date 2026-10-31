import base64
import json
import unittest
from unittest.mock import Mock, patch

from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from activities.seven_day import crypto


class SevenDayCryptoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def setUp(self):
        self.public = patch.object(crypto, '_public_key', self.private.public_key())
        self.public.start()
        self.addCleanup(self.public.stop)

    def decode(self, result):
        key = self.private.decrypt(base64.b64decode(result['headers']['coc-aurora']), padding.PKCS1v15())
        self.assertEqual(len(key), 32)
        body = base64.b64decode(result['data']['cocEnContent'])
        return json.loads(AESGCM(key).decrypt(body[:12], body[12:], None))

    def test_envelope_preserves_business_and_real_environment(self):
        environment = {'runtime_type': 'Python', 'platform': 'win32'}
        result = crypto.prepare({'lotteryId': 'test-only'}, environment, 'official-token', ['previous', 'official-token'],
                                timestamp=1793469079409, key=b'k'*32, nonce=b'n'*12)
        frame = self.decode(result)
        self.assertEqual(frame['data'], {'lotteryId': 'test-only', 'deviceToken': 'official-token'})
        self.assertEqual(frame['environment']['device_token'], ['previous', 'official-token'])
        self.assertEqual(frame['environment']['runtime_type'], 'Python')
        self.assertEqual(frame['timestamp'], 1793469079409)
        self.assertEqual(result['headers']['coc-en-version'], '1586234121417629696')
        self.assertNotIn('device_token', environment)

    def test_gdt_uses_separate_encrypted_text_and_then_binds_token(self):
        token = '00000000-0000-4000-8000-000000000001'
        observed = []

        def transport(body, headers):
            self.assertEqual(set(body), {'encryptedText'})
            self.assertEqual(set(headers), {'Content-Type', 'coc-aurora'})
            frame = self.decode({'data': {'cocEnContent': body['encryptedText']}, 'headers': headers})
            observed.append(frame)
            return {'code': '0', 'data': token}

        environment = Mock(side_effect=[{'runtime_type': 'Python', 'sequence': 1}, {'runtime_type': 'Python', 'sequence': 2}])
        result = crypto.encrypt({'lotteryId': 'test-only'}, [], 'Python-test-only', transport=transport, environment=environment)
        self.assertEqual(observed[0]['data'], {'activityId': '109230918290', 'deviceToken': ''})
        self.assertEqual(observed[0]['environment']['device_token'], [])
        final = self.decode(result)
        self.assertEqual(final['data']['deviceToken'], token)
        self.assertEqual(final['environment']['device_token'], [token])
        self.assertEqual(final['environment']['sequence'], 2)
        self.assertEqual(result['user_agent'], 'Python-test-only')
        self.assertEqual(environment.call_count, 2)

    def test_gdt_rejection_does_not_prepare_business_request(self):
        transport = Mock(return_value={'code': '10', 'data': None})
        with patch.object(crypto, 'prepare', wraps=crypto.prepare) as prepare:
            with self.assertRaises(crypto.SevenDayCryptoError) as caught:
                crypto.encrypt({'lotteryId': 'test-only'}, [], 'test', transport=transport, environment={})
        self.assertEqual(caught.exception.code, 'GDT_REJECTED')
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(prepare.call_count, 1)

    def test_missing_token_does_not_fall_back_to_empty(self):
        for response in ({'code': '0', 'data': ''}, {'code': '0', 'data': None}, {'code': '0', 'data': {}}):
            with self.subTest(response=response), patch.object(crypto, 'prepare', wraps=crypto.prepare) as prepare:
                with self.assertRaises(crypto.SevenDayCryptoError) as caught:
                    crypto.encrypt({}, [], 'test', transport=lambda *_: response, environment={})
                self.assertEqual(caught.exception.code, 'GDT_TOKEN_MISSING')
                self.assertEqual(prepare.call_count, 1)

    def test_missing_transport_fails_before_preparing(self):
        with patch.object(crypto, 'prepare') as prepare:
            with self.assertRaises(crypto.SevenDayCryptoError) as caught:
                crypto.encrypt({}, [], 'test')
        self.assertEqual(caught.exception.code, 'GDT_TRANSPORT_REQUIRED')
        prepare.assert_not_called()


if __name__ == '__main__':
    unittest.main()
