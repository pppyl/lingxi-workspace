"""Fetch payment parameters for an existing order from the official APP gateway.

The caller must bind the order to the currently selected local pending operation.
Recharge query amounts are in fen: a verified 30-yuan order returns "3000".
Amounts remain decimal strings; this module never calls Alipay's pay SDK or
confirms a payment.
"""
import base64
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import ipaddress
import re
from xml.etree import ElementTree as ET

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from coupon_client import CouponError, SHANGHAI
from http_transport import CurlHttpVersion, requests

QUERY_ENDPOINT = 'https://pay.shop.10086.cn/iphone_paygw/innerpaygw'
ENDPOINT = 'https://pay.shop.10086.cn/iphone_paygw/innerpaygwreq'
APP_ID = '2683091355271000'
SDK_VERSION = '2.4.2_002'
SIGN_SUFFIX = 'jXJG68rvyubytEo5hYplKTDmnVK5bdFp'
RESPONSE_KEY = base64.b64encode(bytes(value ^ (value >> 4) for value in b'inner_code'))
RESPONSE_IV = b'sdkL04wHGANhWuqU'


class CouponAlipayError(CouponError):
    pass


def _xml(fields):
    root = ET.Element('msgReq')
    for key, value in fields:
        ET.SubElement(root, key).text = str(value)
    return (b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            + ET.tostring(root, encoding='utf-8', short_empty_elements=False))


def _decrypt_response(text):
    decryptor = Cipher(algorithms.AES(RESPONSE_KEY), modes.CBC(RESPONSE_IV)).decryptor()
    plain = decryptor.update(base64.b64decode(text, validate=True)) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return unpadder.update(plain) + unpadder.finalize()


