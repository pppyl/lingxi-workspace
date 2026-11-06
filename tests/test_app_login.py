import base64
from datetime import datetime, timezone
import json
import time
import unittest
from unittest.mock import Mock, patch

from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app_login import (
    APP_IT_HOST, AppCredentialError, AppLoginError, AppLoginSession, IV, IV_14, REQUEST_KEY, REQUEST_KEY_14,
    aes_decrypt, aes_encrypt, client_id, encrypt_phone, md5, wire_request,
)


class AppLoginTests(unittest.TestCase):
    def test_it_gateway_sends_legacy_encryption_and_signs_the_actual_official_url(self):
        headers=Mock();headers.get.return_value='';headers.get_list.return_value=[]
        transport=Mock();transport.post.return_value=Mock(status_code=200,text='{"retCode":"000000"}',headers=headers)
        flow=AppLoginSession.from_credentials({'phone':'19900000000','cookie':'JSESSIONID=unit',
             'profile':{'provinceCode':'851'},'app_context':{'device_id':'unit','xk':'unit'}})
        flow.gateway_host,flow.request_version,flow.transport=APP_IT_HOST,2,transport
        payload={'cellNum':flow.phone,'busType':'1','productName':'测试业务'}
        flow.call('/biz-orange/BH/sms/getSmsCode',payload)
        call=transport.post.call_args
        self.assertEqual(call.args[0],APP_IT_HOST+'/biz-orange/BH/sms/getSmsCode')
        self.assertEqual(call.kwargs['headers']['x-qen'],'2')
        clear=aes_decrypt(call.kwargs['data'],REQUEST_KEY,IV)
        self.assertEqual(json.loads(clear)['reqBody'],payload)
        self.assertEqual(json.loads(clear)['tel'],flow.phone)
        self.assertEqual(call.kwargs['headers']['xs'],md5(call.args[0]+'_'+clear+'_Leadeon/SecurityOrganization'))
        default=AppLoginSession.from_credentials({'phone':flow.phone,'cookie':flow.cookie})
        self.assertEqual(default.gateway_host,'https://client.app.coc.10086.cn')
        self.assertEqual(default.request_version,14)

    def test_gateway_http_error_keeps_response_for_safe_diagnostics_and_is_not_success(self):
        response=Mock(status_code=403,headers=Mock(),text='Forbidden')
        flow=AppLoginSession(transport=Mock())
        flow.transport.post.return_value=response
        with self.assertRaises(AppLoginError) as raised:
            flow.call('/biz-orange/BH/sms/getSmsCode',{})
        self.assertIn('HTTP 403',str(raised.exception))
        self.assertIs(raised.exception.response,response)
        flow.transport.post.assert_called_once()

    def test_authenticated_business_envelope_uses_native_logged_in_identity_and_region(self):
        flow=AppLoginSession.from_credentials({'phone':'19900000000','cookie':'JSESSIONID=unit',
             'profile':{'provinceCode':'851','cityCode':'0851'},'app_context':{'device_id':'unit','xk':'unit'}})
        body={'cellNum':flow.phone,'busType':'1','productName':'测试业务'}
        self.assertEqual(flow.envelope(body)['tel'],'99999999999')
        flow.gateway_host,flow.request_version=APP_IT_HOST,2
        envelope=flow.envelope(body)
        self.assertEqual((envelope['tel'],envelope['prov'],envelope['city']),('19900000000','851','0851'))
        self.assertEqual(envelope['reqBody'],body)
        flow.app_authenticated=False
        anonymous=flow.envelope(body,login_component=True)
        self.assertEqual((anonymous['tel'],anonymous['prov'],anonymous['city']),('99999999999','',''))
        self.assertEqual(anonymous['componentID'],'BI2021SA000055')

    def test_caller_owned_connection_is_reused_with_latest_cookie_and_not_closed_by_each_call(self):
        headers=Mock();headers.get.return_value='';headers.get_list.return_value=['JSESSIONID=rotated']
        response=Mock(status_code=200,text='{"retCode":"000000","rspBody":{}}',headers=headers)
        transport=Mock();transport.post.return_value=response
        flow=AppLoginSession(cookie='JSESSIONID=original',transport=transport)
        with patch('app_login.requests.Session') as create:
            flow.call('/first',{});flow.call('/second',{})
        create.assert_not_called();transport.close.assert_not_called()
        self.assertEqual(transport.post.call_count,2)
        self.assertEqual(transport.post.call_args_list[0].kwargs['headers']['Cookie'],'JSESSIONID=original')
        self.assertEqual(transport.post.call_args_list[1].kwargs['headers']['Cookie'],'JSESSIONID=rotated')

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

    def test_expired_auth_refresh_is_terminal_and_never_sends_sms(self):
        flow=AppLoginSession(phone='13800000000',cookie='JSESSIONID=fixture',xk='fixture',refresh_token='fixture-refresh')
        with patch.object(flow,'call',return_value=(200,{'retCode':'410000'},{})) as call,patch.object(flow,'send_sms') as sms:
            with self.assertRaises(AppCredentialError) as error:flow.refresh(force_identity=True)
        self.assertTrue(error.exception.login_required)
        self.assertEqual(error.exception.code,'410000')
        self.assertEqual(call.call_count,2)
        sms.assert_not_called()


if __name__ == "__main__":
    unittest.main()
