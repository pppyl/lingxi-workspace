"""Python wire protocol for the touch.10086.cn wallet ConstID SDK.

The retained official SDK is 1.1511.0.1973. Its ``param`` is JSON encoded with
CESU-8 and a custom Base64 alphabet, not an independently minted risk token.
Only successful c1 response data becomes ``stc-rrt-token``. Browser-only
fingerprint collection is deliberately an explicit provider boundary.
"""
import base64
from decimal import Decimal
import json
import math
import secrets
from threading import RLock
import time
from urllib.parse import quote, unquote_plus


ORIGIN = 'https://touch.10086.cn'
SERVER = ORIGIN + '/i/mobile/tfk/mgt/decision-engine/udid/c1'
APP_ID = 'e1f92650d20263d38dbe327e7237f516'
LID_COOKIE = '_zw_kvani5r'
TOKEN_COOKIE = 'stc-rrt-token'
ALPHABET = 'S0DOZN9bBJyPV-qczRa3oYvhGlUMrdjW7m2CkE5_FuKiTQXnwe6pg8fs4HAtIL1x='
STANDARD = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/='
LOCAL_ID_ALPHABET = '0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ'
DOMAIN = 'touch.10086.cn'


class CouponDeviceError(Exception):
    def __init__(self, message, code='RISK_INITIALIZATION_FAILED', *, status=None):
        super().__init__(message)
        self.code = code
        self.status = status


def _stringify(value):
    """JSON.stringify's scalar spelling for SDK request primitives."""
    if value is None:
        return 'null'
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            return 'null'
        if value == 0:
            return '0'
        text = repr(value)
        if 1e-6 <= abs(value) < 1e21:
            decimal_text = format(Decimal(text), 'f')
            return decimal_text.rstrip('0').rstrip('.') if '.' in decimal_text else decimal_text
        mantissa, exponent = text.lower().split('e')
        return mantissa.removesuffix('.0') + 'e' + ('+' if int(exponent) >= 0 else '-') + str(abs(int(exponent)))
    if isinstance(value, (list, tuple)):
        return '[' + ','.join(_stringify(item) for item in value) + ']'
    if isinstance(value, dict):
        return '{' + ','.join(_stringify(str(key)) + ':' + _stringify(item) for key, item in value.items()) + '}'
    raise TypeError('SDK 参数只能包含 JSON 值。')


def encode_param(payload):
    """Encode actual SDK request material; this never creates a risk token."""
    # The SDK processes UTF-16 code units separately, including surrogate pairs.
    units = _stringify(payload).encode('utf-16-le', 'surrogatepass')
    text = ''.join(chr(int.from_bytes(units[index:index+2], 'little')) for index in range(0, len(units), 2))
    encoded = base64.b64encode(text.encode('utf-8', 'surrogatepass')).decode('ascii')
    return encoded.translate(str.maketrans(STANDARD, ALPHABET))


def decode_param(param):
    """Offline inspection helper. Do not log its credential-bearing result."""
    raw = base64.b64decode(param.translate(str.maketrans(ALPHABET, STANDARD)), validate=True)
    text = raw.decode('utf-8', 'surrogatepass').encode('utf-16-le', 'surrogatepass').decode('utf-16-le', 'surrogatepass')
    return json.loads(text)


def _cookie_value(value):
    return isinstance(value, str) and bool(value) and len(value) <= 8192 and not any(char in value for char in '\r\n;')


