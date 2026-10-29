import json
import unittest
from unittest.mock import Mock, patch
from curl_cffi.requests.exceptions import ConnectionError

from app import create_app
from lingxi_client import Account, LingxiError, chat, cookie_parameters, parse_sse, verify_token


class ProtocolTests(unittest.TestCase):
    def test_cookie_preserves_equals_and_normalizes_ticket(self):
        self.assertEqual(cookie_parameters("JSESSIONID=a==; TicketID=b; ignored"), {"jsessionid": "a==", "ticketId": "b"})

    def test_sse_comments_multiline_and_eof(self):
        lines = [b": keepalive", b"event: message\r", b'data: {"a":', b"data: 1}", b"", b"data: second"]
        self.assertEqual(list(parse_sse(lines)), [("message", '{"a":\n1}'), ("message", "second")])

    def test_sse_utf8_and_limit(self):
        self.assertEqual(list(parse_sse(["data: 你好".encode()])), [("message", "你好")])
        with patch("lingxi_client.MAX_EVENT_BYTES", 3):
            with self.assertRaises(LingxiError):
                list(parse_sse(["data: 你好"]))

    def make_response(self, events):
        response = Mock(status_code=200, headers={"content-type": "text/event-stream"})
        lines = []
        for event in events:
            lines += [("data: " + json.dumps(event)).encode(), b""]
        response.iter_lines.return_value = iter(lines)
        session = Mock()
        session.__enter__ = Mock(return_value=session)
        session.__exit__ = Mock(return_value=False)
        session.post.return_value = response
        return response, session

    def test_stream_text_cards_and_success(self):
        response, session = self.make_response([
            {"code": 0, "data": {"type": "Txt", "content": "你"}},
            {"code": 0, "data": {"type": "Txt", "content": "好"}},
            {"code": 0, "data": {"type": "Card", "content": {"title": "结果"}}},
        ])
        account = Account(auth_code="test-code")
        with patch("lingxi_client.requests.Session", return_value=session):
            output = list(chat(account, "你好"))
        self.assertEqual("".join(x["text"] for x in output if x["type"] == "delta"), "你好")
        self.assertEqual(output[-1]["type"], "done")
        self.assertTrue(account.verified)
        self.assertEqual(session.post.call_args.kwargs["json"]["msg"], "你好")
        self.assertEqual(session.post.call_args.kwargs["json"]["inputType"], "TextInput")
        self.assertEqual(session.post.call_args.kwargs["json"]["extInfo"]["uiState"], "FullScreen")
        self.assertEqual(session.post.call_args.kwargs["headers"]["authtoken"], "test-code")
        response.close.assert_called_once()
        self.assertFalse(account.lock.locked())

    def test_clear_replaces_text_and_structured_answer_is_readable(self):
        _, session = self.make_response([
            {"code": 0, "data": {"type": "Txt", "content": "旧内容"}},
            {"code": 0, "data": {"type": "Clear"}},
            {"code": 0, "data": {"type": "Txt", "content": json.dumps({"aBeans": {"q": "问题", "a": "回答"}})}},
        ])
        with patch("lingxi_client.requests.Session", return_value=session):
            result = list(chat(Account(auth_code="test"), "测试"))
        self.assertEqual(result[1], {"type": "clear"})
        self.assertEqual(result[2], {"type": "delta", "text": "问题\n回答"})

    def test_verifier_rejects_server_auth_failure(self):
        _, session = self.make_response([])
        session.post.return_value.json.return_value = {"rtnCode": "9999", "ok": False}
        with patch("lingxi_client.requests.Session", return_value=session):
            with self.assertRaises(LingxiError) as failure:
                verify_token("bad")
        self.assertTrue(failure.exception.authentication)

    def test_auth_error_clears_credentials_even_with_http_200(self):
        response, session = self.make_response([{"code": 401, "msg": "鉴权异常", "data": None}])
        account = Account(auth_code="expired", verified=True)
        with patch("lingxi_client.requests.Session", return_value=session):
            with self.assertRaises(LingxiError) as failure:
                list(chat(account, "测试"))
        self.assertTrue(failure.exception.authentication)
        self.assertFalse(account.auth_code)
        self.assertFalse(account.verified)
        self.assertFalse(account.lock.locked())
        response.close.assert_called_once()

    def test_client_disconnect_closes_upstream(self):
        response, session = self.make_response([
            {"code": 0, "data": {"type": "Txt", "content": "first"}},
            {"code": 0, "data": {"type": "Txt", "content": "second"}},
        ])
        account = Account(auth_code="test-code")
        with patch("lingxi_client.requests.Session", return_value=session):
            stream = chat(account, "测试")
            next(stream)
            stream.close()
        response.close.assert_called_once()
        self.assertFalse(account.lock.locked())

    def test_legacy_tls_eof_finishes_only_an_unframed_close_response(self):
        response, session = self.make_response([])
        response.headers['connection'] = 'close'
        def lines():
            yield b'data: {"code":0,"data":{"type":"Txt","content":"answer"}}'
            yield b''
            raise ConnectionError('Connection closed abruptly', code=56)
        response.iter_lines.side_effect = lines
        with patch('lingxi_client.requests.Session', return_value=session):
            output = list(chat(Account(auth_code='test'), 'hello'))
        self.assertEqual(output[-1]['type'], 'done')
        response.headers['content-length'] = '9999'
        with patch('lingxi_client.requests.Session', return_value=session):
            with self.assertRaises(ConnectionError):
                list(chat(Account(auth_code='test'), 'hello'))

    def test_reset_is_never_mistaken_for_a_complete_answer(self):
        response, session = self.make_response([])
        response.headers['connection'] = 'close'
        def lines():
            yield b'data: {"code":0,"data":{"type":"Txt","content":"partial"}}'
            yield b''
            raise ConnectionError('Recv failure: Connection was reset', code=56)
        response.iter_lines.side_effect = lines
        with patch('lingxi_client.requests.Session', return_value=session):
            with self.assertRaises(ConnectionError):
                list(chat(Account(auth_code='test'), 'hello'))

    def test_progress_card_alone_is_not_a_successful_answer(self):
        _, session = self.make_response([{'code':0,'data':{'type':'Card','content':{'cardNumber':'-1000','data':{'msg':'working'}}}}])
        with patch('lingxi_client.requests.Session', return_value=session):
            with self.assertRaises(LingxiError):
                list(chat(Account(auth_code='test'), 'hello'))


class WebTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config.update(TESTING=True)
        self.client = self.app.test_client()

    @patch("app.verify_token")
    @patch("app.exchange_cookie", return_value="secret-placeholder")
    @patch("app.AppLoginSession")
    def test_sms_login_exchanges_cookie_and_isolates_browser_sessions(self, factory, exchange, verify):
        flow = factory.return_value
        flow.send_sms.return_value = {"sent": True, "phone_masked": "199****0000", "retry_after": 60}
        flow.login.return_value = "JSESSIONID=private-cookie"
        result = self.client.post("/api/login/sms", json={"phone": "19900000000"})
        self.assertEqual(result.status_code, 200)
        result = self.client.post("/api/login", json={"phone": "19900000000", "code": "123456"})
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.json["verified"])
        self.assertNotIn(b"secret-placeholder", result.data)
        self.assertNotIn(b"private-cookie", result.data)
        flow.login.assert_called_once_with("19900000000", "123456")
        exchange.assert_called_once_with("JSESSIONID=private-cookie")
        verify.assert_called_once_with("secret-placeholder")
        self.assertFalse(self.app.test_client().get("/api/status").json["has_credentials"])
        self.assertEqual(self.client.post("/api/logout", json={}).status_code, 200)
        self.assertFalse(self.client.get("/api/status").json["verified"])

    @patch("app.verify_token", side_effect=LingxiError("验证失败", authentication=True))
    @patch("app.exchange_cookie", return_value="unverified-secret")
    @patch("app.AppLoginSession")
    def test_failed_verification_never_connects_account(self, factory, exchange, verify):
        factory.return_value.login.return_value = "JSESSIONID=test"
        result = self.client.post("/api/login", json={"phone": "19900000000", "code": "123456"})
        self.assertEqual(result.status_code, 400)
        self.assertFalse(self.client.get("/api/status").json["has_credentials"])
        self.assertEqual(self.client.post("/api/logout", json={}).status_code, 200)

    @patch("app.AppLoginSession")
    def test_gateway_failure_releases_account_lock(self, factory):
        from app_login import AppLoginError
        factory.return_value.send_sms.side_effect = AppLoginError("网关未通过")
        result = self.client.post("/api/login/sms", json={"phone": "19900000000"})
        self.assertEqual(result.status_code, 400)
        self.assertEqual(self.client.post("/api/logout", json={}).status_code, 200)

    def test_invalid_input_and_login_without_sms_never_reach_provider(self):
        with patch("app_login.requests.Session") as provider:
            for phone in (None, [], "199", "11900000000"):
                self.assertEqual(self.client.post("/api/login/sms", json={"phone": phone}).status_code, 400)
            self.assertEqual(self.client.post("/api/login", json={"phone": "19900000000", "code": "123456"}).status_code, 400)
            provider.assert_not_called()

    def test_origin_and_host_validation(self):
        self.assertEqual(self.client.post("/api/login/sms", json={}, headers={"Origin": "https://unrelated.example"}).status_code, 403)
        self.assertEqual(self.client.get("/api/status", base_url="http://unrelated.example").status_code, 403)

    def test_not_logged_in_and_message_validation(self):
        self.assertEqual(self.client.post("/api/chat", json={"message": "你好"}).status_code, 401)
        for value in ({}, {"message": []}, {"message": " "}, {"message": "a" * 8001}):
            self.assertEqual(self.client.post("/api/chat", json=value).status_code, 400)

    def test_render_and_security_headers(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("灵犀多账号对话".encode(), response.data)
        self.assertNotIn(b'id="send-sms"', response.data)
        center = self.client.get("/accounts")
        self.assertIn(b'id="send-sms"', center.data)
        feature = self.client.get("/lingxi")
        self.assertIn(b'id="chat-account"', feature.data)
        self.assertNotIn(b'id="send-sms"', feature.data)
        self.assertNotIn(b'id="credential"', response.data)
        self.assertNotIn(b'id="device"', response.data)
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])


if __name__ == "__main__":
    unittest.main()
