"""China Mobile login protocol recovered from the supplied APK.

AES, token and signature formulas match an observed APP request. Standalone
initialization, SMS, OTP login and Cookie-to-Lingxi exchange are live verified.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import secrets
import re
import time
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.asymmetric import padding as rsa_padding, rsa
try:
    from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
except ImportError:
    TripleDES = algorithms.TripleDES
from http_transport import requests, CurlHttpVersion
from http_transport import RequestException

REQUEST_KEY = b"bAIgvwAuA4tbDr9d"
RESPONSE_KEY = b"GS7VelkJl5IT1uwQ"
IV = b"9791027341711819"
REQUEST_KEY_14 = b"tVkdaRWRY0ZkV1Vr"
RESPONSE_KEY_14 = b"RYV0hCV1lV25KYVJ"
IV_14 = b"VjFSQ1ZtVkQxRTlQ"
APP_HOST = "https://client.app.coc.10086.cn"
SEND_SMS_PATH = "/biz-orange/LN/uamrandcode/sendMsgLoginNew"
LOGIN_PATH = "/biz-orange/LN/uamsmscode/login"
INIT_PATH = "/biz-orange/DN/init/startInit"
REFRESH_PATH = "/biz-orange/DN/refreshSession"
AUTH_REFRESH_PATH = "/biz-orange/LN/newuamauthlogin/login"


class AppLoginError(Exception):
    pass


class AppCredentialError(AppLoginError):
    def __init__(self, message, *, code='', login_required=False):
        super().__init__(message)
        self.code, self.login_required = code, login_required


def merge_app_cookie(previous: str, headers: list[str]) -> str:
    """Preserve the APP's embedded UID/ticketID when only JSESSIONID rotates."""
    parts = {}
    for value in [previous, *headers]:
        for part in value.split(';'):
            key, sep, content = part.strip().partition('=')
            if sep and key and '\r' not in part and '\n' not in part:
                parts[key.lower()] = (key, content)
    return '; '.join(key + '=' + value for key, value in parts.values())


def aes_encrypt(text: str, key: bytes = REQUEST_KEY, iv: bytes = IV) -> str:
    padder = padding.PKCS7(128).padder()
    data = padder.update(text.encode()) + padder.finalize()
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return base64.b64encode(enc.update(data) + enc.finalize()).decode()


def aes_decrypt(text: str, key: bytes = RESPONSE_KEY, iv: bytes = IV) -> str:
    dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    data = dec.update(base64.b64decode(text)) + dec.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return (unpadder.update(data) + unpadder.finalize()).decode()


def encrypt_phone(phone: str, now: datetime | None = None) -> str:
    config = json.loads(Path(__file__).with_name("app_login_public_key.json").read_text())
    key = rsa.RSAPublicNumbers(config["e"], int(config["n"])).public_key()
    stamp = (now or datetime.now(timezone(timedelta(hours=8)))).strftime("%Y%m%d%H%M%S")
    value = ("leadeon" + phone + stamp).encode()
    return base64.b64encode(key.encrypt(value, rsa_padding.PKCS1v15())).decode()


