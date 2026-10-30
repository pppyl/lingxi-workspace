"""Official H5 continuation of an existing, unpaid recharge order.

Only userAuth, orderListQuery, orderDetailQuery and orderGoPay are supported.
APP cookies and the H5 bearer token remain in this request's memory.
"""
import base64
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
import secrets
import time
from types import SimpleNamespace
from urllib.parse import urlsplit
import uuid

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from http_transport import requests

from coupon_client import CouponError, SHANGHAI

ORIGIN = 'https://h.app.coc.10086.cn'
ENTRY = ORIGIN+'/cmcc-app/app-pages/orderList.html'
AUTH_PATH = '/website/nrapigate/nrauth/auth/encryt/userAuth'
ORDER_BASE = '/website/nrapigate/nrmyorder/new/SHS/nyOrder/'
KEY = b'03054BD8937F62FA8FE72F00012B9658'
IV = b'82787D91132E6AE5'


class CouponCashierError(CouponError):
    pass


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def _encrypt(text, *, url_safe=True):
    padder = padding.PKCS7(128).padder()
    plain = padder.update(text.encode())+padder.finalize()
    cipher = Cipher(algorithms.AES(KEY), modes.CBC(IV)).encryptor()
    encoded = base64.b64encode(cipher.update(plain)+cipher.finalize()).decode()
    return encoded.replace('+', '-').replace('/', '_') if url_safe else encoded


def _decrypt(text):
    encoded = re.sub(r'\s', '', text).replace('-', '+').replace('_', '/')
    encoded += '='*((-len(encoded)) % 4)
    cipher = Cipher(algorithms.AES(KEY), modes.CBC(IV)).decryptor()
    plain = cipher.update(base64.b64decode(encoded))+cipher.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return (unpadder.update(plain)+unpadder.finalize()).decode()


def _amount(value):
    try:
        result = Decimal(str(value))
    except InvalidOperation as error:
        raise CouponCashierError('订单金额无效，本次没有进入收银台。', 'CASHIER_AMOUNT_INVALID', 409) from error
    if not result.is_finite() or result <= 0:
        raise CouponCashierError('订单金额无效，本次没有进入收银台。', 'CASHIER_AMOUNT_INVALID', 409)
    return result


