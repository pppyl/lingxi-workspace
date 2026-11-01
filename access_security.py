"""One access boundary for the workspace and all mounted business apps."""
from __future__ import annotations

import json
import math
import secrets
import time
from threading import RLock
from urllib.parse import urlencode

from flask import jsonify, make_response, render_template, request
from itsdangerous import BadSignature, URLSafeTimedSerializer
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import redirect
from werkzeug.wrappers import Request, Response

from network_access import classify_access_host, is_loopback_peer

COOKIE_NAME = 'workspace_access'
SESSION_SECONDS = 7 * 24 * 3600
RETRY_SECONDS = (5, 10, 30, 60, 120, 180, 300)


class AccessError(Exception):
    def __init__(self, message, *, status=400, code='ACCESS_INVALID', retry_after=0):
        super().__init__(message)
        self.status, self.code, self.retry_after = status, code, retry_after


class AccessSecurity:
    def __init__(self, storage=None, *, now=None):
        self.storage, self.now, self.lock = storage, now or time.time, RLock()
        self.data = (storage.load_access_security() if storage is not None else None) or {
            'password_hash': '', 'secret': secrets.token_hex(32),
            'revision': secrets.token_hex(16), 'failures': 0, 'next_attempt_at': 0,
        }
        self.signer = URLSafeTimedSerializer(self.data['secret'], salt='workspace-access-v1')
        self._save()

    def _save(self):
        if self.storage is not None:
            self.storage.save_access_security(self.data)

    def retry_after(self):
        return max(0, math.ceil(self.data['next_attempt_at'] - self.now()))

    def authenticated(self, token):
        if not token or not self.data['password_hash']:
            return False
        try:
            payload = self.signer.loads(token, max_age=SESSION_SECONDS)
        except BadSignature:
            return False
        return isinstance(payload, dict) and payload.get('revision') == self.data['revision']

    def status(self, local, token):
        with self.lock:
            return {'configured': bool(self.data['password_hash']), 'local': local,
                    'authenticated': self.authenticated(token), 'retry_after': self.retry_after()}

    def _verify(self, password):
        # A single shared password has one cooldown, regardless of IP or cookie.
        remaining = self.retry_after()
        if remaining:
            raise AccessError(f'请等待 {remaining} 秒后再试。', status=429,
                              code='ACCESS_RATE_LIMITED', retry_after=remaining)
        if not check_password_hash(self.data['password_hash'], password):
            self.data['failures'] += 1
            delay = RETRY_SECONDS[min(self.data['failures'], len(RETRY_SECONDS)) - 1]
            self.data['next_attempt_at'] = self.now() + delay
            self._save()
            raise AccessError(f'访问密码错误，请等待 {delay} 秒后再试。', status=401,
                              code='ACCESS_PASSWORD_INCORRECT', retry_after=delay)

    def login(self, password):
        with self.lock:
            if not self.data['password_hash']:
                raise AccessError('首次访问需要先设置访问密码。', status=428, code='ACCESS_SETUP_REQUIRED')
            self._verify(password)
            self.data.update(failures=0, next_attempt_at=0)
            self._save()
            return self.signer.dumps({'revision': self.data['revision'], 'nonce': secrets.token_hex(16)})

    def set_password(self, password, current_password):
        with self.lock:
            if self.data['password_hash']:
                self._verify(current_password)
            self.data.update(password_hash=generate_password_hash(password), revision=secrets.token_hex(16),
                             failures=0, next_attempt_at=0)
            self._save()
            return self.signer.dumps({'revision': self.data['revision'], 'nonce': secrets.token_hex(16)})


class AccessGate:
    PUBLIC = frozenset(('/access', '/api/access/status', '/api/access/login',
                        '/api/access/logout', '/static/access.js', '/static/access.css', '/favicon.ico'))

    def __init__(self, application, security):
        self.application, self.security = application, security

    def __call__(self, environ, start_response):
        incoming = Request(environ)
        allowed, local_name = classify_access_host(incoming.host)
        local = local_name and is_loopback_peer(incoming.remote_addr)
        environ['workspace.local_access'] = local
        error = None
        if not allowed:
            error = Response('不支持此访问地址。', status=403)
        elif incoming.path.startswith('/_desktop/') and not local:
            error = Response('客户端控制接口仅限本机访问。', status=403)
        elif local or incoming.path in self.PUBLIC:
            return self.application(environ, start_response)
        else:
            state = self.security.status(local, incoming.cookies.get(COOKIE_NAME))
            if incoming.path == '/api/access/password' and not state['configured']:
                return self.application(environ, start_response)
            if state['authenticated']:
                return self.application(environ, start_response)
            mode = 'login' if state['configured'] else 'setup'
            target = '/access?' + urlencode({'next': incoming.full_path.rstrip('?')})
            if incoming.method not in ('GET', 'HEAD') or '/api/' in incoming.path:
                error = Response(json.dumps({
                    'error': '请输入访问密码。' if mode == 'login' else '首次访问需要先设置访问密码。',
                    'code': 'ACCESS_LOGIN_REQUIRED' if mode == 'login' else 'ACCESS_SETUP_REQUIRED',
                    'redirect': target,
                }, ensure_ascii=False), status=401 if mode == 'login' else 428, mimetype='application/json')
            else:
                error = redirect(target, code=303)
            error.headers['X-Workspace-Access'] = mode
        error.headers['Cache-Control'] = 'no-store'
        error.headers['X-Content-Type-Options'] = 'nosniff'
        error.headers['Content-Security-Policy'] = "default-src 'none'; frame-ancestors 'none'"
        return error(environ, start_response)


def register_access(app, storage):
    security = AccessSecurity(storage)
    app.extensions['access_security'] = security

    def signed_in(token, message):
        response = make_response(jsonify(message=message))
        response.set_cookie(COOKIE_NAME, token, max_age=SESSION_SECONDS, httponly=True,
                            secure=request.is_secure, samesite='Strict', path='/')
        return response

    @app.errorhandler(AccessError)
    def access_error(error):
        response = jsonify(error=str(error), code=error.code, retry_after=error.retry_after)
        response.status_code = error.status
        if error.retry_after:
            response.headers['Retry-After'] = str(error.retry_after)
        return response

    @app.get('/access')
    def access_page():
        return render_template('access.html')

    @app.get('/api/access/status')
    def access_status():
        return jsonify(security.status(request.environ['workspace.local_access'], request.cookies.get(COOKIE_NAME)))

    @app.post('/api/access/login')
    def access_login():
        password = request.get_json().get('password')
        if not isinstance(password, str) or not 1 <= len(password) <= 128:
            raise AccessError('请输入访问密码（不超过 128 位）。')
        return signed_in(security.login(password), '登录成功。')

    @app.post('/api/access/password')
    def access_password():
        payload = request.get_json()
        password, confirmation, current = payload.get('password'), payload.get('confirmation'), payload.get('current_password', '')
        if not isinstance(password, str) or not 8 <= len(password) <= 128:
            raise AccessError('访问密码需要 8～128 位。')
        if password != confirmation:
            raise AccessError('两次输入的密码不一致。')
        if not isinstance(current, str) or len(current) > 128:
            raise AccessError('请输入当前访问密码。')
        return signed_in(security.set_password(password, current), '访问密码已保存，其他设备需使用新密码重新登录。')

    @app.post('/api/access/logout')
    def access_logout():
        response = jsonify(message='已退出远程访问。')
        response.delete_cookie(COOKIE_NAME, path='/', samesite='Strict')
        return response

    return security