def md5(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


def client_id(android_id: str, mac: str = "02:00:00:00:00:00") -> str:
    # AppUtils.getClientId / Constant.commcon_keyByte. These labels are literal
    # strings in the shipped method, as confirmed against its DEX instructions.
    plain = ("oaid|#$deviceId|#$" + android_id + "|#$" + mac + "|#$null").encode()
    key = bytes((64,120,105,39,97,110,37,108,118,100,105,97,110,35,120,105,116,111,110,103,98,117,126,38))
    padder = padding.PKCS7(64).padder()
    plain = padder.update(plain) + padder.finalize()
    enc = Cipher(TripleDES(key), modes.CBC(b"01234567")).encryptor()
    return base64.b64encode(enc.update(plain) + enc.finalize()).decode()


def wire_request(url: str, envelope: dict, *, xk: str = "null", cookie: str = "", millis: int | None = None, nonce: str = "", version: int = 14) -> tuple[str, dict]:
    body = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    stamp = str(millis if millis is not None else int(time.time() * 1000))
    nonce = nonce or str(secrets.randbelow(90_000_000) + 10_000_000)
    token = aes_encrypt("_".join((xk or "null", urlsplit(url).path, stamp, nonce)))
    session_id = "null"
    for part in cookie.split(";"):
        k, sep, v = part.strip().partition("=")
        if sep and k.lower() == "jsessionid":
            session_id = v.strip()
            break
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "x-qen": str(version), "x-time": stamp, "x-nonce": nonce, "x-token": token,
        "xs": md5(url + "_" + body + "_Leadeon/SecurityOrganization"),
        "x-sign": md5("_".join((token, stamp, nonce, session_id))),
        "User-Agent": "okhttp/3.14.9 ChinaMobile/" + envelope["cv"] + " (Linux; Android " + envelope["sv"] + "; " + envelope["sn"] + ")",
    }
    if cookie:
        headers["Cookie"] = cookie
    return (aes_encrypt(body, REQUEST_KEY_14, IV_14) if version == 14 else aes_encrypt(body)), headers


