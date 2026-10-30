from copy import deepcopy
from datetime import datetime
import base64
import hashlib
import unittest
from unittest.mock import patch
from xml.etree import ElementTree as ET

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from coupon_alipay import (APP_ID, CouponAlipay, CouponAlipayError,
                            RESPONSE_IV, RESPONSE_KEY, SIGN_SUFFIX)
from http_transport import CurlHttpVersion, requests

ORDER = 'fixture-existing-order'
PHONE = '13800000000'
SNAPSHOT = {'phone': PHONE, 'cookie': 'JSESSIONID=fixture-private', 'credential_revision': 1}
WEB = 'https://mclient.alipay.com/cashierRoutePay.htm?awid=fixture'


class Response:
    def __init__(self, text, status=200):
        self.text, self.content, self.status_code = text, text.encode(), status
        self.headers = {'Content-Type': 'text/xml; charset=UTF-8'}


def encrypt_xml(fields):
    root = ET.Element('msgRsp')
    for key, value in fields.items():
        element = ET.SubElement(root, key)
        if isinstance(value, dict):
            for nested_key, nested_value in value.items():
                ET.SubElement(element, nested_key).text = nested_value
        else:
            element.text = value
    plain = ET.tostring(root)
    padder = padding.PKCS7(128).padder()
    encrypted = Cipher(algorithms.AES(RESPONSE_KEY), modes.CBC(RESPONSE_IV)).encryptor()
    return base64.b64encode(encrypted.update(padder.update(plain)+padder.finalize())
                            +encrypted.finalize()).decode()


class Session:
    def __init__(self):
        self.headers, self.calls, self.closed = {}, [], False
        self.amount, self.gateway = '2000', 'ALIPAY'
        self.query_returned_order = None
        self.after_query = None
        self.status, self.network_error = 200, False
        self.begin_status = 200
        self.begin = {'returnCode': '0', 'payOrderId': 'fixture-payment-order',
                      'paymentRout': 'alipay', 'payPrmts': {'p1': 'private-signed-order'}}

    def post(self, url, *, data, timeout, allow_redirects, http_version):
        wire_data = data
        if url.endswith('/innerpaygwreq'):
            cipher = Cipher(algorithms.AES(RESPONSE_KEY), modes.CBC(RESPONSE_IV)).decryptor()
            padded = cipher.update(base64.b64decode(data, validate=True))+cipher.finalize()
            unpadder = padding.PKCS7(128).unpadder()
            data = unpadder.update(padded)+unpadder.finalize()
        root = ET.fromstring(data)
        fields = {item.tag: item.text or '' for item in root}
        self.calls.append({'url': url, 'fields': fields, 'xml': data, 'wire_data': wire_data, 'timeout': timeout,
                           'allow_redirects': allow_redirects, 'http_version': http_version,
                           'headers': deepcopy(self.headers)})
        if self.network_error:
            raise requests.RequestsError('private network diagnostic')
        if fields['msgType'] == 'QueryPayTypeReq':
            result = ('<msgRsp><returnCode>0</returnCode><payOrderId>fixture-payment-order</payOrderId>'
                      '<amount>'+self.amount+'</amount><totalAmount>'+self.amount+'</totalAmount><payTypeList>'
                      '<payTypeItem><payOrganization>'+self.gateway+'</payOrganization>'
                      '<payOrganizationName>支付宝</payOrganizationName></payTypeItem>'
                      '</payTypeList></msgRsp>')
            if self.query_returned_order:
                result = result.replace('</msgRsp>', '<orderId>'+self.query_returned_order+'</orderId></msgRsp>')
            if self.after_query:
                self.after_query()
            return Response(result, self.status)
        if fields['msgType'] == 'SDKPayReq':
            return Response(encrypt_xml(self.begin), self.begin_status)
        raise AssertionError('Unexpected native payment operation')

    def close(self):
        self.closed = True


class CouponAlipayTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = deepcopy(SNAPSHOT)
        self.session = Session()
        self.client = CouponAlipay(lambda: deepcopy(self.snapshot), session_factory=lambda: self.session)
        exchange = patch('alipay_h5.AlipayH5.payment_url', return_value=WEB)
        self.exchange = exchange.start()
        self.addCleanup(exchange.stop)

    def payment(self, **kwargs):
        return self.client.get_payment(ORDER, expected_amount='2000', ip_address='192.168.1.2', **kwargs)

    def test_query_preserves_gateway_amount_units_and_sends_no_login_secrets(self):
        result = self.client.query_order(ORDER)
        self.assertEqual(result['amount'], '2000')
        self.assertIsNone(result['returned_order_id'])
        self.assertEqual(result['pay_order_id'], 'fixture-payment-order')
        call, = self.session.calls
        self.assertEqual(call['url'], 'https://pay.shop.10086.cn/iphone_paygw/innerpaygw')
        self.assertEqual(list(call['fields']), ['msgType', 'msgVer', 'channelId', 'APPId', 'payWay',
                                              'orderId', 'language', 'userNet', 'numberStatus'])
        self.assertEqual(call['fields']['APPId'], APP_ID)
        self.assertEqual(call['fields']['userNet'], '1')
        self.assertEqual(call['headers']['x-iap-sdk-ver'], '2.4.2_002')
        self.assertFalse(call['allow_redirects'])
        self.assertEqual(call['http_version'], CurlHttpVersion.V1_1)
        self.assertEqual(call['headers']['Expect'], '100-continue')
        self.assertNotIn(SNAPSHOT['cookie'], str(call))
        self.assertNotIn(PHONE, str(call))
        self.assertTrue(self.session.closed)

    def test_begin_uses_exact_signed_field_sequence_and_returns_web_url(self):
        with patch('coupon_alipay.datetime') as clock:
            clock.now.return_value = datetime(2026, 10, 30, 12, 0, 0)
            result = self.payment()
        self.assertEqual(len(self.session.calls), 2)
        self.assertEqual(self.session.calls[1]['url'], 'https://pay.shop.10086.cn/iphone_paygw/innerpaygwreq')
        self.assertTrue(self.session.calls[0]['wire_data'].startswith(b'<?xml'))
        self.assertNotIn(b'<msgReq>', self.session.calls[1]['wire_data'])
        fields = self.session.calls[1]['fields']
        expected = ['SDKPayReq', '1.5', '20', APP_ID, 'APP', 'ALIPAY', ORDER,
                    '192.168.1.2', '20261030120000']
        self.assertEqual(fields['hmac'], hashlib.md5((''.join(expected)+SIGN_SUFFIX).encode()).hexdigest())
        self.assertEqual(list(fields), ['msgType', 'msgVer', 'channelId', 'APPId', 'payWay', 'gatewayId',
                                       'orderId', 'ipAddress', 'ts', 'hmac', 'language', 'diversionFlag',
                                       'userNet', 'numberStatus'])
        self.assertEqual(result['payment_type'], 'alipay_web')
        self.assertEqual(fields['diversionFlag'], '0')
        self.assertEqual(result['payment_url'], WEB)

    def test_native_yinshang_cashier_is_not_presented_as_an_alipay_link(self):
        self.session.begin.update(paymentRout='Chinaums', tn='other')
        self.session.begin['payPrmts']['p2'] = 'https://cash.open.chinaums.com/h5-cashier/#/fixture?ticket=a%2Bb'
        with self.assertRaises(CouponAlipayError) as caught:
            self.payment()
        self.assertEqual(caught.exception.code, 'ALIPAY_WEB_UNAVAILABLE')
        self.exchange.assert_not_called()

    def test_channel_url_rejects_untrusted_origin_and_keeps_signed_url_unmodified(self):
        self.session.begin.update(paymentRout='Chinaums')
        for value in ['http://cash.open.chinaums.com/pay', 'https://cash.open.chinaums.com.evil.invalid/pay',
                      'https://cash.open.chinaums.com@evil.invalid/pay', 'https://cash.open.chinaums.com:444/pay']:
            self.session.begin['payPrmts']['p2'] = value
            with self.subTest(value=value), self.assertRaises(CouponAlipayError) as error:
                self.payment()
            self.assertEqual(error.exception.code, 'ALIPAY_WEB_UNAVAILABLE')

    def test_begin_payment_reference_need_not_equal_query_reference(self):
        self.session.begin['payOrderId'] = 'fixture-alipay-channel-reference'
        result = self.payment()
        self.assertEqual(result['pay_order_id'], 'fixture-alipay-channel-reference')
        self.assertEqual(result['order_id'], ORDER)

    def test_amount_mismatch_does_not_begin_payment(self):
        with self.assertRaises(CouponAlipayError) as caught:
            self.client.get_payment(ORDER, expected_amount='20', ip_address='192.168.1.2')
        self.assertEqual(caught.exception.code, 'ALIPAY_AMOUNT_MISMATCH')
        self.assertEqual(len(self.session.calls), 1)

    def test_expected_amount_is_in_fen(self):
        self.session.amount = '3000'
        result = self.client.get_payment(ORDER, expected_amount='3000', ip_address='192.168.1.2')
        self.assertEqual(result['amount'], '3000')
        self.assertEqual(len(self.session.calls), 2)

    def test_client_payment_parameters_are_not_converted_or_returned_as_a_link(self):
        from alipay_h5 import AlipayH5Error
        self.session.begin.update(paymentRout='alipay')
        self.session.begin['payPrmts']['p1'] = 'client-only-parameters'
        self.exchange.side_effect = AlipayH5Error('订单参数无效', 'ALIPAY_H5_ORDER_INVALID')
        with self.assertRaises(CouponAlipayError) as caught:
            self.payment()
        self.assertEqual(caught.exception.code, 'ALIPAY_H5_ORDER_INVALID')
        self.assertNotIn('client-only-parameters', str(caught.exception))

    def test_missing_official_payprmts_structure_is_rejected(self):
        self.session.begin['p1'] = self.session.begin.pop('payPrmts')['p1']
        with self.assertRaises(CouponAlipayError) as caught:
            self.payment()
        self.assertEqual(caught.exception.code, 'ALIPAY_PAYLOAD_MISSING')

    def test_alipay_route_exchanges_sdk_parameters_for_official_web_cashier(self):
        self.session.begin.update(paymentRout='alipay')
        self.session.begin['payPrmts']['p1'] = 'private-signed-sdk-order'
        with patch('alipay_h5.AlipayH5.payment_url', return_value='https://mclient.alipay.com/cashier/mobilepay.htm?awid=fixture') as exchange:
            result = self.payment()
        exchange.assert_called_once_with('private-signed-sdk-order')
        self.assertEqual(result['payment_type'], 'alipay_web')
        self.assertNotIn('private-signed-sdk-order', str(result))

    def test_payment_captcha_resumes_same_begin_with_verify_key_and_no_second_query(self):
        self.session.begin.update(returnCode='CV4', riskAppId='official-payment-app', riskId='official-risk-id')
        with self.assertRaises(CouponAlipayError) as caught:
            self.payment()
        error = caught.exception
        self.assertEqual(error.code, 'ALIPAY_VERIFICATION_REQUIRED')
        self.assertEqual(error.challenge, {'app_id': 'official-payment-app', 'risk_id': 'official-risk-id'})
        self.session.begin['returnCode'] = '0'
        self.payment(verify_key='human-solved-token', verified_quote=error.quote)
        self.assertEqual(len(self.session.calls), 3)
        fields = self.session.calls[-1]['fields']
        self.assertEqual(fields['msgType'], 'SDKPayReq')
        self.assertEqual(fields['orderId'], ORDER)
        self.assertEqual(fields['verifyKey'], 'human-solved-token')
        self.assertNotIn('fkToken', fields)
        self.assertNotIn('verifyCode', fields)

    def test_payment_captcha_from_old_credentials_cannot_resume_begin(self):
        self.session.begin.update(returnCode='CV4', riskAppId='official-payment-app', riskId='official-risk-id')
        with self.assertRaises(CouponAlipayError) as caught:
            self.payment()
        quote = caught.exception.quote
        self.snapshot['credential_revision'] = 2
        with self.assertRaises(CouponAlipayError) as changed:
            self.payment(verify_key='human-token', verified_quote=quote)
        self.assertEqual(changed.exception.code, 'APP_CREDENTIALS_CHANGED')
        self.assertEqual(len(self.session.calls), 2)

    def test_gateway_not_offered_does_not_begin_payment(self):
        self.session.gateway = 'WXPAY'
        with self.assertRaises(CouponAlipayError) as caught:
            self.payment()
        self.assertEqual(caught.exception.code, 'ALIPAY_UNAVAILABLE')
        self.assertEqual(len(self.session.calls), 1)

    def test_credential_change_between_query_and_begin_stops(self):
        self.session.after_query = lambda: self.snapshot.update(credential_revision=2)
        with self.assertRaises(CouponAlipayError) as caught:
            self.payment()
        self.assertEqual(caught.exception.code, 'APP_CREDENTIALS_CHANGED')
        self.assertEqual(len(self.session.calls), 1)

    def test_provider_verification_is_not_retried_or_bypassed(self):
        self.session.begin.update(returnCode='CV1', returnMsg='手机号'+PHONE+'需要再次验证')
        with self.assertRaises(CouponAlipayError) as caught:
            self.payment()
        description = '手机号138****0000需要再次验证'
        self.assertEqual(caught.exception.safe, {'stage': 'begin_pay', 'return_code': 'CV1',
                                                'description': description})
        self.assertIn(description, str(caught.exception))
        self.assertNotIn(PHONE, str(caught.exception))
        self.assertNotIn(PHONE, str(caught.exception.safe))
        self.assertEqual(len(self.session.calls), 2)

    def test_sp13_is_the_confirmed_expired_order_response(self):
        self.session.begin.update(returnCode='SP13', returnMsg='您的订单已超时，请重新下单支付')
        with self.assertRaises(CouponAlipayError) as caught:
            self.payment()
        self.assertEqual(caught.exception.code, 'ALIPAY_REJECTED')
        self.assertEqual(caught.exception.safe, {
            'stage': 'begin_pay', 'return_code': 'SP13', 'description': '您的订单已超时，请重新下单支付',
            'closed': True, 'order_state': 'expired'})
        self.assertEqual(len(self.session.calls), 2)

    def test_cv3_explicitly_requests_relogin_without_closing_or_retrying_order(self):
        self.session.begin.update(returnCode='CV3', returnMsg='请重新登录APP后再试')
        with self.assertRaises(CouponAlipayError) as caught:
            self.payment()
        self.assertEqual(caught.exception.code, 'ALIPAY_RELOGIN_REQUIRED')
        self.assertEqual(caught.exception.safe['return_code'], 'CV3')
        self.assertNotIn('closed', caught.exception.safe)
        self.assertEqual(len(self.session.calls), 2)

    def test_other_provider_codes_do_not_infer_closed_state_from_message(self):
        self.session.begin.update(returnCode='SP14', returnMsg='未知错误；请核对订单是否超时')
        with self.assertRaises(CouponAlipayError) as caught:
            self.payment()
        self.assertNotIn('closed', caught.exception.safe)
        self.assertNotIn('order_state', caught.exception.safe)
        self.assertEqual(caught.exception.safe['return_code'], 'SP14')

    def test_network_error_has_no_retry_and_closes_session(self):
        self.session.network_error = True
        with self.assertRaises(CouponAlipayError) as caught:
            self.client.query_order(ORDER)
        self.assertEqual(caught.exception.code, 'ALIPAY_NETWORK')
        self.assertEqual(len(self.session.calls), 1)
        self.assertTrue(self.session.closed)

    def test_http_error_identifies_query_and_begin_without_logging_payload(self):
        self.session.status = 500
        with self.assertRaises(CouponAlipayError) as query_error:
            self.client.query_order(ORDER)
        self.assertEqual(query_error.exception.safe['stage'], 'query_pay_type')
        self.session.status, self.session.begin_status = 200, 500
        with self.assertRaises(CouponAlipayError) as begin_error:
            self.payment()
        safe = begin_error.exception.safe
        self.assertEqual((safe['stage'], safe['http_status']), ('begin_pay', 500))
        self.assertEqual(set(safe), {'stage', 'http_status', 'content_type', 'response_bytes'})
        self.assertNotIn(self.session.begin['payPrmts']['p1'], str(safe))


if __name__ == '__main__':
    unittest.main()