class CouponDevice:
    """One account and authentication domain, using its existing wallet client.

    ``state`` must be encrypted by the caller when persisted. No lifetime is
    inferred from another activity, cookie duration, or an APP cookie version.
    Call ``invalidate`` only after the official service explicitly rejects this
    device credential; this class never replays a redemption or SMS operation.

    A fingerprint provider, when required by c1, returns genuine shortened SDK
    fields, e.g. ``ua``/``res``/``can``. Absence raises FINGERPRINT_REQUIRED;
    no simulated canvas, fabricated browser data, or ``#param`` fallback is used.
    """
    def __init__(self, account_key, client, *, state=None, fingerprint_provider=None,
                 clock=time.time, lock=None):
        if not isinstance(account_key, str) or not account_key:
            raise ValueError('设备凭据必须绑定账号。')
        if state is not None and (state.get('account_key') != account_key or state.get('domain') != DOMAIN
                or state.get('app_id') != APP_ID or not _cookie_value(state.get('lid'))
                or type(state.get('lid_type')) not in (int, str)
                or state.get('token') and not _cookie_value(state['token'])):
            raise ValueError('设备凭据与当前卡券账号或认证域不匹配。')
        self.account_key = account_key
        self.client = client
        self.fingerprint_provider = fingerprint_provider
        self.clock = clock
        self.lock = lock or RLock()
        self.lid = state['lid'] if state else ''
        self.lid_type = state['lid_type'] if state else '0'
        self.token = state.get('token', '') if state else ''
        if not self.lid:
            # A real existing wallet cookie takes precedence over a new local ID.
            for cookie in client.cookies.jar:
                if cookie.name == LID_COOKIE and cookie.domain.lstrip('.') == DOMAIN and cookie.path == '/' and not cookie.is_expired():
                    self.lid, self.lid_type = unquote_plus(cookie.value), 1
                    break

    def export_state(self):
        with self.lock:
            return {'account_key': self.account_key, 'domain': DOMAIN, 'app_id': APP_ID,
                    'lid': self.lid, 'lid_type': self.lid_type, 'token': self.token}

    def invalidate(self):
        with self.lock:
            self.token = ''
            for cookie in list(self.client.cookies.jar):
                if cookie.name == TOKEN_COOKIE and cookie.domain.lstrip('.') == DOMAIN:
                    self.client.cookies.jar.clear(cookie.domain, cookie.path, cookie.name)

    def _request(self, payload):
        headers = {'User-Agent': self.client.headers.get('User-Agent', ''),
                   'Accept': 'application/json, text/plain, */*',
                   'appId': APP_ID, 'param': encode_param(payload)}
        if self.client.headers.get('Referer'):
            headers['Referer'] = self.client.headers['Referer']
        # Native SDK XHR has no ownAjax business headers. Keep this exact wallet
        # Cookie jar and connection while restoring its business headers afterward.
        saved_headers = self.client.headers
        self.client.headers = headers
        try:
            response = self.client.request('GET', SERVER, timeout=(5, 30), allow_redirects=False)
        finally:
            self.client.headers = saved_headers
        if not 200 <= response.status_code < 300:
            raise CouponDeviceError('官方用券设备校验请求失败，请稍后主动重试。', status=response.status_code)
        try:
            result = response.json()
        except ValueError as error:
            raise CouponDeviceError('官方用券设备校验返回了无法识别的数据。') from error
        if not isinstance(result, dict) or type(result.get('status')) is not int:
            raise CouponDeviceError('官方用券设备校验返回了无效状态。')
        if result['status'] == -800:
            raise CouponDeviceError('官方提示请求频繁，请稍后主动重试。', 'RISK_RATE_LIMITED', status=-800)
        return result

    def prepare(self):
        with self.lock:
            if self.token:
                self.client.cookies.set(TOKEN_COOKIE, self.token, domain=DOMAIN, path='/i', secure=True)
                return self.token
            if not self.lid:
                # This is the SDK's documented local ID construction, not a
                # fallback token: milliseconds + 32 uniform alphabet characters.
                self.lid = str(int(self.clock()*1000)) + ''.join(secrets.choice(LOCAL_ID_ALPHABET) for _ in range(32))
            lid_type = self.lid_type
            self.client.cookies.set(LID_COOKIE, quote(self.lid, safe="~()*!.'-"), domain=DOMAIN, path='/', secure=False)
            self.lid_type = 1
            result = self._request({'lid': self.lid, 'lidType': lid_type, 'cache': True,
                                    'appKey': quote(APP_ID, safe="~()*!.'-")})
            if result['status'] == -3:
                raise CouponDeviceError('官方用券设备认证失败，请稍后主动重试。', status=-3)
            if result['status'] not in (1, 2):
                if result['status'] == -5:
                    raise CouponDeviceError('官方要求兼容设备校验传输，Python 该分支尚未接入，已保留短信和用券会话。',
                                            'RISK_TRANSPORT_REQUIRED', status=-5)
                if result['status'] == -4:
                    if not _cookie_value(result.get('data')):
                        raise CouponDeviceError('官方返回的设备标识无效。', status=-4)
                    self.lid = result['data']
                    self.client.cookies.set(LID_COOKIE, quote(self.lid, safe="~()*!.'-"), domain=DOMAIN, path='/', secure=False)
                if self.fingerprint_provider is None:
                    raise CouponDeviceError('官方需要设备环境校验；Python 环境采集尚未接入，已保留短信和用券会话。',
                                            'FINGERPRINT_REQUIRED', status=result['status'])
                fingerprint = self.fingerprint_provider()
                if not isinstance(fingerprint, dict) or not fingerprint or {'lid', 'lidType', 'cache', 'appKey', 'appId'} & fingerprint.keys():
                    raise ValueError('设备环境采集必须返回实际 SDK 字段，不能覆盖账号认证参数。')
                result = self._request({'lid': self.lid, 'lidType': 1, 'cache': True,
                                        **fingerprint, 'appKey': quote(APP_ID, safe="~()*!.'-")})
                if result['status'] != 2:
                    raise CouponDeviceError('官方用券设备环境校验未通过，请稍后主动重试。', status=result['status'])
            if not _cookie_value(result.get('data')):
                raise CouponDeviceError('官方未返回有效的用券设备凭据。', status=result['status'])
            self.token = result['data']
            self.client.cookies.set(TOKEN_COOKIE, self.token, domain=DOMAIN, path='/i', secure=True)
            return self.token
