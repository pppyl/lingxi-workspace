import json
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
import unittest
from unittest.mock import patch

from http_transport import requests
from star_day_auth import StarDaySessionCache, star_day_session
from star_day_client import StarDayClient


class TransportTests(unittest.TestCase):
    def test_actual_wire_requests_are_ajax_with_correct_form_and_json_encoding(self):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append((self.path, self.headers, self.rfile.read(int(self.headers['Content-Length']))))
                body = b'{"status":"1","data":{}}'
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Set-Cookie','flow_state=initialized; Path=/')
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        server = HTTPServer(('127.0.0.1', 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        cache = StarDaySessionCache()
        self.addCleanup(cache.forget,'a')
        snapshot = {'id':'a', 'phone':'13800000001', 'cookie':'UID=test; JSESSIONID=test', 'status':'login_required'}
        with requests.Session() as cookie_source:
            cookie_source.cookies.set('access_token', 'test-token', domain='qqt.cmicrwx.cn')
            cache.save('a', snapshot['phone'], cookie_source, source='sms')
        protocol = StarDayClient(lambda:snapshot, session_cache=cache)
        def send(path, **kwargs):
            with star_day_session(lambda:snapshot,cache=cache,allow_login=False) as (client,_):
                protocol._request(client,path,**kwargs)
                return client
        with patch('star_day_auth.AppLoginSession.from_credentials') as primary:
            with patch('star_day_client.BASE', 'http://127.0.0.1:'+str(server.server_port)):
                with ThreadPoolExecutor(max_workers=1) as first, ThreadPoolExecutor(max_workers=1) as second:
                    initialized=first.submit(send,'/claim',data={'productId':'3913','receivePhone':'','channelCode':'75628','isRandom':'0','sourece':'2'}).result()
                    reused=second.submit(send,'/sign',json={'sspid':'75628'}).result()
                    self.assertIs(reused,initialized)
            primary.assert_not_called()
        self.assertIn('flow_state=initialized',received[1][1]['Cookie'])
        self.assertEqual(received[0][2], b'productId=3913&receivePhone=&channelCode=75628&isRandom=0&sourece=2')
        self.assertEqual(received[0][1]['Content-Type'], 'application/x-www-form-urlencoded; charset=UTF-8')
        self.assertEqual(json.loads(received[1][2]), {'sspid':'75628'})
        self.assertEqual(received[1][1]['Content-Type'], 'application/json')
        for _, headers, _ in received:
            self.assertEqual(headers['X-Requested-With'], 'XMLHttpRequest')
            self.assertEqual(headers['Accept'], 'application/json, text/javascript, */*; q=0.01')
            self.assertEqual(headers['Sec-Fetch-Mode'], 'cors')
            self.assertNotIn('Sec-Fetch-User', headers)
            self.assertNotIn('Upgrade-Insecure-Requests', headers)
            self.assertNotIn('Sec-CH-UA', headers)


if __name__ == '__main__':
    unittest.main()
