"""LAN access keeps both apps' same-origin and CSRF boundaries intact."""
import unittest
from unittest.mock import Mock

from app import create_app
from network_access import allowed_lan_host
from recharge.cmcc import ActivityClient


class NetworkAccessTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(enable_recharge=True)
        self.http = self.app.test_client()

    def test_host_validation_accepts_loopback_and_private_ipv4_only(self):
        for host in ('localhost','LOCALHOST:5500','127.0.0.1:5500','[::1]:5500',
                     '192.168.31.176:5500','192.168.1.100:5500','10.2.3.4:5500','172.16.0.5:5500','172.31.255.254:5500'):
            with self.subTest(host=host):self.assertTrue(allowed_lan_host(host))
        for host in ('evil.test','192.168.31.176.evil.test','8.8.8.8:5500','172.32.0.1:5500',
                     '0.0.0.0:5500','169.254.1.2:5500','[2001:db8::1]:5500','[::1','',
                     'evil@192.168.31.176','192.168.31.176/','localhost:bad'):
            with self.subTest(host=host):self.assertFalse(allowed_lan_host(host))

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
        self.assertEqual(client.get('/api/status',base_url='http://evil.test').status_code,403)


if __name__ == '__main__':unittest.main()
