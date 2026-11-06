"""Python transport and codec for the Wednesday activity's ConstID SDK.

Protocol source: the public activity's hooks.LE-uhCa-.js, ConstID 1.1511.0.
The lightweight cache request is implemented. A request requiring fingerprint
collection needs an explicit Python provider; no browser/JavaScript fallback or
locally fabricated ConstID is used. Persist ``export_state()`` through the
account's existing encrypted credential storage, never a shared browser store.
"""
from __future__ import annotations

import base64
from concurrent.futures import Future
import json
import secrets
import string
import threading
import time

from http_transport import requests


APP_ID = '9e11b6f9f78e14953febc4e3ce2ea609'
SERVER = 'https://yxfk.market.chinamobile.com:8081/mgt/decision-engine/udid/c1'
PAGE_ORIGIN = 'https://wx.10086.cn'
LID_COOKIE = '_zw_kvani5r'
SDK_TIMEOUT_SECONDS = 1.5
ALPHABET = 'S0DOZN9bBJyPV-qczRa3oYvhGlUMrdjW7m2CkE5_FuKiTQXnwe6pg8fs4HAtIL1x='
_STANDARD_ALPHABET = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/='
_ENCODE_TABLE = str.maketrans(_STANDARD_ALPHABET, ALPHABET)
_DECODE_TABLE = str.maketrans(ALPHABET, _STANDARD_ALPHABET)
_LID_ALPHABET = string.digits + string.ascii_lowercase + string.ascii_uppercase


