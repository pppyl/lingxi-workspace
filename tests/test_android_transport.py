import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch


@unittest.skipUnless(importlib.util.find_spec('httpx'), 'Android transport tests require httpx')
class AndroidTransportTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.closed = 0
        self.body = b'data: hello\n\ndata: world\n\n'
        self.headers = [['Content-Type', 'text/event-stream'], ['Set-Cookie', 'a=1; Path=/'], ['Set-Cookie', 'b=2; Path=/']]
        owner = self

        class Result:
            def status(self): return 200
            def headers(self): return json.dumps(owner.headers)
            def read(self):
                body, owner.body = owner.body, None
                return body
            def close(self): owner.closed += 1

        class Native:
            @staticmethod
            def request(*args):
                owner.calls.append(args)
                return Result()

        java = SimpleNamespace(jclass=lambda name: OSError if name == 'java.io.IOException' else Native)
        spec = importlib.util.spec_from_file_location('android_transport_fixture', Path(__file__).resolve().parents[1] / 'http_transport.py')
        self.module = importlib.util.module_from_spec(spec)
        with patch.dict('os.environ', {'LINGXI_ANDROID': '1'}), patch.dict(sys.modules, {'java': java}):
            spec.loader.exec_module(self.module)

    def test_stream_http_version_headers_and_timeouts_reach_native_stack(self):
        with self.module.requests.Session(impersonate='chrome') as client:
            response = client.post('https://example.test/chat', json={'message': '你好'}, stream=True,
                                   allow_redirects=False, http_version=self.module.CurlHttpVersion.V1_1, timeout=(10, 90))
            self.assertEqual(list(response.iter_lines()), ['data: hello', '', 'data: world', ''])
            response.close()
            call = self.calls[0]
            self.assertEqual(json.loads(call[3]), {'message': '你好'})
            self.assertEqual(call[4:], (False, 10, 90))
            self.assertEqual(response.headers.get_list('set-cookie'), ['a=1; Path=/', 'b=2; Path=/'])
            self.assertEqual(client.cookies.get('a'), '1')
            self.assertEqual(client.cookies.get('b'), '2')

    def test_cookie_jar_is_carried_forward_and_local_form_bytes_unchanged(self):
        with self.module.requests.Session() as client:
            client.get('https://example.test/login')
            self.body = b'{}'
            client.post('https://example.test/verify', data=b'sid=x&answer=user%2Binput',
                        headers={'Content-Type': 'application/x-www-form-urlencoded'}, timeout=(5, 15))
            call = self.calls[-1]
            self.assertIn('a=1', dict(json.loads(call[2]))['cookie'])
            self.assertEqual(call[3], b'sid=x&answer=user%2Binput')
            self.assertEqual(call[4:], (True, 5, 15))

    def test_wallet_sdk_cookie_secure_flag_is_supported_and_preserved(self):
        with self.module.requests.Session() as client:
            client.cookies.set('_zw_kvani5r', 'sdk-lid', domain='example.test', path='/', secure=False)
            client.cookies.set('stc-rrt-token', 'sdk-token', domain='example.test', path='/i', secure=True)
            client.get('https://example.test/i/exchange')
            cookie = dict(json.loads(self.calls[-1][2]))['cookie']
            self.assertIn('stc-rrt-token=sdk-token', cookie)
            self.assertIn('_zw_kvani5r=sdk-lid', cookie)
            saved = next(item for item in client.cookies.jar if item.name == 'stc-rrt-token')
            self.assertTrue(saved.secure)
            self.body = b'{}'
            client.get('http://example.test/i/exchange')
            insecure_cookie = dict(json.loads(self.calls[-1][2])).get('cookie', '')
            self.assertNotIn('stc-rrt-token', insecure_cookie)
            self.assertIn('_zw_kvani5r=sdk-lid', insecure_cookie)
