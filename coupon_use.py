"""User-confirmed coupon redemption and unpaid recharge orders.

The activity session, redeemable coupon code and SMS exchange stay in memory.
Submission results and the account reference needed to resume payment are encrypted at rest.
"""
import base64
from contextlib import contextmanager
from copy import deepcopy
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, ROUND_DOWN, ROUND_CEILING, ROUND_FLOOR
import hashlib
import hmac
import json
import logging
import re
import secrets
import socket
from threading import Lock, RLock
import time
from types import SimpleNamespace
from urllib.parse import urlsplit, parse_qs

from cryptography.exceptions import InvalidTag
from http_transport import requests

from coupon_client import (CouponClient, CouponError, ORIGIN, ENTRY, _cbc_encrypt,
                           _item, _payload, _unpack, RECHARGE_CARD_TYPES)
from coupon_alipay import CouponAlipay, CouponAlipayError
from app_login import AppLoginSession


class CouponUseError(CouponError):
    pass


class _UnknownSubmission(CouponUseError):
    pass


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def _money(value):
    try:
        number = Decimal(str(value))
    except InvalidOperation as error:
        raise CouponUseError('官方未提供有效的卡券金额。', 'AMOUNT_MISSING', 409) from error
    if not number.is_finite() or number <= 0:
        raise CouponUseError('官方未提供有效的卡券金额。', 'AMOUNT_MISSING', 409)
    return number


def _decimal_text(number):
    return format(number, 'f').rstrip('0').rstrip('.') if '.' in format(number, 'f') else str(number)


def _choices(rule, amount_rule=None):
    sale_type = str(rule.get('saleType'))
    if sale_type not in ('7', '10', '15'):
        raise CouponUseError('此券优惠规则尚未接入，请在 APP 使用此券。', 'RULE_UNSUPPORTED', 409)
    bands = []
    for text in str(rule.get('saleCode') or '').split(','):
        match = re.fullmatch(r'(\d+(?:\.\d+)?)~(\d+(?:\.\d+)?):(\d+(?:\.\d+)?)', text.strip())
        if not match:
            raise CouponUseError('官方充值规则无法解析，本次没有生成订单。', 'RULE_MISSING', 409)
        band = tuple(Decimal(part) for part in match.groups())
        if band[0] > band[1]:
            raise CouponUseError('官方充值规则区间无效，本次没有生成订单。', 'RULE_MISSING', 409)
        bands.append(band)
    limits = amount_rule or {}
    minimum = _money(limits.get('minAmount') or 10).quantize(Decimal('.01'), rounding=ROUND_DOWN)
    maximum = _money(limits.get('maxAmount') or 1000).quantize(Decimal('.01'), rounding=ROUND_DOWN)
    if minimum > maximum:
        raise CouponUseError('官方充值金额范围无效。', 'RULE_MISSING', 409)
    candidates = {Decimal(value) for value in (20, 30, 50, 100, 200, 500, 1000)}
    candidates.update(max(low, minimum).quantize(Decimal('.01'), rounding=ROUND_CEILING)
                      for low, high, _ in bands if max(low, minimum) <= min(high, maximum))
    rounding = {'1': ROUND_CEILING, '2': ROUND_FLOOR}.get(str(rule.get('discountFormat') or ''), ROUND_HALF_UP)
    choices = []
    for original in sorted(candidates):
        if not minimum <= original <= maximum:
            continue
        eligible = [discount for low, high, discount in bands if low <= original <= high]
        if eligible:
            discount = eligible[-1]
            payment = (original if sale_type == '7' else original-discount).quantize(Decimal('.01'), rounding=rounding)
            credit = (original+discount if sale_type == '7' else original).quantize(Decimal('.01'), rounding=rounding)
            if payment <= 0:
                continue
            paid_number = int(payment) if payment == payment.to_integral_value() else float(payment)
            special = sale_type == '15' or str(rule.get('pcardType') or rule.get('pCardType')) == 'hf0011'
            choices.append({'value': _decimal_text(original), 'amount': paid_number,
                            'payment': format(payment, '.2f'), 'credit': format(credit, '.2f'),
                            'submit_amount': format(original, '.2f') if special else paid_number,
                            'special_reduction': format(original-payment, '.2f') if special else None,
                            'label': f'充值 {_decimal_text(original)} 元 · 实付 {_decimal_text(payment)} 元，到账 {_decimal_text(credit)} 元'})
    if not choices:
        raise CouponUseError('此券当前没有可用的充值档位，请前往 APP 查看。', 'NO_CHOICE', 409)
    best = min(choices, key=lambda choice: (
        -(Decimal(str(choice['credit']))-Decimal(choice['amount']))/Decimal(choice['amount']),
        choice['amount']))
    return [best]


