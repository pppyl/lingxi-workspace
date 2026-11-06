"""消费 AI 豆：查询当前移动登录的待领数量，凭一次性资格领取。"""

from contextlib import contextmanager
from decimal import Decimal
import random
import re
import secrets
from threading import Lock
import time
from urllib.parse import urlencode

from http_transport import requests, RequestException
from mobile_rights_gateway import GatewayError, page_signature, request_params
from mobile_rights_auth import cookie_header, login_required


BASE_URL = 'https://wap.gz.10086.cn/waps/'
PAGE_URL = BASE_URL + 'static/activity/pointClaim/index.html'
QUERY_PATH = '/waps/pointClaim/initQueryPointClaim'
CLAIM_PATH = '/waps/pointClaim/conmitPointClaim'
QUERY_LIFETIME = 300


class PointClaimError(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.code = code


class PointClaim:
    def __init__(self, ss, *, is_current=None):
        if not isinstance(ss, requests.Session):
            raise ValueError('请使用当前移动登录 Session')
        self.ss = ss
        self._is_current = is_current
        self._lock = Lock()
        self._snapshot = None

    def _ensure_current(self):
        if self._is_current is not None and not self._is_current():
            self._snapshot = None
            raise PointClaimError('登录信息已失效，请重新登录', 401)

    @contextmanager
    def _operation(self):
        if not self._lock.acquire(blocking=False):
            raise PointClaimError('AI豆请求正在处理，请勿重复操作', 409)
        try:
            self._ensure_current()
            yield
        finally:
            self._lock.release()

    @staticmethod
    def _amount(value):
        if type(value) not in (str, int, float):
            raise PointClaimError('AI豆数量无法识别，请重新查询', 502)
        value = str(value).strip()
        if len(value) > 64 or not re.fullmatch(r'[0-9]+(?:\.[0-9]+)?', value):
            raise PointClaimError('AI豆数量无法识别，请重新查询', 502)
        return value

    @staticmethod
    def _safe_access_rejected(data):
        message = data.get('X_RESULTINFO')
        return (str(data.get('X_RESULTCODE')) == '-1'
                and isinstance(message, str)
                and message.strip().rstrip('!！') == '尊敬的用户，请使用安全方式进行访问噢'
                and not any(key in data for key in ('RetVal', 'resultCode', 'AbleDrawScore', 'TotalBalance')))

    def _request(self, path, params):
        self._ensure_current()
        nonce = f'{random.random():.16f}'
        cookies = list(self.ss.cookies.jar)
        coordinates = {'longitude': '', 'latitude': ''}
        for cookie in sorted(list(cookies), key=lambda item: len(item.path or '/')):
            if cookie.domain.lstrip('.') in ('', 'wap.gz.10086.cn'):
                if cookie.name in coordinates:
                    coordinates[cookie.name] = cookie.value or ''
        business = {**params, 'pageName': 'index.html', 'channel': 'E004', **coordinates,
                    'ORDER_URL': PAGE_URL, 'ajax_code': page_signature(PAGE_URL),
                    'ajaxSubmitType': 'post', 'ajax_randomcode': nonce}
        query_prefix = 'undefined&' if path == QUERY_PATH else ''
        try:
            encrypted = request_params(path, business, query_prefix=query_prefix)
        except GatewayError:
            raise PointClaimError('AI豆加密配置不可用，请检查网关配置', 503) from None
        url = 'https://wap.gz.10086.cn' + path
        base_headers = {
            'Accept': 'application/json, text/javascript, */*; q=0.01',
            'X-Requested-With': 'XMLHttpRequest', 'Referer': PAGE_URL,
            'User-Agent': self.ss.headers.get('User-Agent') or 'Mozilla/5.0',
        }
        for attempt in range(2):
            self._ensure_current()
            headers = {**base_headers, 'Cookie':cookie_header(self.ss,nonce,url)}
            payload = encrypted if attempt == 0 else query_prefix + urlencode(business)
            target=url+'?'+payload if isinstance(payload,str) else url
            self._ensure_current()
            try:
                response = self.ss.get(target, params=None if isinstance(payload,str) else payload, headers=headers,
                                       timeout=(10, 30), allow_redirects=False)
            except RequestException:
                raise PointClaimError('AI豆服务连接失败，请重新查询确认', 503) from None
            if response.status_code == 401 or 300 <= response.status_code < 400:
                raise PointClaimError('登录信息已失效，请重新登录', 401)
            if not response.ok or 300 <= response.status_code < 400:
                raise PointClaimError('AI豆服务暂时不可用，请重新查询确认', 503)
            try:
                data = response.json()
            except ValueError:
                raise PointClaimError('AI豆返回结果无法识别，请重新查询确认', 502) from None
            if not isinstance(data, dict):
                raise PointClaimError('AI豆返回结果无法识别，请重新查询确认', 502)
            if login_required(data):
                raise PointClaimError('登录信息已失效，请重新登录', 401)
            self._ensure_current()
            # Only a definitive gateway refusal permits one original-query fallback.
            if attempt == 0 and self._safe_access_rejected(data):
                continue
            break
        if str(data.get('X_RESULTCODE')) != '0':
            message = data.get('X_RESULTINFO')
            raise PointClaimError(message if isinstance(message, str) and message else 'AI豆请求未完成，请重新查询')
        self._ensure_current()
        return data

    def get_info(self):
        with self._operation():
            self._snapshot = None
            data = self._request(QUERY_PATH, {})
            pending = self._amount(data.get('AbleDrawScore'))
            total = self._amount(data.get('TotalBalance'))
            can_claim = Decimal(pending) > 0
            claim_id = secrets.token_urlsafe(24) if can_claim else None
            if can_claim:
                self._snapshot = {'claim_id': claim_id, 'amount': pending,
                                  'expires': time.monotonic() + QUERY_LIFETIME}
            return {'pending_amount': pending, 'total_balance': total,
                    'can_claim': can_claim, 'claim_id': claim_id}

    def claim(self, claim_id):
        with self._operation():
            snapshot = self._snapshot
            if (not isinstance(claim_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', claim_id) or snapshot is None
                    or not secrets.compare_digest(claim_id, snapshot['claim_id'])
                    or time.monotonic() >= snapshot['expires']):
                raise PointClaimError('请先重新查询本账号可领取的AI豆', 409)
            # Consume before any claim attempt, including ambiguous failures.
            self._snapshot = None
            data = self._request(CLAIM_PATH, {'AbleDrawScore': snapshot['amount']})
            if data.get('RetVal') != 'Y':
                raise PointClaimError('AI豆领取未成功，请重新查询确认')
            return {'message': 'AI豆领取成功', 'claimed_amount': snapshot['amount']}
