import base64
import gzip
import json
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa, padding

import alipay_h5 as h5

ORDER = 'app_id=fixture&method=alipay.trade.app.pay&biz_content=%7B%22out_trade_no%22%3A%22fixture%22%7D&sign=a%2Bb%2Fc%3D'
WEB = 'https://mclient.alipay.com/cashier/mobilepay.htm?awid=fixture%2Bticket'


class Gateway:
    def __init__(self, command=None):
        self.private = rsa.generate_private_key(public_exponent=65537, key_size=1024)
        self.public = base64.b64encode(self.private.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)).decode()
        self.command = command or "js://wappay('"+WEB+"')"
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def post(self, url, **options):
        self.calls.append((url, options))
        data, offset, parts = options['data'], 0, []
        for _ in range(3):
            size = int(data[offset:offset+5]); offset += 5
            parts.append(data[offset:offset+size]); offset += size
        self.key = self.private.decrypt(parts[1], padding.PKCS1v15())
        self.envelope = json.loads(parts[0])
        self.body = json.loads(gzip.decompress(h5._decrypt(parts[2], self.key)))
        response = {'form': {'onload': {'name': self.command}}}
        wire = h5._frame((h5._json({'data': {'params': {}}}),
                          h5._encrypt(gzip.compress(h5._json(response), mtime=0), self.key)))
        return type('Response', (), {'status_code': 200, 'content': wire, 'headers': {'msp-gzip': 'true'}})()


class AlipayH5Tests(unittest.TestCase):
    def request(self, gateway):
        with patch('alipay_h5.PUBLIC_KEY', gateway.public):
            return h5.AlipayH5(session_factory=lambda: gateway).payment_url(ORDER)

    def test_official_framed_exchange_returns_untouched_https_url_without_payment(self):
        gateway = Gateway()
        self.assertEqual(self.request(gateway), WEB)
        url, options = gateway.calls[0]
        self.assertEqual(url, 'https://mobilegw.alipay.com/mgw.htm')
        self.assertEqual(options['headers']['Operation-Type'], 'alipay.msp.cashier.dispatch.bytes')
        self.assertFalse(options['allow_redirects'])
        self.assertEqual(gateway.body['action'], {'type': 'cashier', 'method': 'main'})
        self.assertTrue(gateway.body['external_info'].startswith(ORDER+'&bizcontext='))
        self.assertEqual(gateway.body['tid'], '')
        self.assertTrue(gateway.body['user_agent'].startswith('Msp/15.8.14 ('))
        self.assertIn(';sdk-and-lite;', gateway.body['user_agent'])
        self.assertNotIn('phone', gateway.body)
        self.assertNotIn('cookie', gateway.body)
        self.assertEqual(len(gateway.calls), 1)

    def test_no_guessing_client_scheme_untrusted_domain_cookie_or_post_as_get(self):
        cases = [
            ("js://wappay('alipays://fixture')", 'ALIPAY_URL_UNSUPPORTED'),
            ("js://wappay('https://alipay.com.evil.invalid/pay')", 'ALIPAY_URL_UNSUPPORTED'),
            ("js://wappay('https://mclient.alipay.com/pay', 'private-cookie')", 'ALIPAY_H5_SESSION_REQUIRED'),
            ("loc:openweb('https://mclient.alipay.com/pay', 'title', '"+base64.b64encode(b'{"method":"POST"}').decode()+"')", 'ALIPAY_H5_METHOD_UNSUPPORTED'),
        ]
        for command, code in cases:
            with self.subTest(code=code), self.assertRaises(h5.AlipayH5Error) as caught:
                self.request(Gateway(command))
            self.assertEqual(caught.exception.code, code)

    def test_truncated_or_extra_response_data_is_not_a_payment_url(self):
        for packet in (b'00005abc', b'00002{}00000extra', b'abcde'):
            with self.subTest(packet=packet), self.assertRaises(ValueError):
                h5._unframe(packet)

    def test_signed_order_is_not_changed_when_bizcontext_already_exists(self):
        order = ORDER+'&bizcontext=%7B%22an%22%3A%22original%22%7D'
        self.assertEqual(h5._sdk_order(order), order)


if __name__ == '__main__':
    unittest.main()