class CouponCashierH5:
    def __init__(self, credentials_provider, *, session_factory=None):
        self.credentials = credentials_provider
        self.session_factory = session_factory or (lambda: requests.Session(impersonate='chrome'))

    @contextmanager
    def _session(self):
        snapshot = self.credentials()
        if (not isinstance(snapshot, dict) or not re.fullmatch(r'1[3-9]\d{9}', str(snapshot.get('phone', '')))
                or not isinstance(snapshot.get('cookie'), str) or 'JSESSIONID=' not in snapshot['cookie']):
            raise CouponCashierError('请先恢复当前账号的 APP 登录。', 'APP_LOGIN_REQUIRED', 401)
        def current():
            latest = self.credentials()
            if (latest is None or latest.get('phone') != snapshot['phone']
                    or latest.get('cookie') != snapshot['cookie']
                    or latest.get('credential_revision') != snapshot.get('credential_revision')):
                raise CouponCashierError('账号凭据已变化，请重新获取订单信息。', 'APP_CREDENTIALS_CHANGED', 409)
        client = self.session_factory()
        client.headers.update({'Origin': ORIGIN, 'Referer': ENTRY})
        flow = SimpleNamespace(client=client, snapshot=snapshot, current=current,
                               xk=str(uuid.uuid4()), bearer='')
        try:
            yield flow
        finally:
            client.close()

    @staticmethod
    def _request(flow, path, body, *, auth=False):
        flow.current()
        stamp = str(int(time.time()*1000))
        nonce = ''.join(secrets.choice('0123456789') for _ in range(8))
        seed = flow.xk+'_'+path+'_'+stamp+'_'+nonce
        session_id = flow.client.cookies.get('JSESSIONID') or 'null'
        sign_text = _encrypt(seed)+'_'+stamp+'_'+nonce+'_'+session_id
        headers = {'Content-Type': 'application/json; charset=utf-8', 'Accept': 'application/json, text/plain, */*',
                   'x-token': _encrypt(seed, url_safe=False), 'x-time': stamp, 'x-nonce': nonce,
                   'x-sign': hashlib.md5(sign_text.encode()).hexdigest(),
                   'TRACE': flow.xk+'_'+path+'_'+stamp+'_'+''.join(
                       secrets.choice('0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ') for _ in range(20)),
                   'pageType': 'xds38', 'X-Group-Env': '', 'x-app-version': '1.0.2'}
        if auth:
            # This is native getUserInfo.token: the complete APP cookie, not
            # the unrelated Lingxi token stored under snapshot['token'].
            data = {'appToken': flow.snapshot['cookie']}
            headers.update(channelid='CHINA_APP', **{'x-appToken': flow.snapshot['cookie']})
        else:
            data = {**body, 'xk': flow.xk, 'xat1n': flow.snapshot['cookie']}
            headers['Authorization'] = flow.bearer
        try:
            response = flow.client.post(ORIGIN+path, data=_encrypt(_json(data)), headers=headers,
                                        timeout=(10, 25), allow_redirects=False)
        except requests.RequestsError as error:
            raise CouponCashierError('连接官方订单服务失败，请稍后重新查询；没有付款。', 'CASHIER_NETWORK') from error
        if response.status_code != 200:
            raise CouponCashierError('官方订单服务暂未返回有效结果。', 'CASHIER_HTTP')
        try:
            outer = response.json()
            result = json.loads(_decrypt(outer['body']))
            if not isinstance(result, dict):
                raise ValueError('invalid result')
        except (ValueError, TypeError, KeyError) as error:
            raise CouponCashierError('官方订单响应无法解析，本次没有继续付款。', 'CASHIER_FORMAT') from error
        flow.current()
        if str(result.get('returnCode')) != '0':
            code = str(result.get('returnCode', 'unknown'))
            code = code if re.fullmatch(r'[A-Za-z0-9_-]{1,32}', code) else 'unknown'
            error = CouponCashierError('官方未完成订单操作（'+code+'），本次没有付款。', 'CASHIER_REJECTED', 409)
            error.safe = {'returnCode': code, 'fields': sorted(result)}
            raise error
        data = result.get('data')
        if not isinstance(data, dict):
            raise CouponCashierError('官方未返回有效订单数据。', 'CASHIER_FORMAT')
        return data

    def _details(self, flow, order_id, expected_amount):
        auth = self._request(flow, AUTH_PATH, {}, auth=True)
        user = auth.get('userInfo') or {}
        if user.get('uid') != flow.snapshot['phone']:
            raise CouponCashierError('官方订单认证的账号不一致，本次已停止。', 'ACCOUNT_MISMATCH', 409)
        if not isinstance(user.get('token'), str) or not user['token']:
            raise CouponCashierError('官方未返回订单会话凭据，请重新验证账号。', 'CASHIER_AUTH_FAILED', 401)
        flow.bearer = user['token']
        phone = flow.snapshot['phone']
        province = str((flow.snapshot.get('profile') or {}).get('provinceCode') or '')
        today = datetime.now(SHANGHAI).date()
        start = date(today.year-1, today.month, 1)+timedelta(days=today.day-1)
        orders = self._request(flow, ORDER_BASE+'orderListQuery', {
            'customerId': phone, 'cellNum': phone, 'loginType': '1',
            'queryTypes': [{'orderType': f'{index:03d}'} for index in range(1, 7)],
            'startTime': start.strftime('%Y%m%d'), 'endTime': today.strftime('%Y%m%d'),
            'provcode': province, 'systemId': '1001',
            'filterType': '', 'page': {'currentPage': 1, 'pageSize': 20}, 'filterPayType': '',
            'tab': '1', 'subBizType': ['101'], 'yzdOrderFlag': True})
        record = next((row for row in orders.get('busiDataResp', []) if str(row.get('orderId')) == order_id), None)
        if record is None:
            raise CouponCashierError('当前账号最近订单中未找到此订单，请到 APP 查看。', 'CASHIER_ORDER_NOT_FOUND', 404)
        first = (record.get('orderItems') or [{}])[0]
        metadata = {'orderId': order_id, 'orderItemId': first.get('orderItemId') or '',
                    'customerId': record.get('customerId') or '', 'orderType': record.get('orderType') or '',
                    'chargeOrderFlag': record.get('chargeOrderFlag') or '0'}
        if 'loginType' in record:
            metadata['loginType'] = record['loginType']
        detail = self._request(flow, ORDER_BASE+'orderDetailQuery',
            {'cellNum': phone, 'provcode': province, 'systemId': '1001', **metadata})
        if str(detail.get('orderId')) != order_id or detail.get('rechargeNum') != phone:
            raise CouponCashierError('订单号或充值账号不一致，本次已停止。', 'CASHIER_ORDER_MISMATCH', 409)
        if str(detail.get('orderType')) != '004':
            raise CouponCashierError('此订单不是话费充值订单。', 'CASHIER_ORDER_NOT_PAYABLE', 409)
        status = str(detail.get('status') or '')
        # For bonus coupons charge/chargeFee include the gifted airtime.
        # chargeChoseFee is the selected principal (the page's 充值面额).
        amount = _amount(detail.get('chargeChoseFee'))
        if status == '17' and expected_amount is not None and amount != expected_amount:
            raise CouponCashierError('官方订单应付金额与预约金额不一致，本次没有进入收银台。', 'CASHIER_AMOUNT_MISMATCH', 409)
        return detail, {'order_id': order_id, 'amount': format(amount, '.2f'),
                        'account_masked': phone[:3]+'****'+phone[-4:], 'status': status,
                        'status_description': str(detail.get('statusDesc') or '官方状态 '+status),
                        'payable': status == '17', 'closed': status in ('24', '25', '77', '3')}

    @staticmethod
    def _selection(order_id, expected_amount):
        if not isinstance(order_id, str) or not order_id or len(order_id) > 128:
            raise CouponCashierError('请选择本机已生成的订单。', 'INVALID_ORDER', 400)
        return _amount(expected_amount) if expected_amount is not None else None

    def inspect_order(self, order_id, expected_amount=None):
        expected = self._selection(order_id, expected_amount)
        with self._session() as flow:
            _, summary = self._details(flow, order_id, expected)
            return summary

    def continue_payment(self, order_id, expected_amount=None):
        expected = self._selection(order_id, expected_amount)
        with self._session() as flow:
            detail, summary = self._details(flow, order_id, expected)
            if not summary['payable']:
                error = CouponCashierError('订单当前状态：'+summary['status_description']+'，不能继续支付。',
                                           'CASHIER_ORDER_NOT_PAYABLE', 409)
                error.order_summary = dict(summary)
                raise error
            phone = flow.snapshot['phone']
            data = self._request(flow, ORDER_BASE+'orderGoPay', {
                'payPhoneNo': phone, 'phoneNo': detail['rechargeNum'], 'channel': '0003',
                'orderId': order_id, 'payWay': 'WAP', 'oprType': '01', 'systemId': '1001',
                'loginChannel': '12', 'provcode': str((flow.snapshot.get('profile') or {}).get('provinceCode') or ''),
                'cellNum': phone})
            url = data.get('payUrl')
            parsed = urlsplit(url) if isinstance(url, str) else None
            host = parsed.hostname if parsed is not None else ''
            if (parsed is None or parsed.scheme != 'https' or not host
                    or parsed.username is not None or parsed.password is not None
                    or not (host == 'cashier.cmpay.com' or any(
                        host == domain or host.endswith('.'+domain) for domain in ('10086.cn', 'alipay.com')))):
                raise CouponCashierError('官方未返回可打开的支付地址，请在 APP 继续。', 'CASHIER_LINK_UNAVAILABLE', 409)
            return {**summary, 'payment_url': url}
