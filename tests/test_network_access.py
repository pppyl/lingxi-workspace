"""LAN access keeps both apps' same-origin and CSRF boundaries intact."""
import unittest
from unittest.mock import Mock

from app import create_app
from network_access import classify_access_host
from recharge.cmcc import ActivityClient


class NetworkAccessTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(enable_recharge=True)
        self.http = self.app.test_client()
        self.app.extensions['access_security'].set_password('network-test-password', '')
        response = self.http.post('/api/access/login', json={'password':'network-test-password'},
            base_url='http://192.168.31.176:5500', environ_overrides={'REMOTE_ADDR':'192.168.31.90'})
        self.assertEqual(response.status_code, 200)

    def test_host_validation_accepts_literal_ipv4_and_ipv6_addresses(self):
        for host in ('localhost','LOCALHOST:5500','127.0.0.1:5500','[::1]:5500',
                     '192.168.31.176:5500','192.168.1.100:5500','10.2.3.4:5500','172.16.0.5:5500','172.31.255.254:5500',
                     '8.8.8.8:5500','[2001:db8::1]:5500','[fd12::1]:5500','[fe80::1%12]:5500',
                     'workspace.example.com:5500','my-workspace.ddns.net:5500'):
            with self.subTest(host=host):self.assertTrue(classify_access_host(host)[0])
        for host in ('-invalid.example','bad..example','bad_host.example',
                     '0.0.0.0:5500','[::]:5500','224.0.0.1:5500','[ff02::1]:5500','[::1','',
                     'evil@192.168.31.176','192.168.31.176/','localhost:bad'):
            with self.subTest(host=host):self.assertFalse(classify_access_host(host)[0])

    def test_lan_pages_and_account_apis_are_available_without_renewing_credentials(self):
        maintenance = self.app.extensions['session_maintenance']
        maintenance.check = Mock(side_effect=AssertionError('Page access must not refresh credentials'))
        base = 'http://192.168.31.176:5500'
        for path in ('/','/accounts','/lingxi','/coupons','/checkin','/monthly-coupon','/star-day',
                     '/api/logins','/api/coupons/accounts','/recharge/','/recharge/api/accounts'):
            with self.subTest(path=path):
                self.assertEqual(self.http.get(path,base_url=base).status_code,200)
        maintenance.check.assert_not_called()

    def test_same_origin_post_is_accepted_and_cross_origin_post_is_rejected(self):
        base = 'http://192.168.31.176:5500'
        self.assertEqual(self.http.post('/api/logout',json={},base_url=base,headers={'Origin':base}).status_code,200)
        for origin in ('https://evil.test','http://192.168.31.99:5500','http://192.168.31.176:5600'):
            self.assertEqual(self.http.post('/api/logout',json={},base_url=base,headers={'Origin':origin}).status_code,403)

    def test_mounted_recharge_lan_post_still_requires_csrf(self):
        from recharge.app import create_app as recharge_app
        activity = ActivityClient()
        self.addCleanup(activity.http.close)
        client = recharge_app(activity).test_client()
        base = 'http://192.168.31.176:5500'
        self.assertEqual(client.get('/',base_url=base).status_code,200)
        with client.session_transaction(base_url=base) as session:csrf = session['csrf']
        self.assertEqual(client.post('/api/import',json=[],base_url=base,headers={'Origin':base}).status_code,403)
        response = client.post('/api/import',json=[],base_url=base,headers={'Origin':base,'X-CSRF-Token':csrf})
        self.assertEqual(response.status_code,400)  # Passed origin/CSRF, rejected invalid import data.
        self.assertEqual(client.get('/api/status',base_url=base,headers={'Origin':'https://evil.test'}).status_code,403)

    def test_real_dual_stack_listener_serves_the_same_port_over_both_families(self):
        import os
        import threading
        from urllib.request import ProxyHandler, build_opener
        from unittest.mock import patch
        from network_access import create_http_server
        with patch.dict(os.environ):
            os.environ.pop('LINGXI_HOST', None)
            server = create_http_server(self.app, 0)
        worker = threading.Thread(target=server.run, daemon=True)
        worker.start()
        try:
            opener = build_opener(ProxyHandler({}))
            for host in ('127.0.0.1', '[::1]'):
                with opener.open(f'http://{host}:{server.effective_port}/api/access/status', timeout=5) as response:
                    import json
                    state = json.load(response)
                    self.assertTrue(state['local'])
                    self.assertEqual(response.status, 200)
        finally:
            server.close()
            worker.join(timeout=2)


if __name__ == '__main__':unittest.main()
