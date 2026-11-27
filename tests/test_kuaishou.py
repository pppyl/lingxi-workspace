from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
import zlib

from features.checkin.kuaishou import CheckinKuaishou, normalize_login, TASK_NAME, QUERY, UNDERTAKE, SCOPE
from features.checkin.kuaishou_signing import KuaishouSigner
from features.checkin.client import CheckinError
from workspace.accounts.credentials import CredentialCache
from workspace.http_transport import requests


def login():
    return {'uid': 'synthetic-user', 'api_service_token': 'synthetic-token',
            'client_salt': 'synthetic-client-salt', 'feature_bits': 0xc0010000,
            'common': {'userId': 'synthetic-user', 'client_key': '2ac2a76d',
                       'did': 'synthetic-device', 'kpn': 'NEBULA'}}


class SigningTests(unittest.TestCase):
    def test_complete_padded_core_matches_unchanged_native_vectors(self):
        signer = KuaishouSigner(nonce=1234)
        vectors = json.loads((Path(__file__).parent / 'fixtures/kuaishou_sig3_core_vectors.json').read_text())['vectors']
        for vector in vectors:
            with self.subTest(input=vector['input_hex'][:12]):
                actual = signer.core(bytes.fromhex(vector['input_hex']))
                self.assertEqual(actual.hex(), vector['raw_core_hex'])
                self.assertEqual(zlib.crc32(actual), vector['crc32'])

    def test_form_changes_all_signatures_and_crc_uses_raw_core(self):
        signer = KuaishouSigner(nonce=1234, clock=lambda: 1700000000)
        query = {'client_key': 'synthetic', 'a': 'a+b /中文'}
        form = {'deepLink': 'ksnebula://featured?userToken=synthetic+token'}
        signed = signer.sign(UNDERTAKE, query, client_salt='synthetic-salt', feature_bits=0xc0010000, form=form)
        canonical = ('a=a+b /中文client_key=syntheticdeepLink=' + form['deepLink']).encode()
        self.assertEqual(signed['sig'], hashlib.md5(canonical + b'772867c19925').hexdigest())
        self.assertEqual(signed['__NStokensig'], hashlib.sha256((signed['sig'] + 'synthetic-salt').encode()).hexdigest())
        envelope = bytes.fromhex(signed['__NS_sig3'])
        checksum = envelope[-1]
        raw = bytes(byte ^ checksum ^ index for index, byte in enumerate(envelope[:23])) + b'\0'
        self.assertEqual(raw[:4], b'AQ\x27\0')
        self.assertEqual(struct.unpack_from('<III', raw, 4), (1234, 1, zlib.crc32(signer.core((UNDERTAKE + signed['sig']).encode()))))
        changed = signer.sign(UNDERTAKE, query, client_salt='synthetic-salt', feature_bits=0xc0010000, form={'deepLink': 'different'})
        for key in ('sig', '__NStokensig', '__NS_sig3', '__NS_xfalcon'):
            self.assertNotEqual(signed[key], changed[key])


class TaskFlowTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.client._credential_data = normalize_login(login())
        self.client.get.return_value = SimpleNamespace(status_code=200,
            json=lambda: {'result': 1, 'taskTypeInfo': {'taskType': 'DUCT_CALLBACK'}})
        self.client.post.return_value = SimpleNamespace(status_code=200, json=lambda: {'result': 1})
        self.adapter = CheckinKuaishou(Mock(), Mock(), signer=KuaishouSigner(nonce=1234))
        @contextmanager
        def session():
            yield self.client
        self.adapter._session = session
        self.item = {'taskName': TASK_NAME, 'taskCategory': '6', 'taskType': '1', 'jumpUrl': 'kuaishou'}
        self.info = {'backUrl': 'https://wx.10086.cn/qwhdhub/qwhdmark/1021122301',
                     'scanTime': '0', 'cToken': 'synthetic-token'}

    def test_actual_callback_contract_persists_before_one_write_and_never_claims_completion_itself(self):
        before = Mock()
        def submit(*args, **kwargs):
            before.assert_called_once()
            self.assertEqual(args[0], 'https://apissl.gifshow.com' + UNDERTAKE)
            self.assertEqual(set(kwargs['data']), {'deepLink'})
            self.assertEqual(len(kwargs['params']['__NS_xfalcon']), 143)
            return SimpleNamespace(status_code=200, json=lambda: {'result': 1})
        self.client.post.side_effect = submit
        result = self.adapter.complete(self.item, self.info, before_submit=before)
        self.client.post.assert_called_once()
        self.assertTrue(result['awaiting_mobile_confirmation'])
        self.assertNotIn('business_callback_verified', result)

    def test_duration_or_unknown_task_is_not_reported_as_a_callback(self):
        self.client.get.return_value.json = lambda: {'result': 1, 'taskTypeInfo': {'taskType': 'DUCT_USE_APP_DURATION'}}
        before = Mock()
        with self.assertRaises(CheckinError) as raised:
            self.adapter.complete(self.item, self.info, before_submit=before)
        self.assertEqual(raised.exception.code, 'KUAISHOU_TASK_CHANGED')
        before.assert_not_called()
        self.client.post.assert_not_called()

    def test_signature_rejection_does_not_submit_or_invalidate_compatible_login(self):
        self.client.get.return_value.json = lambda: {'result': 50}
        before = Mock()
        with self.assertRaises(CheckinError):
            self.adapter.complete(self.item, self.info, before_submit=before)
        before.assert_not_called()
        self.client.post.assert_not_called()
        self.adapter.cache.invalidate.assert_not_called()

    def test_lost_write_response_remains_uncertain_without_retry(self):
        self.client.post.side_effect = requests.RequestsError('synthetic network loss')
        with self.assertRaises(CheckinError) as raised:
            self.adapter.complete(self.item, self.info, before_submit=Mock())
        self.assertTrue(raised.exception.uncertain)
        self.client.post.assert_called_once()

    def test_changed_callback_destination_is_rejected_before_network(self):
        with self.assertRaises(CheckinError):
            self.adapter.complete(self.item, {**self.info, 'backUrl': 'https://other.example/'}, before_submit=Mock())
        self.client.get.assert_not_called()
        self.client.post.assert_not_called()

    def test_valid_login_is_reused_across_primary_revision_updates(self):
        cache = CredentialCache()
        snapshot = {'id': 'a', 'phone': '13800138000', 'revision': 1}
        with requests.Session() as client:
            cache.connect(SCOPE, snapshot, client, lambda: normalize_login(login()), expiry_cookies=())
        original = cache.peek(SCOPE, snapshot)
        adapter = CheckinKuaishou(lambda: {**snapshot, 'revision': 2}, cache)
        with adapter._session() as client:
            self.assertEqual(client._credential_data['uid'], 'synthetic-user')
        self.assertEqual(cache.peek(SCOPE, snapshot), original)
        other = CheckinKuaishou(lambda: {'id': 'b', 'phone': '13900139000'}, cache)
        with self.assertRaises(CheckinError) as raised:
            with other._session():
                pass
        self.assertEqual(raised.exception.code, 'KUAISHOU_LOGIN_REQUIRED')


if __name__ == '__main__':
    unittest.main()
