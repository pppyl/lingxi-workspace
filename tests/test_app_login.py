import base64
from datetime import datetime, timezone
import json
import time
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app_login import (
    AppLoginError, AppLoginSession, IV, IV_14, REQUEST_KEY, REQUEST_KEY_14,
    aes_decrypt, aes_encrypt, client_id, encrypt_phone, md5, wire_request,
)


class AppLoginTests(unittest.TestCase):
    def test_crypto_matches_independent_reference_vectors(self):
        # Cross-checked with PyCryptodome, separately from the implementation.
        self.assertEqual(aes_encrypt("你好|login"), "IHc0peiC5fw0/dox2VYw0g==")
        self.assertEqual(aes_encrypt("你好|login", REQUEST_KEY_14, IV_14), "hegaOBvjTb9Ipg5zZ4scIg==")
        self.assertEqual(client_id("0123456789abcdef"), "Ac3elAP8ZcjTQUlgJ5CVF4lzL1LapOBgKp1BP9CdwGjnPUDezAIx0iSnYcIH3foIPPAUWfwVEyJmLzu8f2/xJw==")

    def test_phone_rsa_wraps_timestamp_and_plain_phone(self):
        private = rsa.generate_private_key(public_exponent=65537, key_size=1024)
        public = private.public_key().public_numbers()
        stamp = datetime(2026, 10, 28, 3, 4, 5, tzinfo=timezone.utc)
        with patch("app_login.Path.read_text", return_value=json.dumps({"n": str(public.n), "e": public.e})):
            encoded = encrypt_phone("19900000000", stamp)
        clear = private.decrypt(base64.b64decode(encoded), padding.PKCS1v15())
        self.assertEqual(clear, b"leadeon1990000000020261028030405")

    def test_header_token_uses_legacy_aes_while_body_uses_version_14(self):
        s = AppLoginSession(device_id="0123456789abcdef")
        body, headers = wire_request("https://client.app.coc.10086.cn/biz-orange/LN/test", s.envelope({"x": "你好"}), xk="key", cookie="JSESSIONID= abc== ; UID=u", millis=123, nonce="12345678")
        self.assertEqual(aes_decrypt(headers["x-token"], REQUEST_KEY, IV), "key_/biz-orange/LN/test_123_12345678")
        self.assertEqual(headers["x-sign"], md5(headers["x-token"] + "_123_12345678_abc=="))
        value = json.loads(aes_decrypt(body, REQUEST_KEY_14, IV_14))
        self.assertEqual(value["nt"], "3")
        self.assertEqual(value["reqBody"], {"x": "你好"})

    def test_sms_attempt_is_not_automatically_retried_after_timeout(self):
        s = AppLoginSession(initialized_at=time.monotonic(), xk="key")
        with patch.object(s, "call", side_effect=AppLoginError("network")) as call:
            with self.assertRaises(AppLoginError):
                s.send_sms("19900000000")
            with self.assertRaises(AppLoginError):
                s.send_sms("19900000000")
            call.assert_called_once()
        self.assertFalse(s.phone)

    def test_cookie_can_retry_exchange_without_reusing_otp(self):
        s = AppLoginSession(phone="19900000000", sms_sent_at=time.monotonic())
        def response(*args):
            s.cookie = "JSESSIONID=app-session; UID=user; ticketID=ticket"
            return 200, {"retCode": "000000"}, {}
        with patch.object(s, "call", side_effect=response) as call:
            cookie = s.login("19900000000", "123456")
            self.assertEqual(s.login("19900000000", "123456"), cookie)
            call.assert_called_once()
        self.assertNotIn("app-session", repr(s))
        self.assertNotIn("19900000000", repr(s))

    def test_provider_description_cannot_leak_account_data(self):
        with self.assertRaises(AppLoginError) as failure:
            AppLoginSession.require_success({"retCode": "110001", "retDesc": "secret-account-data"}, "登录")
        self.assertNotIn("secret-account-data", str(failure.exception))


if __name__ == "__main__":
    unittest.main()