class CouponAlipay:
    def __init__(self, credentials_provider, *, session_factory=None):
        self.credentials = credentials_provider
        self.session_factory = session_factory or (lambda: requests.Session(impersonate='chrome'))

    @contextmanager
    def _session(self, order_id):
        snapshot = self.credentials()
        if not isinstance(order_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', order_id):
            raise CouponAlipayError('订单号无效。', 'ALIPAY_ORDER_INVALID', 400)
        if (not isinstance(snapshot, dict)
                or not re.fullmatch(r'1[3-9]\d{9}', str(snapshot.get('phone', '')))
                or not isinstance(snapshot.get('cookie'), str)
                or 'JSESSIONID=' not in snapshot['cookie']):
            raise CouponAlipayError('请先恢复当前账号的 APP 登录。', 'APP_LOGIN_REQUIRED', 401)
        client = self.session_factory()
        # The native SDK sends no APP cookie or H5 bearer token to this endpoint.
        client.headers.update({'Content-Type': 'application/octet-stream',
                               'x-iap-sdk-ver': SDK_VERSION, 'x-iap-os': 'android',
                               'Expect': '100-continue'})
        try:
            yield client, snapshot
        finally:
            client.close()

    def _request(self, client, snapshot, fields, *, encrypted=False, verification_quote=None):
        stage = 'begin_pay' if encrypted else 'query_pay_type'
        latest = self.credentials()
        if (latest is None or latest.get('phone') != snapshot['phone']
                or latest.get('cookie') != snapshot['cookie']
                or latest.get('credential_revision') != snapshot.get('credential_revision')):
            raise CouponAlipayError('账号凭据已变化，请重新获取支付信息。', 'APP_CREDENTIALS_CHANGED', 409)
        payload = _xml(fields)
        if encrypted:
            # Raw Dalvik @2da branches to AES when the SDK flag is false.
            # JADX inverted this condition in its reconstructed expression.
            padder = padding.PKCS7(128).padder()
            encryptor = Cipher(algorithms.AES(RESPONSE_KEY), modes.CBC(RESPONSE_IV)).encryptor()
            payload = base64.b64encode(encryptor.update(padder.update(payload)+padder.finalize())
                                       +encryptor.finalize())
        try:
            response = client.post(ENDPOINT if encrypted else QUERY_ENDPOINT,
                                   data=payload, timeout=(10, 20),
                                   allow_redirects=False, http_version=CurlHttpVersion.V1_1)
        except requests.RequestsError as cause:
            error = CouponAlipayError('连接官方支付网关失败，请重新查询；没有付款。', 'ALIPAY_NETWORK')
            error.safe = {'stage': stage}
            raise error from cause
        if response.status_code != 200:
            error = CouponAlipayError('官方支付网关暂未返回有效结果。', 'ALIPAY_HTTP')
            error.safe = {'stage': stage, 'http_status': response.status_code,
                          'content_type': response.headers.get('Content-Type', '').split(';', 1)[0],
                          'response_bytes': len(response.content)}
            raise error
        try:
            raw = _decrypt_response(response.text.strip()) if encrypted else response.content
            root = ET.fromstring(raw)
        except (ValueError, ET.ParseError) as cause:
            error = CouponAlipayError('官方支付响应无法解析；没有付款。', 'ALIPAY_FORMAT')
            error.safe = {'stage': stage, 'response_bytes': len(response.content)}
            raise error from cause
        code = root.findtext('returnCode', '')
        if code != '0':
            if encrypted and code == 'CV4':
                app_id, risk_id = root.findtext('riskAppId', ''), root.findtext('riskId', '')
                if (re.fullmatch(r'[A-Za-z0-9_-]{1,128}', app_id)
                        and risk_id and len(risk_id) <= 2048):
                    error = CouponAlipayError('请完成人工滑块验证后继续获取支付链接。',
                                              'ALIPAY_VERIFICATION_REQUIRED', 409)
                    error.challenge = {'app_id': app_id, 'risk_id': risk_id}
                    error.quote = {**verification_quote, 'credential_revision': snapshot.get('credential_revision')}
                    raise error
            safe_code = code if re.fullmatch(r'[A-Za-z0-9_-]{1,32}', code) else 'unknown'
            description = re.sub(r'1[3-9]\d{9}', lambda m: m[0][:3]+'****'+m[0][-4:],
                                 root.findtext('returnMsg', ''))[:180]
            error = CouponAlipayError((description or '官方支付网关未完成操作')+'（'+safe_code+'）；没有付款。',
                                      'ALIPAY_RELOGIN_REQUIRED' if safe_code == 'CV3' else 'ALIPAY_REJECTED', 409)
            error.safe = {'stage': stage, 'return_code': safe_code, 'description': description}
            # Only SP13 has been confirmed against the provider's live message.
            if safe_code == 'SP13':
                error.safe.update(closed=True, order_state='expired')
            raise error
        return root

    def _query(self, client, snapshot, order_id):
        # phonePay is a separate, initially empty SDK field, not phoneNo.
        root = self._request(client, snapshot, [
            ('msgType', 'QueryPayTypeReq'), ('msgVer', '2.2'), ('channelId', '20'),
            ('APPId', APP_ID), ('payWay', ''), ('orderId', order_id),
            ('language', '0'), ('userNet', '1'), ('numberStatus', '1')])
        returned_order = root.findtext('orderId')
        if returned_order is not None and returned_order != order_id:
            raise CouponAlipayError('官方返回的订单号不一致，本次已停止。', 'ALIPAY_ORDER_MISMATCH', 409)
        return {'order_id': order_id, 'returned_order_id': returned_order,
                'pay_order_id': root.findtext('payOrderId', ''),
                'amount': root.findtext('amount', ''), 'total_amount': root.findtext('totalAmount', ''),
                'product_name': root.findtext('productName', ''),
                'account_masked': snapshot['phone'][:3]+'****'+snapshot['phone'][-4:],
                'gateways': [{'gateway_id': item.findtext('payOrganization', ''),
                              'name': item.findtext('payOrganizationName', '')}
                             for item in root.iter('payTypeItem')]}

    def query_order(self, order_id):
        """Return metadata; recharge amount/total_amount are strings in fen."""
        with self._session(order_id) as (client, snapshot):
            return self._query(client, snapshot, order_id)

    def get_payment(self, order_id, *, expected_amount, ip_address, gateway_id=None,
                    verify_key='', verified_quote=None):
        """Fetch an Alipay web URL; expected_amount is the approved amount in fen.

        For a 30-yuan recharge pass "3000", not "30". The caller binds this to
        the local pending order; this method compares it with the fresh gateway
        query without converting units. A response without the official web
        channel is rejected rather than converted into a client launch URI.
        """
        try:
            expected = Decimal(str(expected_amount))
            local_ip = str(ipaddress.IPv4Address(ip_address))
        except (InvalidOperation, ValueError) as error:
            raise CouponAlipayError('支付金额或本机网络地址无效。', 'ALIPAY_INPUT_INVALID', 400) from error
        if not expected.is_finite() or expected <= 0:
            raise CouponAlipayError('支付金额无效。', 'ALIPAY_INPUT_INVALID', 400)
        with self._session(order_id) as (client, snapshot):
            if verify_key:
                if not isinstance(verified_quote, dict) or verified_quote.get('order_id') != order_id:
                    raise CouponAlipayError('请重新获取此订单的支付验证。', 'PAYMENT_CAPTCHA_EXPIRED', 409)
                if verified_quote.get('credential_revision') != snapshot.get('credential_revision'):
                    raise CouponAlipayError('账号凭据已更新，请重新获取支付验证。', 'APP_CREDENTIALS_CHANGED', 409)
                query = verified_quote
            else:
                query = self._query(client, snapshot, order_id)
            try:
                amount = Decimal(query['amount'])
            except InvalidOperation as error:
                raise CouponAlipayError('官方未返回可核对的金额。', 'ALIPAY_AMOUNT_INVALID', 409) from error
            if not amount.is_finite() or amount != expected:
                raise CouponAlipayError('官方订单金额与已核对金额不一致，本次已停止。', 'ALIPAY_AMOUNT_MISMATCH', 409)
            candidates = [item['gateway_id'] for item in query['gateways'] if 'ALIPAY' in item['gateway_id']]
            if gateway_id is None:
                gateway_id = 'ALIPAY' if 'ALIPAY' in candidates else (candidates[0] if candidates else None)
            if gateway_id not in candidates:
                raise CouponAlipayError('该订单未提供所选支付宝支付方式。', 'ALIPAY_UNAVAILABLE', 409)
            stamp = datetime.now(SHANGHAI).strftime('%Y%m%d%H%M%S')
            signed = ['SDKPayReq', '1.5', '20', APP_ID, 'APP', gateway_id, order_id, local_ip, stamp]
            digest = hashlib.md5((''.join(signed)+SIGN_SUFFIX).encode()).hexdigest()
            root = self._request(client, snapshot, [
                ('msgType', signed[0]), ('msgVer', signed[1]), ('channelId', signed[2]),
                ('APPId', signed[3]), ('payWay', signed[4]), ('gatewayId', signed[5]),
                ('orderId', signed[6]), ('ipAddress', signed[7]), ('ts', signed[8]),
                # Web checkout requests the direct Alipay route. Native SDK
                # diversion returns a Chinaums UAPP cashier, not a web pay URL.
                ('hmac', digest), ('language', '0'), ('diversionFlag', '0'),
                ('userNet', '1'), ('numberStatus', '1')]
                + ([('verifyKey', verify_key)] if verify_key else []), encrypted=True,
                verification_quote=query)
            pay_order_id = root.findtext('payOrderId', '')
            route = root.findtext('paymentRout', '')
            if route.casefold() == 'alipay':
                from alipay_h5 import AlipayH5, AlipayH5Error
                sdk_order = root.findtext('payPrmts/p1', '')
                if not sdk_order:
                    raise CouponAlipayError('官方未返回支付宝支付参数。', 'ALIPAY_PAYLOAD_MISSING', 409)
                try:
                    payload = AlipayH5().payment_url(sdk_order)
                except AlipayH5Error as cause:
                    raise CouponAlipayError(str(cause), cause.code, 409) from cause
                return {'order_id': order_id, 'pay_order_id': pay_order_id, 'amount': query['amount'],
                        'account_masked': query['account_masked'], 'gateway_id': gateway_id,
                        'payment_route': route, 'payload_type': 'channel_url', 'payload': payload,
                        'payment_type': 'alipay_web', 'payment_url': payload}
            raise CouponAlipayError('官方本次未返回支付宝直接付款通道，请重新获取支付链接。',
                                    'ALIPAY_WEB_UNAVAILABLE', 409)
