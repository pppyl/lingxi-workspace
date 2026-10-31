"""Fixed-origin client for the observed Wednesday recharge activity."""
from __future__ import annotations

import atexit
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, urljoin

from http_transport import requests
from app_login import AppLoginSession, AppLoginError
from runtime_paths import RECHARGE_DATA_DIR

ROOT = Path(__file__).resolve().parent
ENTRY = 'https://wx.10086.cn/hlwyxhdhub/act-wedrecharge/1024101716'
BASE = 'https://wx.10086.cn/hlwyxhdhub/api/wedrecharge/'
SHANGHAI = timezone(timedelta(hours=8))
ENDPOINTS = {'_pub/rec_login', 'queryActivityInfo', 'queryDrawPrizeInfo', 'getPrizeList', 'drawPrize'}
APP_UA = ('Mozilla/5.0 (Linux; Android 16; 2210132C; wv) AppleWebKit/537.36 '
          '(KHTML, like Gecko) Version/4.0 Chrome/145.0.0.0 Mobile Safari/537.36 leadeon/12.5.6')
RD_STATES = {0: '已参与', 1: '可参与', 2: '未中奖', 3: '发放中', 4: '去使用',
             5: '已使用', 6: '未到活动日', 7: '尚未开始', 10: '已锁定'}


class ActivityError(Exception):
    def __init__(self, message, code='ACTIVITY_ERROR', status=400, *, retryable=None):
        super().__init__(message)
        self.code, self.status = code, status
        self.retryable = retryable


def validate_entry(value):
    value = (value or ENTRY).strip()
    parts = urlsplit(value)
    if (parts.scheme != 'https' or parts.netloc != 'wx.10086.cn' or
            parts.path not in {'/hlwyxhdhub/act-wedrecharge/1024101716',
                               '/hlwyxhdhub/act-wedrecharge/index.html'} or len(value) > 4096):
        raise ActivityError('请输入此活动的 wx.10086.cn 官方链接', 'INVALID_URL')
    return urlunsplit(parts)


def parse_server_time(value):
    """No local-clock fallback when authorizing a claim."""
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, (int, float)) or re.fullmatch(r'\d{10}|\d{13}', str(value)):
            number = float(value)
            return datetime.fromtimestamp(number / 1000 if number > 1e11 else number, SHANGHAI)
        text = str(value).strip()
        if re.fullmatch(r'\d{14}', text):
            return datetime.strptime(text, '%Y%m%d%H%M%S').replace(tzinfo=SHANGHAI)
        result = datetime.fromisoformat(text.replace('Z', '+00:00'))
        return result.replace(tzinfo=SHANGHAI) if result.tzinfo is None else result.astimezone(SHANGHAI)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def availability(user, draw):
    hour = 10 if str(user.get('provinceCode')) in {'891', '991'} else 8
    current = parse_server_time(draw.get('currentTime'))
    try:
        rd = int(draw.get('rdStatus'))
    except (TypeError, ValueError):
        rd = None
    next_start = None
    if current:
        days = (2 - current.weekday()) % 7
        next_start = (current + timedelta(days=days)).replace(hour=hour, minute=0, second=0, microsecond=0)
        if next_start <= current:
            next_start += timedelta(days=7)
    if not current:
        reason = '未取得官方服务器时间，请刷新状态'
    elif current.weekday() != 2:
        reason = '每周三开放领取'
    elif current.hour < hour:
        reason = f'本场将在今天 {hour:02d}:00 开始'
    elif draw.get('getPrize'):
        reason = '本场已有领取结果，请查看我的奖品'
    elif rd != 1:
        reason = RD_STATES.get(rd, '官方暂未允许领取，请刷新状态')
    else:
        reason = '当前可提交一次领取，结果以官方接口为准'
    can_claim = bool(current and current.weekday() == 2 and current.hour >= hour
                     and rd == 1 and not draw.get('getPrize'))
    return {'can_claim': can_claim, 'reason': reason, 'rd_status': rd,
            'state_label': RD_STATES.get(rd, '状态未知'), 'start_hour': hour,
            'server_time': current.isoformat() if current else None,
            'next_start': next_start.isoformat() if next_start else None}


