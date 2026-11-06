"""新青年瑞幸兑换：查询、发送活动验证码、确认领取。"""

from collections.abc import Mapping
from contextlib import contextmanager
from datetime import datetime
import math
import random
import re
from threading import Lock
import time
from urllib.parse import quote, urlencode

from http_transport import requests, RequestException
from mobile_rights_gateway import GatewayError, page_signature, request_params
from mobile_rights_auth import cookie_header, login_required


BASE_URL = 'https://wap.gz.10086.cn/waps/'
MEMBER_PAGE = BASE_URL + 'static/activity/apRight/verticalClassVip/index.html'
RIGHT_PAGE = BASE_URL + 'static/activity/apRightPlatform/special_rights_details_01.html'
RIGHT_ID = 'a060a22793564352ba789ece97e4328b'
REFER_CHANNELS = '1000000002110700006'
SMS_INTERVAL = 70


class XQNError(Exception):
    def __init__(self, message, code=400, retry_after=None):
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after


class NewYouthClient:
    def __init__(self, auth):
        # Web 入口直接共享登录 Session，自动使用 WAP_SID 及响应更新的 Cookie。
        if isinstance(auth, requests.Session):
            self.ss = auth
        else:
            self.ss = requests.Session()
            self.ss.headers['User-Agent'] = 'Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Mobile Safari/537.36'
            if isinstance(auth, Mapping):
                self.ss.cookies.update(auth)
            elif isinstance(auth, str) and auth:
                self.ss.cookies.set('WAP_SID', auth, domain='wap.gz.10086.cn', path='/waps')
            else:
                raise ValueError('请传入登录 Session、完整 Cookie 或 WAP_SID')
        self.headers = {
            'Accept': 'application/json, text/javascript, */*; q=0.01',
            'X-Requested-With': 'XMLHttpRequest',
            'User-Agent': self.ss.headers.get('User-Agent') or 'Mozilla/5.0',
        }
        self._lock = Lock()
        self._coupons = {}
        self._selected = None
        self._confirmation_id = None
        self._sms_sent_at = None

    @contextmanager
    def _operation(self):
        if not self._lock.acquire(blocking=False):
            raise XQNError('新青年请求正在处理，请勿重复操作', 409)
        try:
            yield
        finally:
            self._lock.release()

    def _clear_selection(self):
        self._selected = None
        self._confirmation_id = None

    def _right_page(self, v_id):
        return RIGHT_PAGE + '?' + urlencode({
            'rightId': RIGHT_ID, 'rightLinkType': '2', 'needCount': '1',
            'vId': v_id, 'referChannels': REFER_CHANNELS,
        })

    def _coordinates(self):
        coordinates = {'longitude': '', 'latitude': ''}
        # 仅使用当前登录 Session 已有的位置；不复用抓包中其他账号的位置。
        for cookie in sorted(self.ss.cookies.jar, key=lambda item: len(item.path or '/')):
            if cookie.domain.lstrip('.') in ('', 'wap.gz.10086.cn') and cookie.name in coordinates:
                coordinates[cookie.name] = cookie.value or ''
        return coordinates

    def _new_confirmation_id(self):
        now = datetime.now()
        # 与活动页面 getSecCfmId 一致：当前时间、毫秒及六位随机数。
        return now.strftime('%Y%m%d%H%M%S') + str(now.microsecond // 1000).ljust(3, '0') + str(random.randint(100000, 999999))

    def _response_data(self, endpoint, params, referer, nonce):
        target=BASE_URL+endpoint
        if isinstance(params,str):target+='?'+params;params=None
        try:
            response = self.ss.get(
                target, params=params,
                headers={**self.headers, 'Referer': referer, 'Cookie':cookie_header(self.ss,nonce,BASE_URL+endpoint)}, timeout=(10, 30),
                allow_redirects=False,
            )
        except RequestException:
            raise XQNError('新青年服务连接失败，请稍后重试', 503) from None
        if response.status_code == 401 or 300 <= response.status_code < 400:
            self._clear_selection()
            raise XQNError('登录信息已失效，请重新登录', 401)
        if not response.ok:
            raise XQNError('新青年服务暂时不可用，请稍后重试', 503)
        try:
            data = response.json()
        except ValueError:
            raise XQNError('新青年服务返回结果无法识别，请重新查询', 502) from None
        if not isinstance(data, dict):
            raise XQNError('新青年服务返回结果无法识别，请重新查询', 502)
        if login_required(data):
            self._clear_selection()
            raise XQNError('登录信息已失效，请重新登录', 401)
        return data

    @staticmethod
    def _safe_access_rejected(data):
        message = data.get('X_RESULTINFO')
        return (str(data.get('X_RESULTCODE')) == '-1'
                and isinstance(message, str)
                and message.strip().rstrip('!！') == '尊敬的用户，请使用安全方式进行访问噢'
                and 'resultCode' not in data)

    def _request(self, endpoint, params, *, member=False, v_id=None, cfm_id=None):
        random_code = f'{random.random():.16f}'
        referer = MEMBER_PAGE + '?actId=N2KEQ0' if member else self._right_page(v_id)
        page_url, page_query = referer.split('?', 1)
        try:
            business = {**params, 'pageName': page_url.rsplit('/', 1)[-1],
                        'channel': 'E004', **self._coordinates()}
            if not member:
                business.update({'referChannels': REFER_CHANNELS, 'cfmId': cfm_id})
            business.update({
                'ORDER_URL': page_url, 'ORDER_URL_PARAM': quote(page_query, safe=''),
                'ajax_code': page_signature(referer),
                'ajaxSubmitType': 'post', 'ajax_randomcode': random_code,
            })
            encrypted_params = request_params('/waps/' + endpoint, business)
        except GatewayError as error:
            raise XQNError(str(error), 503) from None
        data = self._response_data(endpoint, encrypted_params, referer, random_code)
        if self._safe_access_rejected(data):
            # 仅明确的网关拒绝允许重试一次；沿用同一业务流水，避免重复发送或兑换。
            # 与活动页面原始 AJAX 查询一致，保留开头无值的 undefined 参数。
            data = self._response_data(endpoint, 'undefined&' + urlencode(business), referer, random_code)
        result_code = str(data.get('X_RESULTCODE', ''))
        if result_code != '0':
            if not result_code:
                raise XQNError('新青年服务返回结果无法识别，请重新查询', 502)
            message = data.get('X_RESULTINFO')
            raise XQNError(message if isinstance(message, str) and message else '新青年操作失败，请重试')
        return data

    def getInfo_xqn(self):
        """只查询兑换券；查询不会发送短信，也不会领取。"""
        with self._operation():
            self._clear_selection()
            self._coupons = {}
            data = self._request('apRight/initVerticalClassVip', {}, member=True)
            members = data.get('verticalClassVips')
            if not isinstance(members, list):
                raise XQNError('新青年兑换券数据无法识别，请重新查询', 502)
            coupons = {}
            for member in members:
                if not isinstance(member, dict):
                    raise XQNError('新青年兑换券数据无法识别，请重新查询', 502)
                available = member.get('apRightCoupons', [])
                if not isinstance(available, list):
                    raise XQNError('新青年兑换券数据无法识别，请重新查询', 502)
                v_id = member.get('vId') or member.get('id')
                for coupon in available:
                    coupon_id = coupon.get('id') if isinstance(coupon, dict) else None
                    if not isinstance(v_id, str) or not v_id or not isinstance(coupon_id, str) or not coupon_id:
                        raise XQNError('新青年兑换券数据不完整，请重新查询', 502)
                    coupons[(v_id, coupon_id)] = {
                        'v_id': v_id, 'coupon_id': coupon_id,
                        'product_name': member.get('productName') or '新青年兑换券',
                    }
            self._coupons = coupons
            return list(coupons.values())

    def send_sms(self, v_id, coupon_id):
        with self._operation():
            if not isinstance(v_id, str) or not isinstance(coupon_id, str) or (v_id, coupon_id) not in self._coupons:
                raise XQNError('请先查询并选择当前账户的兑换券', 409)
            if self._sms_sent_at is not None:
                remaining = math.ceil(SMS_INTERVAL - (time.monotonic() - self._sms_sent_at))
                if remaining > 0:
                    raise XQNError(f'请等待 {remaining} 秒后重新获取活动验证码', 429, remaining)
            self._clear_selection()
            confirmation_id = self._new_confirmation_id()
            self._sms_sent_at = time.monotonic()
            self._request('rightSell/getSmsBusiCode', {
                'type': 'ORDER', 'rightId': RIGHT_ID,
            }, v_id=v_id, cfm_id=confirmation_id)
            self._selected = (v_id, coupon_id)
            self._confirmation_id = confirmation_id
            self._sms_sent_at = time.monotonic()
            return {'message': '活动验证码已发送，请查看手机短信', 'retry_after': SMS_INTERVAL}

    def lq_xqn(self, sms_code, v_id=None, coupon_id=None):
        with self._operation():
            if not isinstance(sms_code, str) or not re.fullmatch(r'[0-9]{6}', sms_code):
                raise XQNError('请输入 6 位活动验证码')
            if self._selected is None or self._selected not in self._coupons or self._confirmation_id is None:
                raise XQNError('请先选择兑换券并获取活动验证码', 409)
            if (v_id is not None or coupon_id is not None) and (v_id, coupon_id) != self._selected:
                raise XQNError('兑换券已变更，请为所选兑换券重新获取活动验证码', 409)
            selected = self._selected
            v_id, coupon_id = selected
            try:
                data = self._request('rightSell/coupunRightOrder', {
                    'rightId': RIGHT_ID, 'couponId': coupon_id, 'busSmsCode': sms_code,
                    'type': 'ORDER', 'rightLinkType': '2', 'vId': v_id, 'operType': 'OPER',
                }, v_id=v_id, cfm_id=self._confirmation_id)
            except XQNError as error:
                if error.code >= 500:
                    self._clear_selection()
                    raise XQNError('领取结果未能确认，请重新查询兑换券后再操作', error.code) from None
                raise
            # 外层成功仅表示请求完成，兑换还必须通过内层业务码。
            result = data.get('resultCode')
            if result != '0000':
                message = data.get('resultMsg')
                if result is None:
                    self._clear_selection()
                    raise XQNError('领取结果无法识别，请重新查询兑换券确认', 502)
                raise XQNError(message if isinstance(message, str) and message else '新青年领取失败，请核对活动验证码后重试')
            self._coupons.pop(selected)
            self._clear_selection()
            return '新青年瑞幸领取成功'