class DeviceError(Exception):
    def __init__(self, message, code='DEVICE_ERROR', *, retryable=False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def encode_param(payload):
    """SDK JSON.stringify + UTF-16-code-unit UTF8 encoder + custom Base64.

    Supplementary Unicode characters use CESU-8, matching the SDK's two regular
    expression replacements rather than Python's ordinary four-byte UTF-8.
    SDK request fields use strings, booleans and integer detector values.
    """
    text = json.dumps(payload, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    units = text.encode('utf-16-le', errors='surrogatepass')
    data = ''.join(chr(units[index] | units[index + 1] << 8)
                   for index in range(0, len(units), 2)).encode('utf-8', errors='surrogatepass')
    return base64.b64encode(data).decode('ascii').translate(_ENCODE_TABLE)


def decode_param(param):
    """Decode retained SDK protocol fixtures without executing JavaScript."""
    text = base64.b64decode(param.translate(_DECODE_TABLE), validate=True).decode(
        'utf-8', errors='surrogatepass')
    text = text.encode('utf-16-le', errors='surrogatepass').decode('utf-16-le', errors='surrogatepass')
    return json.loads(text)


class DeviceSession:
    """One account, one authentication domain, with merged concurrent setup.

    The public SDK supplies no ConstID expiry. Unknown expiry remains ``None``;
    an imported official token survives restart and APP credential revisions.
    Call ``invalidate`` only for an explicit official invalid-token response.
    A fingerprint provider, if supplied, must collect the SDK detector fields
    in Python and return their original shortened names and values.
    """
    def __init__(self, account_key, *, http=None, state=None, user_agent='',
                 fingerprint_provider=None, clock=time.time):
        if not isinstance(account_key, str) or not account_key.strip():
            raise ValueError('设备会话必须绑定账号')
        if state is not None and (
                not isinstance(state, dict) or state.get('account_key') != account_key or
                state.get('app_id') != APP_ID or state.get('server') != SERVER or
                not isinstance(state.get('lid', ''), str) or
                not isinstance(state.get('const_id', ''), str) or
                state.get('expires_at') is not None and
                (isinstance(state['expires_at'], bool) or not isinstance(state['expires_at'], (int, float)))):
            raise ValueError('设备会话不属于当前账号或活动认证域')
        self.account_key = account_key
        self.http = http if http is not None else requests.Session(impersonate='chrome')
        self.user_agent = user_agent
        self.fingerprint_provider = fingerprint_provider
        self.clock = clock
        self._lock = threading.Lock()
        self._flight = None
        self._lid = (state or {}).get('lid', '')
        self._const_id = (state or {}).get('const_id', '')
        self._issued_at = (state or {}).get('issued_at')
        self._expires_at = (state or {}).get('expires_at')

    def export_state(self):
        with self._lock:
            return {'account_key': self.account_key, 'app_id': APP_ID, 'server': SERVER,
                    'lid': self._lid, 'const_id': self._const_id,
                    'issued_at': self._issued_at, 'expires_at': self._expires_at}

    def invalidate(self):
        """Forget an explicitly rejected official token, retaining device identity."""
        with self._lock:
            self._const_id = ''
            self._issued_at = self._expires_at = None

    def ensure(self):
        with self._lock:
            if self._const_id and (self._expires_at is None or self._expires_at > self.clock()):
                return self._const_id
            owner = self._flight is None
            if owner:
                self._flight = Future()
            flight = self._flight
        if not owner:
            return flight.result()
        try:
            token = self._acquire()
            with self._lock:
                self._const_id, self._issued_at, self._expires_at = token, self.clock(), None
            flight.set_result(token)
            return token
        except Exception as error:
            flight.set_exception(error)
            raise
        finally:
            with self._lock:
                self._flight = None

    def _acquire(self):
        # This is the SDK's makeLocalID algorithm, not a locally generated token.
        # The new LID is retained even when initialization cannot finish.
        with self._lock:
            fresh = not self._lid
            if fresh:
                self._lid = str(int(self.clock() * 1000)) + ''.join(
                    secrets.choice(_LID_ALPHABET) for _ in range(32))
            lid = self._lid
        payload = {'lid': lid, 'lidType': '0' if fresh else 1, 'cache': True, 'appKey': APP_ID}
        result = self._request(payload)
        status = result.get('status')
        if status in (1, 2):
            return self._token(result)
        if status == -3:
            raise DeviceError('官方设备认证拒绝了当前会话', 'DEVICE_AUTH_REJECTED')
        if status == -4 and isinstance(result.get('data'), str) and result['data']:
            with self._lock:
                self._lid = lid = result['data']
        if status == -5:
            raise DeviceError('官方设备认证要求额外交互，当前 Python 协议尚未完成该分支',
                              'DEVICE_INTERACTION_REQUIRED')
        if self.fingerprint_provider is None:
            raise DeviceError('官方设备认证要求环境采集，Python 环境采集协议尚未完成；请保留已有有效设备凭据',
                              'DEVICE_FINGERPRINT_REQUIRED')
        detector_data = self.fingerprint_provider()
        if not isinstance(detector_data, dict) or {'lid', 'lidType', 'cache', 'appId', 'appKey'} & detector_data.keys():
            raise ValueError('Python 环境采集结果包含无效字段')
        payload = {'lid': lid, 'lidType': 1, 'cache': True, **detector_data, 'appKey': APP_ID}
        result = self._request(payload)
        if result.get('status') != 2:
            raise DeviceError('官方设备认证未返回有效设备凭据', 'DEVICE_AUTH_REJECTED')
        return self._token(result)

    def _request(self, payload):
        headers = {'appId': APP_ID, 'param': encode_param(payload),
                   'Accept': 'application/json, text/plain, */*',
                   'Origin': PAGE_ORIGIN, 'Referer': PAGE_ORIGIN + '/'}
        if self.user_agent:
            headers['User-Agent'] = self.user_agent
        response = self.http.get(SERVER, headers=headers, timeout=SDK_TIMEOUT_SECONDS,
                                 allow_redirects=False)
        if response.status_code in (403, 429):
            raise DeviceError('官方设备认证限制了当前请求，请稍后主动重试', 'DEVICE_ACCESS_LIMIT')
        if response.status_code != 200:
            raise DeviceError(f'官方设备认证 HTTP {response.status_code}', 'DEVICE_HTTP_ERROR', retryable=True)
        result = response.json()
        if not isinstance(result, dict) or isinstance(result.get('status'), bool) or not isinstance(result.get('status'), int):
            raise DeviceError('官方设备认证返回格式无效', 'DEVICE_RESPONSE_INVALID')
        if result['status'] == -800:
            raise DeviceError('官方设备认证提示请求频繁，请稍后主动重试', 'DEVICE_ACCESS_LIMIT')
        return result

    @staticmethod
    def _token(result):
        token = result.get('data')
        if not isinstance(token, str) or not token or len(token) > 4096:
            raise DeviceError('官方设备认证未返回有效设备凭据', 'DEVICE_RESPONSE_INVALID')
        return token