def mask_phone(value):
    return re.sub(r'(\d{3})\d{4}(\d{4})', r'\1****\2', str(value or ''))


def public_prize(prize):
    if not isinstance(prize, dict):
        return None
    fields = ('prizeId', 'name', 'prizeName', 'shortName', 'subTitle', 'prizeType',
              'sendStatus', 'getTime', 'countdown', 'date', 'subscript')
    return {key: prize[key] for key in fields if key in prize}


class BrowserBridge:
    def __init__(self, profile_dir=None):
        self.profile_dir = Path(profile_dir or RECHARGE_DATA_DIR/'browser-profile').resolve()
        self.process = None
        self.lock = threading.Lock()
        self.lines = queue.Queue()
        self.serial = 0

    def _read(self, process, target):
        for line in process.stdout:
            try:
                target.put(json.loads(line))
            except ValueError:
                continue
        target.put(None)

    def rpc(self, command, **params):
        with self.lock:
            if self.process is None or self.process.poll() is not None:
                node = os.environ.get('CMCC_NODE_PATH') or shutil.which('node')
                if not node or not (ROOT / 'node_modules/playwright-core').exists():
                    raise ActivityError('浏览器组件未安装，请运行 npm install', 'BROWSER_MISSING', 503)
                flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
                self.process = subprocess.Popen([node, str(ROOT / 'browser_bridge.mjs')], cwd=ROOT,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    text=True, encoding='utf-8', creationflags=flags,
                    env={**os.environ, 'CMCC_PROFILE_DIR': str(self.profile_dir)})
                self.lines = queue.Queue()
                threading.Thread(target=self._read, args=(self.process, self.lines), daemon=True).start()
            self.serial += 1
            request_id = self.serial
            try:
                self.process.stdin.write(json.dumps({'id': request_id, 'command': command, **params}) + '\n')
                self.process.stdin.flush()
                deadline = time.monotonic() + 40
                while True:
                    reply = self.lines.get(timeout=max(0.01, deadline - time.monotonic()))
                    if reply is None:
                        raise ActivityError('浏览器组件已退出，请重新打开官方登录窗口', 'BROWSER_CLOSED', 503)
                    if reply.get('id') == request_id:
                        break
                    if time.monotonic() >= deadline:
                        raise queue.Empty
            except (queue.Empty, BrokenPipeError, OSError):
                raise ActivityError('浏览器响应超时，请检查官方窗口；领取请求不会自动重试', 'BROWSER_TIMEOUT', 504)
            if not reply.get('ok'):
                code = reply.get('code', 'BROWSER_ERROR')
                raise ActivityError(reply.get('error', '浏览器调用失败'), code, 502,
                                    retryable=False if code == 'HTTP_ACCESS_LIMIT' else None)
            return reply['result']

    def stop(self):
        if self.process and self.process.poll() is None:
            try:
                self.rpc('close')
                self.process.wait(timeout=5)
            except Exception:
                self.process.terminate()


