"""Pure Python reconstruction of the official Wheel request envelope.

Sources: wheel_bg.wasm SHA256 541c0fd95270c5db94228f1a84e5dca6d3508053f8eb3224d56432f70389ca91,
functions 135 (AES-GCM/RSA), 138 (envelope), 139 (verification),
174 and 136 (gDT HTTP and token history). No browser/JS/WASM runtime is used.
"""
from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
import json
import secrets
import time

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .environment import collect


VERSION = '1586234121417629696'
GDT_ACTIVITY = '109230918290'
PUBLIC_KEY = b'''-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAvrFoBJy9R5e63CbxObM2
/AmHpomnq2IQBjBryqOFeZUqWeJvUE9HAkEjj6sFNbCLgIIZyUaxjUB4Gc994EIz
32ZUes35EgGLXmoWIxhVWyVd/hB0lV+kSlnt7V4Gw9NB6ca6K/aH3KgW8LLyXOET
02soSK35mt8NOqbbCk/Na+2WG1u9+dd5ojFpfsl0PVve4MUGtI1CY3IUZUNrQTX7
DQN4uYSQVeIXmT/+lkDVCV049ttAUBIvfjUObmqPZ4/ERTfAW7jJ1SWQRRGf9i0G
zQDilr977q+4+lBF3BAiDrirfeRuIU9S2BS5clT68ldnkQEkxibaNq2Nheivyttv
yQIDAQAB
-----END PUBLIC KEY-----
'''
_public_key = serialization.load_pem_public_key(PUBLIC_KEY)


class SevenDayCryptoError(Exception):
    def __init__(self, message, code='PYTHON_CRYPTO_ERROR', status=502):
        super().__init__(message)
        self.code, self.status = code, status


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def verification(environment, timestamp):
    """Match the official func139, including its empty-object branch."""
    stamp = str(timestamp).encode()
    keys = sorted(environment)
    if not keys:
        return hashlib.sha256(stamp + b'empty_json').hexdigest()
    selected = keys[(abs(timestamp) % 10) % len(keys)]
    selected_hash = hashlib.sha256(selected.encode() + _json(environment[selected]) + stamp).digest()
    values = b''.join(key.encode() + _json(environment[key]) for key in keys)
    return hashlib.sha256(values + stamp + selected_hash + b'wcvsv').hexdigest()


def prepare(payload, environment, device_token='', history=(), *, timestamp=None, key=None, nonce=None):
    """Build one envelope; explicit key/nonce/time support offline vector tests.

    The first gDT request legitimately has an empty token/history. Business
    submissions must obtain a nonempty token through encrypt's normal HTTP step.
    """
    if not isinstance(payload, dict) or not isinstance(environment, dict):
        raise SevenDayCryptoError('活动请求与真实运行环境格式无效。', 'CRYPTO_INPUT', 400)
    stamp = int(time.time() * 1000) if timestamp is None else timestamp
    env = deepcopy(environment)
    env['device_token'] = list(history)
    frame = {'data': {**payload, 'deviceToken': device_token}, 'environment': env,
             'timestamp': stamp, 'verification': verification(env, stamp)}
    key = secrets.token_bytes(32) if key is None else key
    nonce = secrets.token_bytes(12) if nonce is None else nonce
    if len(key) != 32 or len(nonce) != 12:
        raise SevenDayCryptoError('活动加密密钥或随机数长度无效。', 'CRYPTO_INPUT', 400)
    content = base64.b64encode(nonce + AESGCM(key).encrypt(nonce, _json(frame), None)).decode()
    aurora = base64.b64encode(_public_key.encrypt(key, padding.PKCS1v15())).decode()
    return {'data': {'cocEnContent': content},
            'headers': {'coc-en-version': VERSION, 'coc-aurora': aurora}}


def encrypt(payload, cookies, user_agent, *, transport=None, environment=None):
    """Authenticate a fresh device context with the activity client's HTTP session.

    transport(body, headers) POSTs only the official /coc/wasmrc/gDT endpoint
    and returns its raw JSON. It owns cookies, network errors and account leases.
    A gDT failure never falls back to an empty token or submits business data.
    """
    if transport is None:
        raise SevenDayCryptoError('缺少官方活动会话，尚未获取设备凭据，本次没有提交。', 'GDT_TRANSPORT_REQUIRED', 503)
    source = collect if environment is None else environment
    initial_environment = source() if callable(source) else source
    initial = prepare({'activityId': GDT_ACTIVITY}, initial_environment)
    response = transport({'encryptedText': initial['data']['cocEnContent']}, {
        'Content-Type': 'application/json', 'coc-aurora': initial['headers']['coc-aurora']})
    if not isinstance(response, dict) or str(response.get('code')) != '0':
        detail = ('：' + str(response.get('message') or '官方未返回原因')[:160]
                  + '（' + str(response.get('code', 'UNKNOWN'))[:40] + '）') if isinstance(response, dict) else ''
        raise SevenDayCryptoError('官方设备认证未完成' + detail + '，本次没有提交活动请求。', 'GDT_REJECTED')
    token = response.get('data')
    if not isinstance(token, str) or not token.strip():
        raise SevenDayCryptoError('官方未返回设备凭据，本次没有提交活动请求。', 'GDT_TOKEN_MISSING')
    current_environment = source() if callable(source) else source
    result = prepare(payload, current_environment, token, [token])
    return {**result, 'cookies': [], 'user_agent': user_agent}


def shutdown():
    """Kept for the common activity lifecycle; pure Python owns no processes."""