class CouponUseManager:
    def __init__(self, credentials_provider_by_id, storage=None, *, payment_credentials_provider_by_id=None,
                 wallet_entry=ENTRY):
        self.credentials = credentials_provider_by_id
        self.payment_credentials = payment_credentials_provider_by_id or credentials_provider_by_id
        self.storage = storage
        self.wallet_entry = wallet_entry
        self.lock, self.operations = RLock(), {}
        self.payment_challenges = {}
        self.data = (storage.load_coupon_use_data() if storage is not None else None) or {}
        if any(record['state'] == 'submitted' for record in self.data.values()):
            recovered = deepcopy(self.data)
            for record in recovered.values():
                if record['state'] == 'submitted':
                    record.update(state='unknown', result={'state': 'unknown',
                        'message': '上次提交中断，结果待核实；请在 APP 查询，本程序不会重复提交。'})
            self._save(recovered)

    def _save(self, data):
        if self.storage is not None:
            self.storage.save_coupon_use_data(data)
        self.data = data

    @staticmethod
    def _close(operation):
        if operation.context is not None:
            operation.context.__exit__(None, None, None)
            operation.context = None
        operation.verified_sms = None
        operation.risk_token = ''

    def _prune(self):
        with self.lock:
            for key, operation in list(self.operations.items()):
                if time.monotonic() >= operation.expires and operation.lock.acquire(blocking=False):
                    try:
                        self._close(operation)
                        self.operations.pop(key)
                    finally:
                        operation.lock.release()

    def busy_login(self, login_id):
        """Keep a live wallet/SMS session stable until completion or expiry."""
        with self.lock:
            now = time.monotonic()
            return any(operation.login_id == login_id and operation.context is not None
                       and operation.result is None and now < operation.expires
                       for operation in self.operations.values())

    def _check_submission(self, key):
        record = self.data.get(key)
        if record and record['state'] in ('submitted', 'unknown', 'accepted', 'payment_required'):
            raise CouponUseError('此卡券已提交，请到 APP 查看兑换或待支付订单；本程序不会重复提交。',
                                 'ALREADY_SUBMITTED', 409)

    def bind_pending(self, login_id, items):
        """Attach legacy orders only when their original account/coupon digest matches."""
        with self.lock:
            updated = deepcopy(self.data)
            changed = False
            for item in items:
                key = hashlib.sha256(_json([login_id, item['id']]).encode()).hexdigest()
                record = updated.get(key)
                if record and record['state'] == 'payment_required' and not record.get('login_id'):
                    record.update(login_id=login_id, coupon_id=item['id'], coupon_name=item['name'])
                    changed = True
            if changed:
                self._save(updated)

    def pending(self, login_id):
        with self.lock:
            return [dict(order_id=record['order_id'], coupon_name=record.get('coupon_name', ''),
                         coupon_id=record.get('coupon_id', ''), amount=record.get('charge_amount'),
                         created_at=record.get('created_at'))
                    for record in self.data.values()
                    if record.get('login_id') == login_id and record['state'] == 'payment_required']

    def payment(self, login_id, order_id, *, refresh=False, captcha_token=''):
        if (not isinstance(login_id, str) or not isinstance(order_id, str) or not isinstance(refresh, bool)
                or not isinstance(captcha_token, str) or len(captcha_token) > 4096):
            raise CouponUseError('请选择本机已生成的待支付订单。', 'INVALID_ORDER', 400)
        with self.lock:
            record = next((deepcopy(row) for row in self.data.values()
                           if row.get('login_id') == login_id and row.get('order_id') == order_id
                           and row['state'] == 'payment_required'), None)
        if record is None:
            raise CouponUseError('未找到此账号的待支付订单，请刷新卡券列表。', 'ORDER_NOT_FOUND', 404)
        with self.lock:
            challenge = self.payment_challenges.get((login_id, order_id))
        if challenge is not None:
            snapshot = self.payment_credentials(login_id)
            if snapshot is None:
                raise CouponUseError('账号已退出，请重新添加账号。', 'APP_LOGIN_REQUIRED', 401)
            if challenge['quote'].get('credential_revision') != snapshot.get('credential_revision'):
                with self.lock:
                    self.payment_challenges.pop((login_id, order_id), None)
                challenge = None
        with self.lock:
            if captcha_token:
                if challenge is None or time.monotonic() >= challenge['expires']:
                    raise CouponUseError('支付验证已失效，请重新获取新的滑块。', 'PAYMENT_CAPTCHA_EXPIRED', 409)
                self.payment_challenges.pop((login_id, order_id))
            elif challenge is not None and time.monotonic() < challenge['expires']:
                return {'state': 'verification_required', 'verification': 'captcha',
                        'challenge': challenge['config'], 'message': '请完成支付阶段的滑块验证。'}
        saved_payment = record.get('payment')
        if saved_payment and not refresh:
            url = urlsplit(saved_payment.get('payment_url', ''))
            if (url.scheme == 'https' and url.hostname
                    and (url.hostname == 'alipay.com' or url.hostname.endswith('.alipay.com'))):
                return deepcopy(saved_payment)
        client = CouponAlipay(lambda: self.payment_credentials(login_id))
        try:
            # Old records did not save the charge amount. Read the official
            # quote for this bound order rather than infer it from the coupon.
            expected = (_money(record['charge_amount'])*100 if record.get('charge_amount') is not None
                        else _money(challenge['quote']['amount'] if captcha_token
                                    else client.query_order(order_id)['amount']))
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
                route.connect(('pay.shop.10086.cn', 443))
                local_ip = route.getsockname()[0]
            options = {'verify_key': captcha_token, 'verified_quote': challenge['quote']} if captcha_token else {}
            payment = client.get_payment(order_id, expected_amount=str(expected), ip_address=local_ip, **options)
        except OSError as error:
            raise CouponUseError('无法取得本机网络地址，请检查网络后重新获取支付链接。', 'PAYMENT_NETWORK') from error
        except CouponAlipayError as error:
            if error.code == 'ALIPAY_VERIFICATION_REQUIRED':
                with self.lock:
                    self.payment_challenges[(login_id, order_id)] = {
                        'config': error.challenge, 'quote': error.quote, 'expires': time.monotonic()+600}
                return {'state': 'verification_required', 'verification': 'captcha',
                        'challenge': error.challenge, 'message': str(error)}
            if not getattr(error, 'safe', {}).get('closed'):
                raise
            result = {'state': 'closed', 'message': '此订单已超时关闭，请刷新卡券后重新生成订单。'}
            with self.lock:
                updated = deepcopy(self.data)
                for row in updated.values():
                    if row.get('order_id') == order_id and row.get('login_id') == login_id:
                        row.update(state='closed', result=result)
                self._save(updated)
            return result
        if payment.get('payment_type') != 'alipay_web':
            raise CouponUseError('该订单返回的支付宝通道暂不支持直接打开，请在 APP 继续支付。', 'PAYMENT_ROUTE_UNSUPPORTED', 409)
        result = {'state': 'payment_required', 'order_id': order_id,
                'amount': format(expected/100, '.2f'), 'account_masked': payment['account_masked'],
                'payment_type': payment['payment_type'], 'payment_url': payment['payment_url']}
        with self.lock:
            self.payment_challenges.pop((login_id, order_id), None)
            updated = deepcopy(self.data)
            for row in updated.values():
                if row.get('order_id') == order_id and row.get('login_id') == login_id:
                    row.update(payment=result, charge_amount=result['amount'])
            self._save(updated)
        return result

    @contextmanager
    def _operation(self, operation_id):
        if not isinstance(operation_id, str):
            raise CouponUseError('操作不存在，请重新选择卡券。', 'OPERATION_MISSING', 404)
        with self.lock:
            operation = self.operations.get(operation_id)
        if operation is None:
            raise CouponUseError('操作不存在，请重新选择卡券。', 'OPERATION_MISSING', 404)
        with operation.lock:
            if time.monotonic() >= operation.expires:
                self._close(operation)
                raise CouponUseError('操作已超过十分钟，请重新选择卡券。', 'OPERATION_EXPIRED', 409)
            yield operation

    @staticmethod
    def _post(operation, path, body, *, plain=False, gcm=False, submitting=False):
        operation.current()
        client, user = operation.client, operation.user
        client.headers.update({'payPhoneNo': base64.b64encode(_cbc_encrypt(user['number']).encode()).decode()+user['number'][5:7],
                               'provcode': user['province'], 'Content-Type': 'application/json; charset=utf-8',
                               'Accept': 'application/json, text/javascript, */*; q=0.01',
                               'X-Requested-With': 'XMLHttpRequest'})
        encoded = (body if plain else _payload(body, user['msg_id']) if gcm else
                   _payload(body, user['msg_id'] if operation.kind != 'recharge' else '', gcm=False))
        if path.startswith(('/i/v1/pay/pcardsms/', '/i/v1/pay/pcardvalid/', '/i/v1/pay/saveorder/',
                            '/i/v1/pay/ruleinfo/', '/i/v1/cust/pcardAndGiftQry/',
                            '/i/v1/cust/pcardInfoQry/', '/i/v1/cust/commonSms/', '/i/v1/cust/rightsCouponsUse/')):
            # The official F() URL transform is separate from body encryption;
            # its Base64 text is inserted verbatim and keeps the query string.
            phone_path = base64.b64encode(_cbc_encrypt(user['number']).encode()).decode()
            path = path.replace('/'+user['number'], '/'+phone_path, 1)
        try:
            response = client.post(ORIGIN+path, json=encoded, timeout=(10, 25), allow_redirects=False)
        except requests.RequestsError as error:
            if submitting:
                raise _UnknownSubmission('提交连接中断，结果待核实；请在 APP 查询，不会自动重发。', 'RESULT_UNKNOWN') from error
            raise CouponUseError('连接卡券服务失败，请稍后重试。', 'UPSTREAM_NETWORK') from error
        error_class = _UnknownSubmission if submitting else CouponUseError
        if response.status_code != 200:
            raise error_class('官方服务暂未返回有效结果，请稍后到 APP 核实。', 'UPSTREAM_HTTP')
        try:
            value = response.json()
            if not isinstance(value, dict):
                raise ValueError('invalid response')
            if value.get('retCode') is None:
                raise ValueError('missing result code')
            code = str(value['retCode'])
            if code not in ('000000', '0'):
                if code == '500002' and path.startswith('/i/v1/pay/saveorder/'):
                    cookies = {cookie.name: len(cookie.value) for cookie in client.cookies.jar}
                    facts = {
                        'account_tail': user['number'][-4:], 'stage': 'saveorder',
                        'fields': {key: {'type': type(item).__name__,
                                        'length': len(item) if isinstance(item, str) else None}
                                   for key, item in body.items()},
                        'controls': {key: body.get(key) for key in ('channel', 'amount', 'numFlag', 'smsCheck')},
                        'msg_id_length': len(encoded.get('msgId', '')),
                        'risk_cookie_lengths': {key: cookies.get(key, 0) for key in ('stc-rrt-token', '_zw_kvani5r')},
                        'risk_callback_matches_c1': operation.risk_callback_matches_c1,
                        'cookie_names': sorted(cookies),
                        'sms_verified': operation.verified_sms == (operation.serial, body.get('smsCode')),
                        'app_user_agent': 'leadeon' in client.headers.get('User-Agent', '').lower(),
                        'app_session_uuid_format': bool(re.fullmatch(
                            r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', body.get('wtAcId', ''))),
                    }
                    logging.getLogger(__name__).warning('coupon_exchange_500002 %s', _json(facts))
                if code in ('571001', '571101', '550023'):
                    raise _UnknownSubmission('官方正在处理此卡券，请在 APP 核实结果，本程序不会重复提交。', 'RESULT_PENDING')
                messages = {'541008': '卡券状态已变化，请刷新后查看。', '543006': '活动周期内用券次数已达上限。',
                            '543007': '本月用券次数已达上限。', '500003': '卡券会话已失效，请重新校验共享账号。',
                            '510002': '短信验证码已失效，请重新获取。', '510003': '短信验证码错误，请重新输入。',
                            '510004': '验证码错误次数过多，请稍后重新获取。', '510006': '官方已限制短信验证，请在 APP 处理。',
                            '510005': '短信验证码重发次数过多，请稍后再试。', '510007': '操作过于频繁，请稍后再试。',
                            '531015': '下单操作频繁，请稍后再试（531015）。',
                            '531022': '官方检测到充值环境异常（531022），本次未生成支付订单。可在当前窗口通过短信重新登录后重试；若仍失败，请切换网络再试。'}
                message = messages.get(code, '官方拒绝此操作（'+code[:30]+'），本次未完成。')
                if code in ('500002', '500004') and isinstance(value.get('retMsg'), str):
                    detail = value['retMsg']
                    for secret in (user['number'], operation.serial, *[body.get(key) for key in
                            ('pCardNo', 'pcardNo', 'pcardPassword', 'smsCode', 'sms_code', 'serialNo', 'serial_no', 'md5', 'wtAcId')]):
                        if isinstance(secret, str) and len(secret) >= 4:
                            detail = detail.replace(secret, '***')
                    detail = re.sub(r'1[3-9]\d{9}', lambda match: match[0][:3]+'****'+match[0][-4:], detail).strip()[:240]
                    if detail:
                        message += '\n官方说明：'+detail
                raise CouponUseError(message,
                                     code, 401 if code == '500003' else 409)
            if value.get('data') is None:
                return {}
            try:
                return _unpack(value, gcm=gcm)
            except CouponError as error:
                raise error_class('官方响应无法解析，请到 APP 核实结果。', 'UPSTREAM_FORMAT') from error
        except (ValueError, TypeError, KeyError, InvalidTag) as error:
            raise error_class('官方响应无法解析，请到 APP 核实结果。', 'UPSTREAM_FORMAT') from error

    def _row(self, operation):
        user = operation.user
        data = self._post(operation, '/i/v1/cust/pcardAndGiftQry/'+user['number'],
            {'loginNo': user['number'], 'loginType': user['login_type'], 'channelId': '11',
             'provCode': user['province'], 'busiType': '', 'pcardPasswd': '',
             'status': '1,8,9,21', 'sortType': '2'}, gcm=True)
        groups = data.get('busiData')
        if not isinstance(groups, list):
            raise CouponUseError('官方未返回卡券列表，请重新查询。', 'LIST_MISSING')
        row = next((row for group in groups for row in group.get('batchInfo', [])
                    if _item(row)['id'] == operation.coupon_id), None)
        if row is None or _item(row)['status'] != 'available':
            raise CouponUseError('此卡券尚未生效、已使用或已失效，请刷新卡券列表。', 'COUPON_NOT_AVAILABLE', 409)
        return row

    def _rule(self, operation, row):
        user = operation.user
        if operation.kind == 'recharge':
            self._post(operation, '/i/v1/cust/pcardCheck/'+user['number'],
                       {'phoneNo': user['number'], 'busiType': 'hf01', 'pcardNo': row['pCardNo'], 'channel': '11'})
        rule = self._post(operation, '/i/v1/pay/ruleinfo/'+user['number'],
            {'payPhoneNo': user['number'], 'pCardNo': row['pCardNo'], 'homeProv': user['province'], 'channel': '0003'})
        if not rule.get('operateId'):
            raise CouponUseError('官方未返回有效用券规则，本次没有提交。', 'RULE_MISSING', 409)
        return rule

    def _initialize_app(self, operation):
        """Use the distinct wallet and recharge APP initialization protocols."""
        operation.current()
        snapshot = self.credentials(operation.login_id)
        uid = re.search(r'(?:^|;)\s*UID=(\w+)', snapshot['cookie'])
        if uid is None:
            raise CouponUseError('APP 凭据未包含充值订单身份，请校验并重连账号。', 'UID_MISSING', 409)
        operation.uid = uid[1]
        profile = snapshot.get('profile') or {}
        operation.recharge_profile = profile
        device = {key: profile.get(key) or '' for key in
                  ('sb', 'sn', 'osType', 'packageName', 'imei', 'loginProvince', 'loginCity')}
        if operation.kind != 'recharge':
            login_device = AppLoginSession.from_credentials(snapshot).envelope({})
            device.update({key: login_device[key] for key in ('sb', 'sn', 'packageName', 'imei')})
            device.update(osType='android', loginProvince=profile.get('provinceCode') or '',
                          loginCity=profile.get('cityCode') or '')
            android_id = (snapshot.get('app_context') or {}).get('device_id', '')[:16]
            device['imei'] = profile.get('imei') or ('AndroidId:'+android_id+'0' if android_id else '')
        signed = 'B3A128FA'+operation.uid+'B3A128FA'
        assertion = {'UID': operation.uid, 'sign': hmac.new(b'', signed.encode(), hashlib.md5).hexdigest(),
                     'deviceInfo': device}
        if operation.kind != 'recharge':
            data = self._post(operation, '/i/v1/cust/initMessage?'+str(int(time.time()*1000)),
                {'data': _cbc_encrypt(_json(assertion)), 'channel': '12'}, plain=True)
            if not data.get('msgId'):
                raise CouponUseError('官方未返回 APP 用券会话，请重新打开用券窗口。', 'WALLET_INIT_FAILED', 409)
            operation.user['msg_id'] = data['msgId']
        else:
            number = operation.user['number']
            shortened = number[:2]+number[3:]
            wel = base64.b64encode(_cbc_encrypt(shortened).encode()).decode()
            data = self._post(operation, '/i/v1/cust/initMessage?wel='+wel,
                              {'data': _cbc_encrypt(_json(assertion)), 'channel': '11'})
        if 'update' in data:
            raise CouponUseError('官方充值服务正在维护，请稍后再试。', 'RECHARGE_MAINTENANCE', 409)

    def prepare(self, login_id, coupon_id):
        if not isinstance(login_id, str) or not login_id or not isinstance(coupon_id, str) or not re.fullmatch(r'[0-9a-f]{24}', coupon_id):
            raise CouponUseError('请选择账号及卡券。', 'INVALID_SELECTION', 400)
        self._prune()
        key = hashlib.sha256(_json([login_id, coupon_id]).encode()).hexdigest()
        with self.lock:
            record = self.data.get(key)
            if record and record['state'] == 'payment_required':
                if not record.get('login_id'):
                    updated = deepcopy(self.data)
                    updated[key].update(login_id=login_id, coupon_id=coupon_id)
                    self._save(updated)
                return {'state': 'payment_required', 'order': {'order_id': record['order_id'],
                        'coupon_name': record.get('coupon_name', ''), 'amount': record.get('charge_amount')}}
            self._check_submission(key)
        client = CouponClient(lambda: self.credentials(login_id), entry=self.wallet_entry)
        context = client._session()
        http, user, current = context.__enter__()
        operation = SimpleNamespace(id=secrets.token_urlsafe(24), login_id=login_id, coupon_id=coupon_id, key=key,
            client=http, user=user, current=current, context=context, expires=time.monotonic()+600,
            lock=Lock(), kind='', row=None, rule=None, choices=[], serial='', md5='', result=None,
            captcha_required=False, verified_sms=None, sdk_bound=False, risk_user_agent='',
            risk_token='', risk_callback_matches_c1=False, sms_sent=False, detail={})
        try:
            app_cookie = self.credentials(login_id)['cookie'].split(';', 1)[0].split('=')
            entry_query = parse_qs(urlsplit(self.wallet_entry).query, keep_blank_values=True)
            channel = entry_query.get('channelId', [''])[0]
            yx = entry_query.get('yx', [''])[0] if channel.startswith('P') else ''
            operation.attribution = {'wtAc': (channel if channel.startswith('P') else '')+'_'+yx,
                                     'wtAcId': app_cookie[1]}
            row = self._row(operation)
            kind = _item(row)['use_kind']
            if not kind:
                raise CouponUseError('此类卡券暂不支持在本程序使用，请前往 APP。', 'TYPE_UNSUPPORTED', 409)
            if row.get('useChannel') and '11' not in str(row['useChannel']).split('|'):
                raise CouponUseError('官方不允许在 APP 渠道使用此券。', 'CHANNEL_UNSUPPORTED', 409)
            operation.kind = kind
            self._initialize_app(operation)
            operation.row = row
            if kind == 'rights':
                if not row.get('activityId') or not row.get('stallId'):
                    raise CouponUseError('官方未返回完整兑换信息，请在 APP 查看此券。', 'RULE_MISSING', 409)
                detail = self._post(operation, '/i/v1/cust/pcardInfoQry/'+user['number'],
                    {'loginNo': user['number'], 'channelId': '11', 'batchId': '', 'loginType': user['login_type'],
                     'pcardPasswd': row['pCardNo'], 'pcardType': row['pCardType'], 'provincode': user['province']}, gcm=True)
                if detail.get('agreementName') or detail.get('agreementUrl'):
                    raise CouponUseError('此券需要阅读并确认官方协议，请前往 APP 兑换。', 'VERIFICATION_REQUIRED', 409)
                operation.detail, operation.rule = detail, {}
                requires_sms = str(detail.get('smsCheck')) == '1'
                if requires_sms and not detail.get('activityId'):
                    raise CouponUseError('官方未返回完整短信认证信息，请重新查询此券。', 'RULE_MISSING', 409)
            else:
                operation.rule = self._rule(operation, row)
                requires_sms = str(row.get('smsCheck')) == '1' or kind == 'exchange' and _money(row.get('valueFormat') or row.get('couponValue')) > 3
            amount = _money(row.get('valueFormat') or row.get('couponValue'))
            if operation.kind == 'recharge':
                if requires_sms:
                    raise CouponUseError('此充值券需要 APP 二次验证，请前往 APP 使用。', 'VERIFICATION_REQUIRED', 409)
                encrypted_phone = base64.b64encode(_cbc_encrypt(user['number']).encode()).decode()
                pay_rules = self._post(operation, '/i/v1/pay/payrule/'+encrypted_phone,
                    {'amountFlag': 1, 'saleFlag': 1, 'enableFlag': 1, 'couponFlag': 1, 'saleQueryType': 2,
                     'cellNum': user['number'], 'phoneNo': base64.b64encode(user['number'].encode()).decode(),
                     'enableType': 0, 'provCode': user['province'], 'channel': '11', 'additionFlag': 1})
                operation.choices = _choices(operation.rule, pay_rules.get('amountRule'))
            current()
            operation.requires_sms = bool(requires_sms)
            with self.lock:
                self.operations[operation.id] = operation
            return {'operation_id': operation.id, 'kind': operation.kind,
                'coupon_name': _item(row)['name'], 'account_masked': user['number'][:3]+'****'+user['number'][-4:],
                'coupon_amount': _decimal_text(amount), 'coupon_unit': str(row.get('unitFormat') or row.get('unitName') or '元'),
                'description': str(row.get('stallInfo') or ''),
                'requires_sms': operation.requires_sms,
                'choices': [{'value': choice['value'], 'label': choice['label']} for choice in operation.choices],
                'message': '确认后提交兑换申请，无需支付费用。' if operation.kind != 'recharge' else '确认后生成待支付充值订单，并显示官方支付入口。'}
        except BaseException:
            self._close(operation)
            raise

    def sms(self, operation_id):
        with self._operation(operation_id) as operation:
            if not operation.requires_sms or operation.kind == 'recharge':
                raise CouponUseError('此操作无需发送兑换验证码。', 'SMS_NOT_REQUIRED', 400)
            with self.lock:
                self._check_submission(operation.key)
            user, row = operation.user, operation.row
            if operation.kind == 'rights':
                body = {'phoneNo': user['number'], 'msgType': '05', 'pcardName': '', 'pcardNo': '',
                        'busiCode': '', 'busiName': '', 'busiActId': operation.detail.get('activityId'),
                        'provPcardId': row['stallId'], 'ifProvSendMsg': '1', 'useChannelId': '11'}
                if 'marketCode' in row:
                    body['marketCode'] = row['marketCode']
                if 'provPcardType' in operation.detail:
                    body['busiType'] = operation.detail['provPcardType']
                self._post(operation, '/i/v1/cust/commonSms/'+user['number'], body)
                operation.sms_sent = True
            else:
                data = self._post(operation, '/i/v1/pay/pcardsms/'+user['number'],
                    {'phoneNo': user['number'], 'pcardName': row['stallName'], 'pcardAmount': row['couponValue'],
                     'pcardNo': row['pCardNo'], 'channel': '02'})
                if not data.get('serialNo'):
                    raise CouponUseError('官方未返回验证码流水号，请重新获取。', 'SMS_FORMAT')
                operation.serial = str(data['serialNo'])
                operation.md5 = str(data['md5']) if data.get('md5') is not None else None
            operation.verified_sms = None
            operation.sdk_bound = False
            operation.risk_token = ''
            operation.expires = time.monotonic()+600
            return {'message': '验证码已发送到当前账号手机号，请填写收到的验证码。'}

    def wallet_sdk(self, operation_id, method, query, headers, body, risk_lid):
        if not isinstance(risk_lid, str) or not risk_lid or len(risk_lid) > 8192 or any(c in risk_lid for c in '\r\n;'):
            raise CouponUseError('官方设备校验值未就绪，请重试。', 'RISK_INITIALIZATION_FAILED', 400)
        with self._operation(operation_id) as operation:
            operation.current()
            if operation.kind not in ('exchange', 'rights') or operation.result is not None:
                raise CouponUseError('请重新打开当前卡券的用券窗口。', 'RISK_STEP_REQUIRED', 409)
            client = operation.client
            client.cookies.set('_zw_kvani5r', risk_lid, domain='touch.10086.cn', path='/', secure=False)
            saved_headers = client.headers
            client.headers = dict(headers)
            try:
                response = client.request(method,
                    ORIGIN+'/i/mobile/tfk/mgt/decision-engine/udid/c1'+('?' + query if query else ''),
                    data=body, timeout=(5, 15), allow_redirects=False)
            finally:
                client.headers = saved_headers
            try:
                result = response.json()
            except ValueError:
                result = {}
            token = result.get('data') if isinstance(result, dict) else None
            success = (response.status_code == 200 and isinstance(result, dict) and result.get('status') in (1, 2)
                       and isinstance(token, str) and bool(token) and len(token) <= 8192
                       and not any(char in token for char in '\r\n;'))
            logging.getLogger(__name__).warning('coupon_sdk_c1_result %s', _json({
                'status': result.get('status') if isinstance(result, dict) else None,
                'data_length': len(token) if isinstance(token, str) else None,
                'data_sha256': hashlib.sha256(token.encode()).hexdigest() if isinstance(token, str) else None,
                'accepted': success}))
            if success:
                operation.risk_token = token
            operation.sdk_bound = bool(operation.risk_token)
            operation.risk_user_agent = headers.get('User-Agent', '')
            return response

    def confirm(self, operation_id, sms_code='', charge_amount='', captcha_token='', risk_token='', risk_lid='',
                prepare_risk=False, browser_user_agent=''):
        if not isinstance(prepare_risk, bool):
            raise CouponUseError('用券步骤无效，请重新打开用券窗口。', 'INVALID_STEP', 400)
        if not isinstance(captcha_token, str) or len(captcha_token) > 4096:
            raise CouponUseError('验证凭据格式无效，请重新完成官方图形验证。', 'INVALID_CAPTCHA_TOKEN', 400)
        if any(not isinstance(value, str) or len(value) > 8192 or any(char in value for char in '\r\n;')
               for value in (risk_token, risk_lid)):
            raise CouponUseError('官方用券校验值无效，请重新打开用券窗口。', 'INVALID_RISK_TOKEN', 400)
        with self._operation(operation_id) as operation:
            if operation.result is not None:
                return dict(operation.result)
            with self.lock:
                self._check_submission(operation.key)
            if operation.captcha_required and not captcha_token:
                raise CouponUseError('请先完成中国移动官方图形验证，再确认生成订单。', 'CAPTCHA_REQUIRED', 400)
            if operation.requires_sms and (not isinstance(sms_code, str) or not re.fullmatch(r'\d{6}', sms_code)
                    or not (operation.sms_sent if operation.kind == 'rights' else operation.serial)):
                raise CouponUseError('请先获取并填写六位短信验证码。', 'SMS_REQUIRED', 400)
            choice = next((choice for choice in operation.choices if choice['value'] == charge_amount), None)
            if operation.kind == 'recharge' and choice is None:
                raise CouponUseError('请选择官方允许的充值档位。', 'INVALID_AMOUNT', 400)
            row, rule = operation.row, operation.rule
            if operation.kind == 'recharge':
                row = self._row(operation)
                if any(row.get(key) != operation.row.get(key) for key in
                       ('couponValue', 'valueFormat', 'cardVersion', 'pCardType', 'busiType', 'smsCheck', 'useChannel')):
                    raise CouponUseError('官方卡券信息已变化，请重新打开用券窗口确认。', 'COUPON_CHANGED', 409)
                rule = self._rule(operation, row)
                if any(rule.get(key) != operation.rule.get(key) for key in
                       ('operateId', 'saleType', 'saleCode', 'discountFormat', 'pcardType', 'pCardType')):
                    raise CouponUseError('官方用券规则已变化，请重新打开用券窗口确认。', 'RULE_CHANGED', 409)
            user = operation.user
            verified_sms = (operation.serial, sms_code)
            if operation.kind == 'exchange' and operation.requires_sms and operation.verified_sms != verified_sms:
                self._post(operation, '/i/v1/pay/pcardvalid/'+user['number'],
                    {'sms_code': sms_code, 'serial_no': operation.serial, 'channel': '02'})
                operation.verified_sms = verified_sms
            if operation.kind in ('exchange', 'rights'):
                if prepare_risk:
                    operation.risk_token = ''
                    operation.sdk_bound = False
                    return {'state': 'risk_required', 'message': '正在获取官方用券校验凭据。',
                            'sdk_server': '/coupon-verification/wallet/udid/c1?operation_id='+operation.id}
                if not operation.risk_token or not operation.sdk_bound:
                    raise CouponUseError('官方用券校验未完成，请重试；已通过的短信校验会保留。',
                                         'RISK_INITIALIZATION_FAILED', 400)
                operation.risk_callback_matches_c1 = risk_token == operation.risk_token
                if not operation.risk_callback_matches_c1:
                    raise CouponUseError('官方用券回调与本次校验数据不一致，请重试；已通过的短信校验会保留。',
                                         'RISK_INITIALIZATION_FAILED', 400)
                operation.client.cookies.set('stc-rrt-token', operation.risk_token,
                    domain='touch.10086.cn', path='/i', secure=True)
                operation.client.headers['User-Agent'] = operation.risk_user_agent
            if operation.kind == 'exchange':
                body = {'activityNO': '', 'amount': 0, 'channel': '11', 'chargeMoney': row['couponValue'],
                    'choseMoney': row['couponValue'], 'homeProv': user['province'], 'md5': operation.md5,
                    'numFlag': '0', 'operateId': rule['operateId'], 'pCardNo': row['pCardNo'],
                    'serialNo': operation.serial, 'smsCode': sms_code if operation.requires_sms else '',
                    'source': '', **operation.attribution}
                if operation.requires_sms:
                    body['smsCheck'] = row.get('smsCheck')
                if operation.md5 is None:
                    body.pop('md5')
                path = '/i/v1/pay/saveorder/'+user['number']+'?provinceId='+user['province']
                plain = False
            elif operation.kind == 'rights':
                body = {'customerId': user['number'], 'loginType': user['login_type'],
                        'activityId': row['activityId'], 'pcardPassword': row['pCardNo'],
                        'busiType': row['busiType'], 'pcardType': row['pCardType'], 'channelId': '11',
                        'goodsId': row['stallId'], 'smsCode': sms_code if operation.requires_sms else ''}
                path, plain = '/i/v1/cust/rightsCouponsUse/'+user['number'], False
            else:
                operation.current()
                profile = operation.recharge_profile
                business = {'operateId': rule['operateId'], 'amount': choice['submit_amount'], 'serialNo': '', 'smsCode': '',
                    'chargeMoney': choice['credit'], 'choseMoney': choice['value'], 'pCardNo': row['pCardNo'],
                    'numFlag': '1', 'additionOperateId': '', 'additionAmount': '', 'additionType': '',
                    'reConfirmWay': '', 'fkToken': captcha_token or None, 'lastCharOfName': None, 'channel': '11', 'payWay': 'SDK',
                    'pointId': None, 'pointNum': None, 'pointFee': None, 'invoinceType': '',
                    'appUid': operation.uid, 'uid': operation.uid}
                if choice['special_reduction'] is not None:
                    business.update(newAmount=choice['payment'], jkCardFee=choice['special_reduction'])
                if 'imei' in profile:
                    business['appImei'] = profile['imei']
                business.update(wtAc='', wtAcId='')
                text = _json(business)
                body = {'payhmcc': hmac.new(b'kk9n54Vh2Bc8aXChLHYPTQ==', ('123456'+text+'123456').encode(), hashlib.md5).hexdigest(),
                        'param': base64.b64encode(text.encode()).decode(), 'channel': '11'}
                encrypted_phone = base64.b64encode(_cbc_encrypt(user['number']).encode()).decode()
                path, plain = '/i/v1/pay/saveorderh5/'+encrypted_phone, False
            operation.current()
            with self.lock:
                self._check_submission(operation.key)
                updated = deepcopy(self.data)
                updated[operation.key] = {'state': 'submitted', 'operation_id': operation.id, 'result': None}
                if operation.kind == 'recharge':
                    updated[operation.key].update(login_id=operation.login_id, coupon_id=operation.coupon_id,
                        coupon_name=_item(row)['name'], created_at=time.time(),
                        charge_amount=choice['payment'], credit_amount=choice['credit'])
                self._save(updated)
            challenge = False
            try:
                data = self._post(operation, path, body, plain=plain, submitting=True)
                if operation.kind == 'recharge':
                    order_id = str(data.get('orderId') or '')
                    if not order_id:
                        raise _UnknownSubmission('订单提交后未返回有效订单号，请在 APP 我的订单中核实；不会重复下单。', 'ORDER_UNKNOWN')
                    result = {'state': 'payment_required', 'order_id': order_id,
                        'message': '充值订单已生成，请通过官方支付入口自行完成付款；付款前尚未充值。'}
                elif operation.kind == 'rights':
                    result = {'state': 'accepted', 'message': '流量兑换申请已受理，官方预计 24 小时内到账，请关注短信通知。'}
                    if data.get('twiceSmsVerify'):
                        result['message'] += '请按运营商短信要求回复确认，完成二次验证。'
                else:
                    result = {'state': 'accepted', 'message': '兑换申请已受理，请关注话费到账情况；兑换失败时官方将退还卡券。'}
            except _UnknownSubmission as error:
                result = {'state': 'unknown', 'message': str(error)}
            except CouponError as error:
                if operation.kind == 'recharge' and error.code in ('531023', '531027'):
                    challenge = True
                    result = {'state': 'verification_required', 'verification': 'captcha',
                              'message': '请完成中国移动官方图形验证，再确认生成待支付订单。'}
                else:
                    result = {'state': 'failed', 'message': str(error), 'code': error.code}
            finally:
                if not challenge:
                    self._close(operation)
            operation.captcha_required = challenge
            if not challenge:
                operation.result = result
            with self.lock:
                updated = deepcopy(self.data)
                updated[operation.key] = {**updated[operation.key], 'state': 'failed' if challenge else result['state'], 'operation_id': operation.id,
                                          'result': result, 'order_id': result.get('order_id', '')}
                self._save(updated)
            return dict(result)