class ActivityClient:
    def __init__(self, profile_dir=None, *, app_credentials_provider=None, credential_cache=None):
        self.mode = 'none'
        self.http = requests.Session(impersonate='chrome')
        if os.environ.get('LINGXI_ANDROID') == '1':
            from android_activity_bridge import AndroidActivityBridge
            self.bridge = AndroidActivityBridge(profile_dir)
        else:
            self.bridge = BrowserBridge(profile_dir)
        self.const_id = ''
        self.last_claim = 0.0
        self.uncertain = False
        self.total_submissions = 0
        self.cached_user = None
        self.user_checked_at = 0.0
        self.clock_anchor = None
        self.claim_lock = threading.Lock()
        self.operation_lock = threading.RLock()
        self.app_login = AppLoginSession()
        self.app_material = None
        self.app_credentials_provider = app_credentials_provider
        self.credential_cache = credential_cache
        self.app_credential_revision = None
        atexit.register(self.bridge.stop)

    def send_sms(self, phone):
        try:
            return self.app_login.send_sms(phone)
        except AppLoginError as error:
            raise ActivityError(str(error), 'APP_LOGIN_FAILED') from error

    def login_app(self, phone, code):
        try:
            cookie = self.app_login.login(phone, code)
        except AppLoginError as error:
            raise ActivityError(str(error), 'APP_LOGIN_FAILED') from error
        return self.connect_app(cookie, phone, self.app_login.user_info)

    @staticmethod
    def _official_url(value, base=ENTRY):
        target = urljoin(base, value)
        parsed = urlsplit(target)
        if parsed.scheme != 'https' or parsed.netloc != 'wx.10086.cn' or parsed.username or parsed.password:
            raise ActivityError('官方 APP 登录返回了不支持的跳转地址', 'APP_SSO_REDIRECT')
        return target

    @classmethod
    def _follow(cls, client, url):
        for _ in range(10):
            url = cls._official_url(url)
            response = client.get(url, timeout=(10, 20), allow_redirects=False)
            if response.status_code in {301, 302, 303, 307, 308}:
                url = cls._official_url(response.headers.get('location', ''), url)
                continue
            if response.status_code != 200:
                raise ActivityError(f'活动 APP 登录入口 HTTP {response.status_code}', 'APP_SSO_HTTP', 502)
            return response
        raise ActivityError('活动 APP 登录跳转次数过多', 'APP_SSO_REDIRECT')

    def _app_credential_snapshot(self):
        if self.app_credentials_provider is None:
            latest = self.app_material
        else:
            try:
                latest = self.app_credentials_provider()
            except ActivityError:
                raise
            except Exception as error:
                raise ActivityError('暂时无法读取共享账号凭据，请稍后重试', 'APP_CREDENTIALS_UNAVAILABLE', 503, retryable=True) from error
            if latest is None:
                raise ActivityError('共享账号已退出或不存在，请重新选择账号', 'APP_LOGIN_REQUIRED', 401)
        if not latest:
            raise ActivityError('没有可复用的 APP 凭据，请在账号管理登录', 'APP_LOGIN_REQUIRED', 401)
        if not isinstance(latest, dict) or not isinstance(latest.get('profile') or {}, dict):
            raise ActivityError('共享账号凭据格式无效，请稍后重试', 'APP_CREDENTIALS_UNAVAILABLE', 503, retryable=True)
        return {**latest, 'profile':dict(latest.get('profile') or {})}

    def _check_current_credentials(self, cookie, phone, profile, revision=None):
        if self.app_credentials_provider is None:
            return None
        latest = self._app_credential_snapshot()
        if (latest.get('cookie') != cookie or latest.get('phone') != phone
                or latest['profile'] != profile
                or revision is not None and latest.get('credential_revision') != revision):
            raise ActivityError('共享账号凭据已更新，请使用最新凭据重连', 'APP_CREDENTIALS_CHANGED', 409, retryable=True)
        return latest.get('credential_revision')

    def connect_app(self, cookie, phone, profile=None):
        if self.credential_cache is None:
            return self._connect_app(cookie,phone,profile)
        snapshot = self._app_credential_snapshot() if self.app_credentials_provider else {'phone':phone,'cookie':cookie,'profile':profile or {}}
        if snapshot['phone'] != phone:
            raise ActivityError('活动账号与共享账号不一致。','ACCOUNT_MISMATCH',409)
        candidate = requests.Session(impersonate='chrome')
        candidate.headers.update({'User-Agent':APP_UA,'Origin':'https://wx.10086.cn'})
        try:
            data = self.credential_cache.connect('wed-recharge',snapshot,candidate,
                lambda:{'user':self._connect_app(cookie,phone,profile,candidate=candidate,raw=True),'checked_at':time.time()})
            if self.http is not candidate:
                self.http.close()
                self.http = candidate
            self.mode = 'app'
            self.app_material = {'phone':phone,'cookie':cookie,'profile':dict(profile or {})}
            self.app_credential_revision = snapshot.get('credential_revision')
            self.cached_user = data['user']
            self.user_checked_at = time.monotonic()-max(0,time.time()-data['checked_at'])
            return self.public_identity(self.cached_user)
        except Exception:
            candidate.close()
            raise

    def _connect_app(self, cookie, phone, profile=None, *, candidate=None, raw=False):
        """Run qwhdsso's APP token flow using a real APP login Cookie.

        leadeon.getUserInfo().token is LoginDataBridge.getCookie(), confirmed
        against the supplied APK. Never reuse the micro-portal web login SSO.
        """
        if not isinstance(cookie, str) or not re.search(r'(?i)(?:^|;)\s*JSESSIONID=', cookie):
            raise ActivityError('缺少有效 APP 登录 Cookie，请重新短信登录', 'APP_LOGIN_REQUIRED', 401)
        if not isinstance(phone, str) or not re.fullmatch(r'1[3-9][0-9]{9}', phone):
            raise ActivityError('APP 登录手机号无效', 'APP_LOGIN_REQUIRED', 401)
        profile = profile or {}
        input_revision = self._check_current_credentials(cookie, phone, profile)
        candidate = candidate or requests.Session(impersonate='chrome')
        candidate.headers.update({'User-Agent': APP_UA, 'Origin': 'https://wx.10086.cn'})
        try:
            page = self._follow(candidate, ENTRY)
            match = re.search(r"var\s+loginPath\s*=\s*['\"]([^'\"]+)['\"]", page.text)
            if not match or not match[1].startswith('/appTokenLogin?sid='):
                raise ActivityError('活动未返回 APP 登录入口，请稍后重试', 'APP_SSO_ENTRY')
            login_url = self._official_url('/qwhdsso' + match[1])
            result = candidate.post(login_url, json={
                'jwtToken': None, 'token': cookie,
                'provinceCode': str(profile.get('provinceCode', '')),
                'cityCode': str(profile.get('cityCode', '')),
                'userCheckId': format(int(phone), 'x'),
                'carrierOperator': str(profile.get('carrierOperator') or '002'),
                'appVersionCode': '12.5.6', 'took': 0,
            }, headers={'Referer': page.url, 'Content-Type': 'application/json;charset=UTF-8'},
                timeout=(10, 25), allow_redirects=False)
            if result.status_code != 200:
                raise ActivityError(f'APP 活动认证 HTTP {result.status_code}', 'APP_SSO_HTTP', 502, retryable=True)
            data = result.json()
            if not isinstance(data, dict):
                raise ActivityError('活动认证响应格式无效，请稍后重试', 'APP_SSO_FORMAT', 502, retryable=True)
            if data.get('code') != 'SUCCESS':
                # A business SSO failure is not evidence that the APP's main
                # credentials expired. Preserve them for the authoritative
                # APP refresh/validation flow instead of demanding a new SMS.
                raise ActivityError('活动认证暂未完成，请稍后重试', 'APP_SSO_FAILED', 502, retryable=True)
            payload = data.get('data')
            if not isinstance(payload, dict):
                raise ActivityError('活动认证响应格式无效，请稍后重试', 'APP_SSO_FORMAT', 502, retryable=True)
            destination = payload.get('url')
            if not isinstance(destination, str):
                raise ActivityError('APP 活动认证缺少返回地址', 'APP_SSO_FORMAT', 502, retryable=True)
            self._follow(candidate, self._official_url(destination))
            response = candidate.get(BASE + '_pub/rec_login', headers={'login-check': '1', 'Referer': ENTRY},
                                     timeout=(10, 20), allow_redirects=False)
            if response.status_code != 200:
                raise ActivityError('无法验证活动 APP 会话', 'APP_SSO_HTTP', 502, retryable=True)
            identity = response.json()
            if not isinstance(identity, dict):
                raise ActivityError('活动身份响应格式无效，请稍后重试', 'APP_SSO_FORMAT', 502, retryable=True)
            if str(identity.get('code')) != '0':
                raise ActivityError('活动会话暂未建立，请稍后重试', 'APP_SSO_FAILED', 502, retryable=True)
            self._check_current_credentials(cookie, phone, profile, input_revision)
        except requests.RequestsError as error:
            candidate.close()
            raise ActivityError('连接 APP 活动认证服务失败，请稍后重试', 'UPSTREAM_NETWORK', 502) from error
        except (ValueError, TypeError, KeyError) as error:
            candidate.close()
            raise ActivityError('APP 活动认证响应格式无效', 'APP_SSO_FORMAT', 502, retryable=True) from error
        except Exception:
            candidate.close()
            raise
        try:
            if self.bridge.process and self.bridge.process.poll() is None:
                self.bridge.rpc('logout')
            # Browser cleanup can block. Recheck after that boundary before
            # publishing the candidate as this account's active session.
            self._check_current_credentials(cookie, phone, profile, input_revision)
        except Exception:
            candidate.close()
            raise
        self.http.close()
        self.http, self.mode = candidate, 'app'
        self.app_material = {'cookie': cookie, 'phone': phone, 'profile': dict(profile)}
        self.app_credential_revision = input_revision
        self.const_id, self.uncertain = '', False
        self.cached_user = identity.get('data') or {}
        self.user_checked_at = time.monotonic()
        return self.cached_user if raw else self.public_identity(self.cached_user)

    def open_login(self, url=None):
        result = self.bridge.rpc('open', url=validate_entry(url), visible=True, login=True)
        self.mode = 'browser'
        self.const_id = ''
        self.cached_user = None
        return result

    def ensure_browser(self):
        if not self.bridge.rpc('info').get('open'):
            if self.mode == 'app':
                cookies = [{'name': c.name, 'value': c.value, 'domain': c.domain or 'wx.10086.cn',
                            'path': c.path or '/', 'secure': c.secure} for c in self.http.cookies.jar]
                self.bridge.rpc('app_session', cookies=cookies, user_agent=APP_UA)
            else:
                self.bridge.rpc('open', visible=False, login=False)
            self.const_id = ''

    def server_now(self):
        if self.clock_anchor:
            clock, sampled = self.clock_anchor
            return clock + timedelta(seconds=time.monotonic() - sampled)
        return datetime.now(SHANGHAI)

    def import_session(self, cookie, const_id='', url=None):
        if not isinstance(cookie, str) or not cookie.strip() or len(cookie) > 32768 or '\r' in cookie or '\n' in cookie:
            raise ActivityError('请粘贴有效 Cookie 请求头，不要包含换行', 'INVALID_COOKIE')
        if not isinstance(const_id, str) or len(const_id) > 4096:
            raise ActivityError('设备标识格式无效', 'INVALID_CONST_ID')
        candidate = requests.Session(impersonate='chrome')
        count = 0
        for part in cookie.split(';'):
            key, separator, value = part.strip().partition('=')
            if separator and re.fullmatch(r'[!#$%&\x27*+.^_`|~0-9A-Za-z-]+', key):
                candidate.cookies.set(key, value, domain='wx.10086.cn', path='/')
                count += 1
        if not count:
            raise ActivityError('未识别到 Cookie 键值对', 'INVALID_COOKIE')
        if url:
            try:
                candidate.get(validate_entry(url), timeout=20)
            except requests.RequestsError:
                raise ActivityError('活动链接初始化失败，请检查网络或链接', 'UPSTREAM_NETWORK', 502)
        old_http, old_mode, old_id = self.http, self.mode, self.const_id
        old_user, old_checked = self.cached_user, self.user_checked_at
        self.http, self.mode, self.const_id = candidate, 'cookie', const_id.strip()
        try:
            self.require_login()
        except Exception:
            self.http, self.mode, self.const_id = old_http, old_mode, old_id
            self.cached_user, self.user_checked_at = old_user, old_checked
            candidate.close()
            raise
        old_http.close()
        self.uncertain = False

    def call(self, endpoint, body=None):
        if endpoint not in ENDPOINTS:
            raise ActivityError('不支持的接口', 'INVALID_ENDPOINT')
        if self.mode == 'browser':
            self.ensure_browser()
            return self.bridge.rpc('api', endpoint=endpoint, body=body or {})
        if self.mode not in {'cookie', 'app'}:
            raise ActivityError('请先登录中国移动', 'LOGIN_REQUIRED', 401)
        headers = {'Content-Type': 'application/json;charset=UTF-8', 'login-check': '1',
                   'Referer': 'https://wx.10086.cn/hlwyxhdhub/act-wedrecharge/index.html',
                   'Origin': 'https://wx.10086.cn'}
        try:
            if endpoint == '_pub/rec_login':
                response = self.http.get(BASE + endpoint, headers=headers, timeout=15, allow_redirects=False)
            else:
                response = self.http.post(BASE + endpoint, headers=headers, json=body or {}, timeout=15, allow_redirects=False)
            if response.status_code != 200:
                if response.status_code == 401:
                    raise ActivityError('活动会话需要恢复', 'GOTO_LOGIN', 401)
                if response.status_code in {403, 429}:
                    raise ActivityError(f'官方接口 HTTP {response.status_code}，该账号任务已暂停',
                                        'HTTP_ACCESS_LIMIT', response.status_code, retryable=False)
                raise ActivityError(f'官方接口 HTTP {response.status_code}', 'UPSTREAM_HTTP', 502)
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError
            return result
        except requests.RequestsError:
            raise ActivityError('连接官方接口失败，请检查网络；请求不会自动重试', 'UPSTREAM_NETWORK', 502)
        except ValueError:
            raise ActivityError('官方接口未返回有效 JSON', 'UPSTREAM_FORMAT', 502)

    def renew_app_session(self):
        uncertain = self.uncertain
        try:
            latest = self._app_credential_snapshot()
            material = {'cookie':latest.get('cookie'), 'phone':latest.get('phone'), 'profile':dict(latest.get('profile') or {})}
            result = self.connect_app(**material)
            self.app_credential_revision = latest.get('credential_revision')
            return result
        finally:
            # Reconnecting authentication does not settle a previous claim.
            self.uncertain = self.uncertain or uncertain

    def read(self, endpoint, body=None):
        if endpoint == 'drawPrize':
            raise ActivityError('领取请求不能自动重放', 'INVALID_ENDPOINT')
        renewed = False
        if self.app_credentials_provider is not None:
            latest = self._app_credential_snapshot()
            if self.app_material and self.app_material.get('phone') != latest.get('phone'):
                self.renew_app_session()
                renewed = True
        try:
            result = self.call(endpoint) if body is None else self.call(endpoint, body)
            self.check_result(result)
            return result
        except ActivityError as error:
            if renewed or error.code not in {'GOTO_LOGIN', 'LOGIN_REQUIRED'} or not self.app_material and self.app_credentials_provider is None:
                raise
            if self.credential_cache is not None and getattr(self.http,'_credential_binding',None):
                self.credential_cache.invalidate(self.http)
        self.renew_app_session()
        # A single recovery is allowed for read-only requests. SMS, login and
        # claim submissions are never retried here.
        result = self.call(endpoint) if body is None else self.call(endpoint, body)
        self.check_result(result)
        return result

    def require_login(self):
        result = self.read('_pub/rec_login')
        self.cached_user = result.get('data') or {}
        self.user_checked_at = time.monotonic()
        return self.cached_user

    @staticmethod
    def check_result(result):
        code = str(result.get('code', 'UNKNOWN'))
        if code != '0':
            message = str(result.get('msg') or '官方接口未成功')
            raise ActivityError(mask_phone(message), code, 401 if code == 'GOTO_LOGIN' else 400)

    def identity(self):
        if self.mode == 'none' and self.app_credentials_provider is None:
            return {'authenticated': False, 'mode': 'none', 'message': '先登录，再查询本场资格'}
        try:
            user = self.require_login()
        except ActivityError as error:
            if error.code in {'GOTO_LOGIN', 'LOGIN_REQUIRED'}:
                return {'authenticated': False, 'mode': self.mode, 'message': str(error)}
            raise
        return self.public_identity(user)

    def public_identity(self, user):
        return {'authenticated': True, 'mode': self.mode,
                'phone': mask_phone(user.get('phone') or user.get('mobile')),
                'province_code': str(user.get('provinceCode', '')),
                'coupons': [{k: str(p.get(k, '')) for k in ('title', 'name', 'subTitle')}
                            for p in (user.get('Seckillnfos') or []) if isinstance(p, dict)]}

    def refresh(self, *, automatic=False):
        user = (self.cached_user if automatic and self.cached_user is not None
                and time.monotonic() - self.user_checked_at < 60 else self.require_login())
        draw = self.read('queryDrawPrizeInfo')
        data = draw.get('data') or {}
        official_time = parse_server_time(data.get('currentTime'))
        if official_time:
            self.clock_anchor = (official_time, time.monotonic())
        state = availability(user, data)
        state.update(authenticated=True, mode=self.mode,
                     phone=mask_phone(user.get('phone') or user.get('mobile')),
                     prize=public_prize(data.get('getPrize')),
                     coupons=[{k: str(p.get(k, '')) for k in ('title', 'name', 'subTitle')}
                              for p in (user.get('Seckillnfos') or []) if isinstance(p, dict)])
        if self.mode in {'browser', 'app'} and not self.const_id:
            self.ensure_browser()
            self.const_id = self.bridge.rpc('risk').get('const_id', '')
        state['device_ready'] = bool(self.const_id)
        # An explicit refresh can resolve a previous uncertain submission after a short settling interval.
        if self.uncertain and (automatic and (state['rd_status'] in {0, 1, 2, 3, 4, 5} or state['prize'])
                               or not automatic and time.monotonic() - self.last_claim >= 10):
            self.uncertain = False
        state['uncertain'] = self.uncertain
        if self.uncertain:
            state['can_claim'] = False
            state['reason'] = '上次提交结果待确认，10 秒后刷新本场状态与我的奖品'
        elif state['can_claim'] and not self.const_id:
            state['can_claim'] = False
            state['reason'] = '等待官方设备标识；请保持官方活动页打开后刷新'
        return state

    def prizes(self):
        self.require_login()
        result = self.read('getPrizeList')
        return [public_prize(p) for p in (result.get('data') or {}).get('prizeList', []) if isinstance(p, dict)]

    def claim(self, *, automatic=False):
        if not self.claim_lock.acquire(blocking=False):
            raise ActivityError('已有领取请求正在处理', 'CLAIM_BUSY', 409)
        try:
            if not automatic and time.monotonic() - self.last_claim < 3:
                raise ActivityError('请稍后刷新结果再操作', 'CLAIM_COOLDOWN', 429)
            if self.uncertain and not automatic:
                raise ActivityError('上次请求结果待确认，请先刷新状态和奖品', 'CLAIM_UNCERTAIN', 409)
            state = self.refresh(automatic=automatic)
            if automatic and (state['prize'] or state['rd_status'] in {3, 4, 5}):
                return {'success': True, 'code': '0', 'message': '官方确认本场已有领取结果',
                        'already_received': True, 'prizes': [state['prize']] if state['prize'] else []}
            if automatic and state['rd_status'] in {0, 2}:
                raise ActivityError('官方确认本场已参与，停止后续领取', 'ROUND_COMPLETED', 409, retryable=False)
            if not state['can_claim']:
                retryable = bool(state['rd_status'] in {1, 7} or state['server_time'] is None)
                raise ActivityError(state['reason'], 'NOT_AVAILABLE', 409, retryable=retryable)
            self.last_claim = time.monotonic()
            self.uncertain = True
            self.total_submissions += 1
            # One POST per round. Automated rounds query the official result before resubmitting.
            result = self.call('drawPrize', {'constId': self.const_id})
            self.uncertain = False
            self.check_result(result)
            data = result.get('data') or {}
            prizes = data.get('actPrizes') or []
            return {'success': True, 'code': '0', 'message': mask_phone(result.get('msg') or '领取请求成功'),
                    'prizes': [public_prize(p) for p in prizes if isinstance(p, dict)]}
        finally:
            self.claim_lock.release()

    def logout(self):
        if self.credential_cache is not None and getattr(self.http,'_credential_binding',None):
            _,snapshot,_,_,_ = self.http._credential_binding
            self.credential_cache.forget(self.credential_cache.account(snapshot),scope='wed-recharge')
        if self.bridge.process and self.bridge.process.poll() is None:
            self.bridge.rpc('logout')
        self.http.close()
        self.http = requests.Session(impersonate='chrome')
        self.mode, self.const_id, self.uncertain = 'none', '', False
        self.cached_user, self.clock_anchor = None, None
        self.app_material = None
        self.app_credential_revision = None
        self.app_credentials_provider = None
        self.app_login = AppLoginSession()