@dataclass
class AppLoginSession:
    device_id: str = field(default_factory=lambda: secrets.token_hex(8))
    xk: str = field(default="null", repr=False)
    cookie: str = field(default="", repr=False)
    phone: str = field(default="", repr=False)
    sms_sent_at: float = 0
    initialized_at: float = 0
    app_authenticated: bool = False
    user_info: dict = field(default_factory=dict, repr=False)
    refresh_token: str = field(default='', repr=False)
    validity_term: str | int = field(default='')
    refreshed_at: float = 0

    @classmethod
    def from_credentials(cls, credentials):
        context = credentials.get('app_context') or {}
        return cls(device_id=context.get('device_id') or secrets.token_hex(8),
                   xk=context.get('xk') or 'null', cookie=credentials['cookie'], phone=credentials['phone'],
                   user_info=dict(credentials.get('profile') or {}), app_authenticated=True,
                   refresh_token=context.get('refreshToken') or '', validity_term=context.get('validityTerm') or '',
                   refreshed_at=context.get('refreshed_at') or 0)

    def export_context(self):
        return {'device_id':self.device_id, 'xk':self.xk, 'refreshToken':self.refresh_token,
                'validityTerm':self.validity_term, 'refreshed_at':self.refreshed_at}

    def capture_profile(self, value):
        profile = value.get('rspBody') or {}
        if isinstance(profile, str):
            try:
                profile = json.loads(profile)
            except ValueError as exc:
                raise AppCredentialError('APP 会话响应格式异常，将自动重试。') from exc
        if isinstance(profile, dict):
            self.user_info.update({key:profile[key] for key in ('provinceCode','cityCode','carrierOperator') if key in profile})
            if profile.get('refreshToken'):
                self.refresh_token = profile['refreshToken']
            if profile.get('validityTerm'):
                self.validity_term = profile['validityTerm']
            if profile.get('cookie') and 'JSESSIONID=' in profile['cookie']:
                self.cookie = merge_app_cookie(self.cookie, [profile['cookie']])

    def envelope(self, body: dict, *, login_component: bool = False) -> dict:
        value = {
            "ak": "F4AA34B89513F0D087CA0EF11A3277469DC74905", "cid": client_id(self.device_id), "ctid": client_id(self.device_id),
            "city": "", "prov": "", "cv": "12.5.6", "en": "0",
            "imei": "", "nt": "3", "packageName": "com.greenpoint.android.mc10086.activity",
            "reqBody": body, "sb": "Xiaomi", "sn": "2210132C", "sp": "1080x2250",
            "st": "1", "sv": "16", "t": md5(self.cookie) if self.cookie else "", "tel": "99999999999", "xc": "A2081", "xk": self.xk,
        }
        if login_component:
            value.update(appKey="00000159", baseFrameVersion="0.0.1", componentID="BI2021SA000055", componentVersion="2.0.0")
        return value

    def call(self, path: str, body: dict) -> tuple[int, dict, dict]:
        url = APP_HOST + path
        data, headers = wire_request(url, self.envelope(body, login_component="/LN/" in path), xk=self.xk, cookie=self.cookie)
        with requests.Session() as client:
            try:
                response = client.post(url, data=data, headers=headers, timeout=(10, 25), allow_redirects=False, http_version=CurlHttpVersion.V2_0)
            except RequestException as exc:
                raise AppLoginError("连接中国移动登录网关失败，请稍后重试。") from exc
            text = response.text
            pen = response.headers.get("x-pen", "")
            if response.status_code != 200:
                raise AppLoginError(f"APP 登录网关返回 HTTP {response.status_code}。")
            try:
                if pen == "2":
                    text = aes_decrypt(text)
                elif pen == "14":
                    text = aes_decrypt(text, RESPONSE_KEY_14, IV_14)
                value = json.loads(text)
            except ValueError as exc:
                raise AppLoginError("APP 登录网关响应无法解析，请稍后重试。") from exc
            if not isinstance(value, dict):
                raise AppLoginError("APP 登录网关响应格式不正确。")
            cookies = response.headers.get_list("set-cookie")
            if cookies and str(value.get("retCode")) == "000000":
                # The APP uses the complete Set-Cookie value, including its
                # embedded UID/ticketID fields, for its SSO exchange.
                self.cookie = merge_app_cookie(self.cookie, cookies)
            return response.status_code, value, {"x-pen": pen, "content-type": response.headers.get("content-type", "")}

    def initialization_body(self):
        return {
            "ssk": client_id(self.device_id), "imei": "", "provinceCode": "", "cityCode": "",
            "channelCode": "A2081", "pkgName": "com.greenpoint.android.mc10086.activity",
            "appName": "中国移动", "androidId": self.device_id[:16], "oaid": "",
            "supportAppStore": 1, "supportDeepLink": 1, "supportQuickApp": 1,
            "mac": "00:00:00:00:00:00", "deviceType": "1", "deviceWidth": "1080",
            "deviceHeight": "2400", "density": "440", "deviceId": "6694594664447",
            "appVersion": "12.5.6", "cellNums": "99999999999", "cellNum": "99999999999",
            "reqNumber": "1", "networkType": "3", "preloadNetworkType": "2", "recordFrequency": [],
        }

    def initialize(self):
        # Initialization may issue an anonymous Cookie; it must never replace
        # the authenticated session being renewed.
        authenticated_cookie = self.cookie if self.app_authenticated else ''
        try:
            _, value, _ = self.call(INIT_PATH, self.initialization_body())
        finally:
            if authenticated_cookie:
                self.cookie = authenticated_cookie
        self.require_success(value, "初始化")
        body = value.get("rspBody") or {}
        if isinstance(body, str):
            body = json.loads(body)
        if isinstance(body, dict) and body.get("ssv"):
            self.xk = body["ssv"]
        if self.xk == "null":
            raise AppLoginError("登录初始化缺少凭据，请稍后重试。")
        self.initialized_at = time.monotonic()
        return value

    def refresh(self, *, force_identity=False):
        """APP 90000 keepalive, then bounded token auth after server timeout.

        No OTP endpoint is called. Provider business errors remain retryable;
        only the APP's documented termination/token codes require login.
        """
        if not self.cookie or not self.phone:
            raise AppCredentialError('账号缺少 APP 凭据，请重新验证。', login_required=True)
        if self.xk == 'null' or not self.xk:
            self.initialize()
        _, value, _ = self.call(REFRESH_PATH, {'cellNum':self.phone})
        code = str(value.get('retCode','unknown'))
        if code == '410000' or force_identity and code == '000000':
            if not self.refresh_token:
                message = '账号缺少身份续期凭据，请重新验证一次。' if force_identity and code == '000000' else '旧 APP 会话已超时，且旧版记录缺少续期凭据，请重新验证一次。'
                raise AppCredentialError(message, code=code, login_required=True)
            _, value, _ = self.call(AUTH_REFRESH_PATH, {
                'cellNum':encrypt_phone(self.phone), 'imei':'', 'devToken':'', 'sendSmsFlag':'0',
                'loginType':'80028' if 'sessiontype=' in self.cookie.lower() else '60028',
                'refreshToken':self.refresh_token, 'pageUrl':'ZF10000', 'channelId':'1', 'reqSource':'appStart',
            })
            code = str(value.get('retCode','unknown'))
        if code != '000000':
            required = code in {'420000','400007','220230','214265','214100','214101','214102'}
            safe_code = code if re.fullmatch(r'[A-Za-z0-9_-]{1,32}',code) else 'unknown'
            message = '官方要求重新验证账号' if required else 'APP 自动续期暂未完成，将自动重试'
            raise AppCredentialError(f'{message}（{safe_code}）。',code=safe_code,login_required=required)
        self.capture_profile(value)
        self.refreshed_at = time.time()
        self.app_authenticated = True
        return self.cookie

    @staticmethod
    def require_success(value: dict, action: str):
        code = str(value.get("retCode", "unknown"))
        if code != "000000":
            # Provider descriptions can contain account information. Do not
            # pass them to the browser or logs; retain only a bounded code.
            safe_code = code if re.fullmatch(r"[A-Za-z0-9_-]{1,32}", code) else "unknown"
            if code in ("214100", "214101", "214102"):
                raise AppLoginError(f"中国移动要求额外安全验证（{safe_code}），当前网页登录暂不支持该验证。")
            raise AppLoginError(f"中国移动{action}未通过（{safe_code}）。请检查输入，勿连续重试。")

    def send_sms(self, phone: str):
        if not isinstance(phone, str) or not re.fullmatch(r"1[3-9][0-9]{9}", phone):
            raise AppLoginError("请输入正确的 11 位手机号。")
        if self.sms_sent_at and time.monotonic() - self.sms_sent_at < 60:
            raise AppLoginError("验证码发送间隔为 60 秒，请稍候。")
        # Record the attempt before sending: a network timeout may still have
        # delivered the SMS, so never automatically retry a send request.
        self.sms_sent_at = time.monotonic()
        self.app_authenticated = False
        if phone != self.phone:
            self.cookie = ""
        self.phone = ""
        if not self.initialized_at or time.monotonic() - self.initialized_at > 600:
            self.initialize()
        _, value, _ = self.call(SEND_SMS_PATH, {"cellNum": phone, "pageUrl": "LL0001", "channelId": "1"})
        self.require_success(value, "发送验证码")
        self.phone = phone
        return {"sent": True, "phone_masked": phone[:3] + "****" + phone[-4:], "retry_after": 60}

    def login(self, phone: str, code: str) -> str:
        if not self.phone or phone != self.phone:
            raise AppLoginError("请先向该手机号发送验证码。")
        if self.app_authenticated and self.cookie:
            return self.cookie
        if time.monotonic() - self.sms_sent_at > 600:
            self.phone = ""
            raise AppLoginError("验证码已超过 10 分钟，请重新获取。")
        if not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code):
            raise AppLoginError("请输入 6 位短信验证码。")
        _, value, _ = self.call(LOGIN_PATH, {"cellNum": encrypt_phone(phone), "verifyCode": code, "imei": "", "sendSmsFlag": "0", "devToken": "", "pageUrl": "LL0001", "channelId": "1"})
        self.require_success(value, "登录")
        if not self.cookie or "JSESSIONID=" not in self.cookie:
            raise AppLoginError("登录响应缺少 APP 会话，无法连接灵犀。")
        self.app_authenticated = True
        self.capture_profile(value)
        self.refreshed_at = time.time()
        return self.cookie
