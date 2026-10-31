"""Global Tone Star Day SSO through the APP's checkUID bridge."""
from contextlib import contextmanager, nullcontext
import base64
from datetime import datetime, timedelta, timezone
import re
import time
from threading import RLock

from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app_login import AppLoginError, AppLoginSession
from http_transport import requests
from recharge.cmcc import APP_UA


ORIGIN = 'https://qqt.cmicrwx.cn'
PLATFORM = ORIGIN + '/2016tyjf/xhmqqthy'
ENTRY_URL = PLATFORM + '/res/wap/xdrNormal.html?sspId=75628'
CHECK_UID_PATH = '/biz-orange/LN/checkUIDAction/checkUID'
# InAlgoEncryptTEL uses rsa_s1, while the SMS-login Encrypt path uses rsa_s.
UID_PUBLIC_MODULUS = 114512387112779088361433476536107433324866010397202326099006996441402171068665405936265402380230135606111502335414309866692637875723467375466815160479383764510644997690933726692443658650748034123556148312806692748125585902936431602753059235580873529762252918450460010900806924390768877421758540880404865098509


def encrypt_uid(uid, now=None):
    stamp = (now or datetime.now(timezone(timedelta(hours=8)))).strftime('%Y%m%d%H%M%S')
    key = rsa.RSAPublicNumbers(65537, UID_PUBLIC_MODULUS).public_key()
    return base64.b64encode(key.encrypt(('leadeon' + uid + stamp).encode(), padding.PKCS1v15())).decode()


class StarDayAuthError(Exception):
    def __init__(self, message, code='STAR_DAY_AUTH_ERROR', status=502):
        super().__init__(message)
        self.code, self.status = code, status


class StarDaySessionCache:
    """Keep campaign cookies separate from rotating primary APP credentials."""
    def __init__(self, storage=None, *, now=time.time):
        self.storage, self.now = storage, now
        self.entries, self.locks, self.lock = {}, {}, RLock()

    def account_lock(self, login_id):
        with self.lock:
            return self.locks.setdefault(login_id, RLock())

    def load(self, login_id, phone):
        with self.lock:
            if login_id not in self.entries:
                self.entries[login_id] = self.storage.load_activity_data('star-day-session:'+login_id) if self.storage else None
            saved = self.entries[login_id]
            if saved and saved['phone'] == phone and saved['expires_at'] > self.now():
                return saved
            return None

    def save(self, login_id, phone, client, *, source='app'):
        stamp = self.now()
        cookies = [{'name': c.name, 'value': c.value, 'domain': c.domain, 'path': c.path,
                    'secure': c.secure, 'expires': c.expires} for c in client.cookies.jar]
        expiry = min([stamp+7200]+[c['expires'] for c in cookies if c['name'] == 'access_token' and c['expires'] is not None])
        saved = {'phone': phone, 'created_at': stamp, 'expires_at': expiry, 'cookies': cookies, 'source':source}
        with self.lock:
            if self.storage:
                self.storage.save_activity_data('star-day-session:'+login_id, saved)
            self.entries[login_id] = saved

    def forget(self, login_id):
        with self.account_lock(login_id), self.lock:
            if self.storage:
                self.storage.delete_activity_data('star-day-session:'+login_id)
            self.entries.pop(login_id, None)


@contextmanager
def star_day_session(credentials_provider, *, session_factory=None, cache=None, force=False, allow_login=True):
    """Yield a campaign session and the exact shared APP credential snapshot."""
    snapshot = credentials_provider()
    cookie = snapshot.get('cookie', '') if isinstance(snapshot, dict) else ''
    uid_match = re.search(r'(?:^|;)\s*UID=([^;]+)', cookie, re.I)
    if (not uid_match or not re.search(r'(?:^|;)\s*JSESSIONID=', cookie, re.I)
            or not re.fullmatch(r'1[3-9]\d{9}', str(snapshot.get('phone', '')))):
        raise StarDayAuthError('共享 APP 登录不可用，请重新登录账号', 'APP_LOGIN_REQUIRED', 401)
    uid = uid_match.group(1)
    login_id = snapshot.get('id') or snapshot['phone']

    def current():
        latest = credentials_provider()
        if (latest is None or latest.get('cookie') != snapshot['cookie']
                or latest.get('phone') != snapshot['phone']):
            raise StarDayAuthError('共享账号登录已变更，请使用最新登录态重试', 'APP_CREDENTIALS_CHANGED', 409)

    with cache.account_lock(login_id) if cache is not None else nullcontext():
        client = session_factory() if session_factory else requests.Session(impersonate='chrome')
        client.headers.update({'User-Agent': APP_UA, 'Origin': ORIGIN, 'Referer': ENTRY_URL})
        try:
            current()
            saved = cache.load(login_id, snapshot['phone']) if cache is not None and not force else None
            if saved:
                for cookie in saved['cookies']:
                    client.cookies.set(cookie['name'], cookie['value'], domain=cookie['domain'], path=cookie['path'])
                yield client, snapshot
                return
            if not allow_login:
                raise StarDayAuthError('等待每日凭据准备时间，或打开星动日页面后获取活动凭据。', 'CAMPAIGN_LOGIN_PENDING', 409)
            flow = AppLoginSession.from_credentials(snapshot)
            try:
                _, response, _ = flow.call(CHECK_UID_PATH, {
                    'channelId': '12063', 'domain': ORIGIN, 'url': ENTRY_URL,
                    'tel': snapshot['phone'], 'uId': uid,
                })
            except AppLoginError as error:
                raise StarDayAuthError('APP 星动日认证暂未完成，请稍后重试', 'APP_UID_FAILED') from error
            code = str(response.get('retCode', ''))
            if code != '000000':
                login_required = code == '233013'
                raise StarDayAuthError('APP 登录已失效，请重新登录' if login_required else 'APP 星动日认证暂未完成（' + code + '）',
                                       'APP_LOGIN_REQUIRED' if login_required else 'APP_UID_FAILED',
                                       401 if login_required else 502)
            current()
            try:
                result = client.post(PLATFORM + '/leadeonLogin.action', data={
                    'token': encrypt_uid(uid), 'sspId': '75628',
                }, timeout=(10, 25), allow_redirects=False)
            except requests.RequestsError as error:
                raise StarDayAuthError('连接星动日认证服务失败，请稍后重试', 'UPSTREAM_NETWORK') from error
            if result.status_code != 200:
                raise StarDayAuthError('星动日认证 HTTP ' + str(result.status_code), 'AUTH_HTTP')
            try:
                value = result.json()
            except ValueError as error:
                raise StarDayAuthError('星动日认证响应格式异常', 'AUTH_FORMAT') from error
            if not isinstance(value, dict) or str(value.get('status')) != '1':
                code = str(value.get('status', 'unknown')) if isinstance(value, dict) else 'unknown'
                if code == '-800':
                    raise StarDayAuthError('官方提示请求频繁（-800），已停止后台重试，等待下一次定时准备。', 'UPSTREAM_RATE_LIMITED',429)
                raise StarDayAuthError('星动日活动认证未通过（官方状态 '+code[:20]+'）；自动检查每日一次，此结果不代表 APP 登录失效。', 'AUTH_FAILED')
            if not any(cookie.name == 'access_token' and cookie.value for cookie in client.cookies.jar):
                raise StarDayAuthError('星动日认证缺少活动会话', 'AUTH_FORMAT')
            current()
            if cache is not None:
                cache.save(login_id, snapshot['phone'], client)
            yield client, snapshot
        finally:
            client.close()
