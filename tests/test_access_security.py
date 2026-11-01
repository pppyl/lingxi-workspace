"""Exercise the real WSGI boundary, shared cooldown and persistent credentials."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import unittest

from app import create_app
from access_security import AccessError, AccessSecurity, RETRY_SECONDS, SESSION_SECONDS
from login_storage import DatabaseLoginStorage

PASSWORD = 'workspace-test-123'


class AccessBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(enable_recharge=True)
        self.client = self.app.test_client()
        self.security = self.app.extensions['access_security']
        self.base = 'http://192.168.31.176:5500'
        self.remote = {'REMOTE_ADDR': '192.168.31.90'}

    def post(self, path, data, *, client=None, **kwargs):
        return (client or self.client).post(path, json=data, base_url=self.base,
            environ_overrides=self.remote, headers={'Origin': self.base}, **kwargs)

    def get(self, path, *, client=None, **kwargs):
        return (client or self.client).get(path, base_url=self.base, environ_overrides=self.remote, **kwargs)

    def setup_password(self):
        response = self.post('/api/access/password', {'password': PASSWORD, 'confirmation': PASSWORD})
        self.assertEqual(response.status_code, 200)
        return response

    def test_first_remote_access_can_only_set_a_password_before_entering(self):
        response = self.get('/coupons?login_id=example')
        self.assertEqual(response.status_code, 303)
        self.assertIn('/access?next=', response.location)
        for path in ('/api/logins', '/api/platforms', '/recharge/api/accounts', '/recharge/',
                     '/static/app.js', '/recharge/static/workspace.js'):
            with self.subTest(path=path):
                response = self.get(path)
                self.assertIn(response.status_code, (303, 428))
                self.assertNotIn(b'phone_masked', response.data)
        self.assertEqual(self.get('/access').status_code, 200)
        with self.get('/static/access.js') as asset:
            self.assertEqual(asset.status_code, 200)
        self.assertFalse(self.get('/api/access/status').json['configured'])
        response = self.setup_password()
        cookie = response.headers['Set-Cookie']
        self.assertIn('HttpOnly', cookie)
        self.assertIn('SameSite=Strict', cookie)
        self.assertIn('Path=/', cookie)
        for path in ('/', '/coupons', '/api/logins', '/recharge/', '/recharge/api/accounts'):
            self.assertEqual(self.get(path).status_code, 200, path)

    def test_remote_login_is_required_for_root_and_mounted_apis(self):
        self.security.set_password(PASSWORD, '')
        for path in ('/api/logins', '/recharge/api/accounts', '/coupon-verification/config'):
            response = self.post(path, {})
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.json['code'], 'ACCESS_LOGIN_REQUIRED')
        self.assertEqual(self.post('/api/access/login', {'password': PASSWORD}).status_code, 200)
        self.assertEqual(self.get('/recharge/api/accounts').status_code, 200)

    def test_local_access_uses_peer_address_and_never_trusts_forwarded_or_host_headers(self):
        self.security.set_password(PASSWORD, '')
        for peer in ('127.0.0.1', '::1', '::ffff:127.0.0.1'):
            self.assertEqual(self.client.get('/api/logins', base_url='http://localhost:5500',
                environ_overrides={'REMOTE_ADDR': peer}).status_code, 200)
        response = self.client.get('/api/logins', base_url='http://localhost:5500',
            environ_overrides=self.remote,
            headers={'X-Forwarded-For': '127.0.0.1', 'Forwarded': 'for=127.0.0.1'})
        self.assertEqual(response.status_code, 401)
        for peer, base in (('2001:db8::2', 'http://[2001:db8::1]:5500'),
                           ('::ffff:192.168.31.90', self.base), ('8.8.8.8', 'http://1.2.3.4:5500')):
            self.assertEqual(self.client.get('/api/logins', base_url=base,
                environ_overrides={'REMOTE_ADDR': peer}).status_code, 401)

    def test_password_change_invalidates_other_cookies_and_requires_current_password(self):
        self.setup_password()
        other = self.app.test_client()
        self.assertEqual(self.post('/api/access/login', {'password': PASSWORD}, client=other).status_code, 200)
        updated = {'current_password': PASSWORD, 'password': 'new-workspace-456', 'confirmation': 'new-workspace-456'}
        self.assertEqual(self.post('/api/access/password', updated).status_code, 200)
        self.assertEqual(self.get('/api/logins').status_code, 200)
        self.assertEqual(self.get('/api/logins', client=other).status_code, 401)
        incorrect = {**updated, 'current_password': PASSWORD}
        response = self.post('/api/access/password', incorrect)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json['retry_after'], 5)

    def test_origin_json_strength_and_desktop_control_boundaries(self):
        data = {'password': PASSWORD, 'confirmation': PASSWORD}
        for headers in ({'Origin': 'http://evil.test'}, {'Origin': 'http://192.168.31.176:5600'}):
            self.assertEqual(self.client.post('/api/access/password', json=data, base_url=self.base,
                environ_overrides=self.remote, headers=headers).status_code, 403)
        self.assertEqual(self.client.post('/api/access/password', data=json.dumps(data),
            base_url=self.base, environ_overrides=self.remote, content_type='text/plain').status_code, 415)
        for invalid in ({'password': 'short', 'confirmation': 'short'},
                        {'password': PASSWORD, 'confirmation': 'other'}, {'password': None}):
            self.assertEqual(self.post('/api/access/password', invalid).status_code, 400)
        self.setup_password()
        self.assertEqual(self.get('/_desktop/window-events?id='+'a'*32).status_code, 403)
        self.assertEqual(self.post('/api/access/logout', {}).status_code, 200)
        self.assertEqual(self.get('/api/logins').status_code, 401)

    def test_security_status_never_discloses_hash_or_signing_material(self):
        self.setup_password()
        response = self.get('/api/access/status')
        self.assertEqual(set(response.json), {'configured', 'local', 'authenticated', 'retry_after'})
        for secret in (PASSWORD, self.security.data['password_hash'], self.security.data['secret']):
            self.assertNotIn(secret, response.get_data(as_text=True))

    def test_domain_access_works_and_cannot_bypass_password_through_loopback_dns(self):
        self.security.set_password(PASSWORD, '')
        for peer in ('2001:db8::2', '192.168.31.90', '127.0.0.1', '::1'):
            for domain in ('workspace.example.com', 'my-workspace.ddns.net', '1.2.3.4', '[2001:db8::1]'):
                base = 'http://'+domain+':5500'
                response = self.client.get('/api/logins', base_url=base, environ_overrides={'REMOTE_ADDR':peer})
                self.assertEqual(response.status_code, 401)
                status = self.client.get('/api/access/status', base_url=base, environ_overrides={'REMOTE_ADDR':peer})
                self.assertFalse(status.json['local'])
        base = 'http://workspace.example.com:5500'
        self.assertEqual(self.client.post('/api/access/login', json={'password':PASSWORD}, base_url=base,
            environ_overrides=self.remote, headers={'Origin':base}).status_code, 200)
        self.assertEqual(self.client.get('/api/logins', base_url=base, environ_overrides=self.remote).status_code, 200)
        self.assertEqual(self.client.get('/recharge/api/accounts', base_url=base, environ_overrides=self.remote).status_code, 200)

    def test_cooldown_is_shared_between_ipv4_ipv6_and_new_browser_cookies(self):
        self.security.set_password(PASSWORD, '')
        response = self.post('/api/access/login', {'password': 'incorrect'})
        self.assertEqual(response.json['retry_after'], 5)
        other = self.app.test_client()
        response = other.post('/api/access/login', json={'password': PASSWORD},
            base_url='http://[2001:db8::1]:5500', environ_overrides={'REMOTE_ADDR': '2001:db8::2'})
        self.assertEqual(response.status_code, 429)
        self.assertIn('Retry-After', response.headers)
        self.assertEqual(self.security.data['failures'], 1)


class AccessPersistenceTests(unittest.TestCase):
    def test_tampered_and_expired_access_cookies_cannot_authorize(self):
        from unittest.mock import patch
        security = AccessSecurity()
        with patch('itsdangerous.timed.TimestampSigner.get_timestamp', return_value=1000):
            token = security.set_password(PASSWORD, '')
            self.assertTrue(security.authenticated(token))
            self.assertFalse(security.authenticated('tampered.'+token))
        with patch('itsdangerous.timed.TimestampSigner.get_timestamp', return_value=1000+SESSION_SECONDS+1):
            self.assertFalse(security.authenticated(token))

    def test_all_backoff_steps_survive_restarts_without_extension_on_blocked_requests(self):
        with tempfile.TemporaryDirectory() as folder:
            storage = DatabaseLoginStorage(Path(folder)/'accounts.sqlite3')
            now = [1000.0]
            security = AccessSecurity(storage, now=lambda: now[0])
            token = security.set_password(PASSWORD, '')
            for delay in (*RETRY_SECONDS, 300, 300):
                with self.assertRaises(AccessError) as rejected:
                    security.login('incorrect')
                self.assertEqual(rejected.exception.retry_after, delay)
                deadline = security.data['next_attempt_at']
                security = AccessSecurity(storage, now=lambda: now[0])
                self.assertTrue(security.authenticated(token))
                with self.assertRaises(AccessError) as blocked:
                    security.login(PASSWORD)
                self.assertEqual(blocked.exception.status, 429)
                self.assertEqual(security.data['next_attempt_at'], deadline)
                now[0] += delay
            security.login(PASSWORD)
            self.assertEqual(security.data['failures'], 0)
            with self.assertRaises(AccessError) as rejected:
                security.login('incorrect')
            self.assertEqual(rejected.exception.retry_after, 5)
            serialized = storage.path.read_bytes()
            self.assertNotIn(PASSWORD.encode(), serialized)

    def test_parallel_wrong_passwords_only_verify_once_per_window(self):
        security = AccessSecurity(now=lambda: 1000)
        security.set_password(PASSWORD, '')
        def fail(_):
            try:
                security.login('incorrect')
            except AccessError as error:
                return error.status
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(fail, range(12)))
        self.assertEqual(results.count(401), 1)
        self.assertEqual(results.count(429), 11)
        self.assertEqual(security.data['failures'], 1)


if __name__ == '__main__':
    unittest.main()
