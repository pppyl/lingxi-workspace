from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from http_transport import requests

from coupon_cashier_h5 import (AUTH_PATH, ORDER_BASE, CouponCashierError, CouponCashierH5,
                               _decrypt, _encrypt)

PHONE = '13800000000'
ORDER = 'fixture-existing-order'
SNAPSHOT = {'phone': PHONE, 'cookie': 'JSESSIONID=private-session; UID=private-uid; ticketID=private-ticket',
            'token': 'private-lingxi-token', 'profile': {'provinceCode': '851'}, 'credential_revision': 1}


class Response:
    status_code = 200
    def __init__(self, value):
        self.value = value
    def json(self):
        return {'body': _encrypt(json.dumps(self.value, ensure_ascii=False, separators=(',', ':')))}


class Session:
    def __init__(self):
        self.headers, self.cookies, self.calls, self.closed = {}, {}, [], False
        self.user_phone = PHONE
        self.record = {'orderId': ORDER, 'loginType': '1', 'customerId': PHONE, 'orderType': '004',
                       'orderItems': [{'orderItemId': 'official-existing-item'}], 'chargeOrderFlag': '0'}
        self.detail = {'orderId': ORDER, 'rechargeNum': PHONE, 'orderType': '004', 'status': '17',
                       'charge': '37', 'chargeFee': '37', 'chargeChoseFee': '30',
                       'billCharge': '0', 'statusDesc': '待支付'}
        self.pay_url = 'https://pay.shop.10086.cn/fixture-cashier?private-order-ticket=example'
        self.error = None
        self.after_request = None
        self.reject = ''

    def post(self, url, *, data, headers, **options):
        body = json.loads(_decrypt(data))
        self.calls.append((url, body, deepcopy(headers)))
        if self.error:
            raise self.error
        if self.after_request:
            self.after_request()
        if self.reject:
            return Response({'returnCode': self.reject, 'returnMessage': 'private upstream account details'})
        if url.endswith(AUTH_PATH):
            result = {'userInfo': {'uid': self.user_phone, 'telephone': 'encrypted-telephone-placeholder32',
                                  'token': 'private-web-bearer'}}
        elif url.endswith(ORDER_BASE+'orderListQuery'):
            result = {'busiDataResp': [self.record]}
        elif url.endswith(ORDER_BASE+'orderDetailQuery'):
            result = self.detail
        elif url.endswith(ORDER_BASE+'orderGoPay'):
            result = {'payUrl': self.pay_url}
        else:
            raise AssertionError('Unsupported endpoint')
        return Response({'returnCode': '0', 'data': result})

    def close(self):
        self.closed = True


class CashierH5Tests(unittest.TestCase):
    def setUp(self):
        self.snapshot = deepcopy(SNAPSHOT)
        self.session = Session()
        self.client = CouponCashierH5(lambda: deepcopy(self.snapshot), session_factory=lambda: self.session)

    def test_inspect_only_authenticates_and_reads_the_existing_order(self):
        with patch('coupon_cashier_h5.datetime') as now:
            now.now.return_value = datetime(2026, 10, 30)
            result = self.client.inspect_order(ORDER, '30')
        self.assertEqual(result, {'order_id': ORDER, 'amount': '30.00', 'account_masked': '138****0000',
                                 'status': '17', 'status_description': '待支付', 'payable': True, 'closed': False})
        self.assertEqual(len(self.session.calls), 3)
        self.assertFalse(any(url.endswith('/orderGoPay') for url, *_ in self.session.calls))
        auth = self.session.calls[0]
        self.assertEqual(auth[1], {'appToken': SNAPSHOT['cookie']})
        self.assertEqual(auth[2]['x-appToken'], SNAPSHOT['cookie'])
        self.assertNotIn(SNAPSHOT['token'], json.dumps(self.session.calls))
        query = self.session.calls[1]
        self.assertEqual((query[1]['startTime'], query[1]['endTime']), ('20251030', '20261030'))
        self.assertEqual(query[1]['queryTypes'], [{'orderType': f'{index:03d}'} for index in range(1, 7)])
        self.assertEqual(query[2]['Accept'], 'application/json, text/plain, */*')
        detail = self.session.calls[2]
        self.assertEqual(detail[1]['orderItemId'], 'official-existing-item')
        self.assertEqual(detail[1]['loginType'], '1')
        self.assertEqual(detail[1]['customerId'], PHONE)
        self.assertEqual(detail[2]['Authorization'], 'private-web-bearer')
        self.assertNotIn('x-appToken', detail[2])
        self.assertEqual(detail[1]['xat1n'], SNAPSHOT['cookie'])
        self.assertTrue(self.session.closed)
        for secret in [PHONE, SNAPSHOT['cookie'], 'private-web-bearer', SNAPSHOT['token']]:
            self.assertNotIn(secret, json.dumps(result))

    def test_continue_returns_only_official_url_and_requests_wap_for_existing_order(self):
        result = self.client.continue_payment(ORDER, '30')
        self.assertEqual(result['payment_url'], self.session.pay_url)
        url, body, _ = self.session.calls[-1]
        self.assertTrue(url.endswith(ORDER_BASE+'orderGoPay'))
        self.assertEqual({key: body[key] for key in ('orderId', 'payWay', 'oprType', 'channel', 'loginChannel')},
                         {'orderId': ORDER, 'payWay': 'WAP', 'oprType': '01', 'channel': '0003', 'loginChannel': '12'})
        self.assertFalse(any(key in body for key in ('amount', 'chargeMoney', 'choseMoney', 'pCardNo')))
        self.assertEqual(len(self.session.calls), 4)

    def test_unknown_expected_amount_displays_official_selected_principal_not_gifted_total(self):
        self.session.detail['charge'] = '57'
        self.session.detail['chargeChoseFee'] = '50'
        self.session.detail['billCharge'] = '0'
        result = self.client.inspect_order(ORDER)
        self.assertEqual(result['amount'], '50.00')

    def test_inspect_returns_expired_order_status_but_never_continues_its_payment(self):
        self.session.detail.update(status='24', statusDesc='超时未付撤销')
        result = self.client.inspect_order(ORDER, '30')
        self.assertEqual((result['status'], result['closed'], result['payable']), ('24', True, False))
        self.assertEqual(result['status_description'], '超时未付撤销')
        self.assertEqual(result['amount'], '30.00')
        with self.assertRaises(CouponCashierError) as error:
            self.client.continue_payment(ORDER, '30')
        self.assertEqual(error.exception.code, 'CASHIER_ORDER_NOT_PAYABLE')
        self.assertIn('超时未付撤销', str(error.exception))
        self.assertEqual(error.exception.order_summary, result)
        self.assertFalse(any(url.endswith('/orderGoPay') for url, *_ in self.session.calls))

    def test_order_query_defaults_match_original_page_initialization_offline(self):
        source = Path(__file__).resolve().parents[1]/'analysis'/'coupons-cache'/'order-orderList.js'
        if not shutil.which('node') or not source.is_file():
            self.skipTest('Retained official order page and Node are required')
        with patch('coupon_cashier_h5.datetime') as now:
            now.now.return_value = datetime(2026, 10, 30)
            self.client.inspect_order(ORDER, '30')
        script = r"""
const vm = require('node:vm');
const source = require('node:fs').readFileSync(0, 'utf8');
const RealDate = Date;
class FixedDate extends RealDate {
  constructor(...args) { super(...(args.length ? args : ['2026-10-30T12:00:00Z'])); }
}
const ctx = {Date: FixedDate, _0x2a7702: {eA() { return ''; }}};
vm.createContext(ctx);
function expression(startText, endText, offset=0) {
  const start = source.indexOf(startText, offset);
  const end = source.indexOf(endText, start);
  if (start < 0 || end < 0) throw Error('Original initializer not found');
  return source.slice(start+startText.length, end+1);
}
vm.runInContext('var _0x47f9c7='+expression('_0x47f9c7=', '},_0x2d7bff')+';', ctx);
vm.runInContext('var _0x2d7bff='+expression('_0x2d7bff=', '},_0x25b7b2')+';', ctx);
const init = vm.runInContext('('+expression("'init':", "},'getOrderList':", 105000)+')', ctx);
const queryTypes = vm.runInContext('('+expression("'getQueryTypes':", "},'changeTag':", 105000)+')', ctx);
const state = {showTabNum:'', $refs:{}, getQueryTypes:queryTypes, getOrderList(){}};
init.call(state);
process.stdout.write(JSON.stringify({startTime:state.startTime,endTime:state.endTime,queryTypes:state.queryTypes}));
"""
        run = subprocess.run(['node', '-e', script], input=source.read_text(encoding='utf-8'),
                             capture_output=True, text=True, encoding='utf-8', timeout=5, check=True)
        official = json.loads(run.stdout)
        query = self.session.calls[1][1]
        self.assertEqual({key: query[key] for key in official}, official)

    def test_auth_account_mismatch_stops_before_any_order_api(self):
        self.session.user_phone = '13900000001'
        with self.assertRaises(CouponCashierError) as error:
            self.client.continue_payment(ORDER, '30')
        self.assertEqual(error.exception.code, 'ACCOUNT_MISMATCH')
        self.assertEqual(len(self.session.calls), 1)
        self.assertTrue(self.session.closed)

    def test_missing_order_or_order_account_mismatch_never_continues_payment(self):
        self.session.record['orderId'] = 'another-order'
        with self.assertRaises(CouponCashierError) as error:
            self.client.continue_payment(ORDER, '30')
        self.assertEqual(error.exception.code, 'CASHIER_ORDER_NOT_FOUND')
        self.assertEqual(len(self.session.calls), 2)
        self.session.record['orderId'] = ORDER
        self.session.detail['rechargeNum'] = '13900000001'
        with self.assertRaises(CouponCashierError) as error:
            self.client.continue_payment(ORDER, '30')
        self.assertEqual(error.exception.code, 'CASHIER_ORDER_MISMATCH')
        self.assertFalse(any(url.endswith('/orderGoPay') for url, *_ in self.session.calls))

    def test_paid_non_recharge_or_different_amount_orders_never_enter_cashier(self):
        for changes, code in [({'status': '10'}, 'CASHIER_ORDER_NOT_PAYABLE'),
                              ({'orderType': '003'}, 'CASHIER_ORDER_NOT_PAYABLE'),
                              ({'chargeChoseFee': '31'}, 'CASHIER_AMOUNT_MISMATCH')]:
            with self.subTest(changes=changes):
                self.session.detail = {**self.session.detail, 'status': '17', 'orderType': '004',
                                       'chargeChoseFee': '30', **changes}
                with self.assertRaises(CouponCashierError) as error:
                    self.client.continue_payment(ORDER, '30')
                self.assertEqual(error.exception.code, code)
        self.assertFalse(any(url.endswith('/orderGoPay') for url, *_ in self.session.calls))

    def test_stale_credentials_abort_and_upstream_failures_do_not_expose_credentials(self):
        self.session.after_request = lambda: self.snapshot.update(credential_revision=2)
        with self.assertRaises(CouponCashierError) as error:
            self.client.inspect_order(ORDER, '30')
        self.assertEqual(error.exception.code, 'APP_CREDENTIALS_CHANGED')
        self.assertEqual(len(self.session.calls), 1)
        self.snapshot['credential_revision'] = 1
        self.session.after_request = None
        self.session.reject = '400007'
        with self.assertRaises(CouponCashierError) as error:
            self.client.inspect_order(ORDER, '30')
        self.assertEqual(error.exception.safe['returnCode'], '400007')
        self.assertNotIn('private', str(error.exception))
        self.session.reject = ''
        self.session.error = requests.RequestsError('private account URL')
        with self.assertRaises(CouponCashierError) as error:
            self.client.inspect_order(ORDER, '30')
        self.assertEqual(error.exception.code, 'CASHIER_NETWORK')
        self.assertNotIn('private', str(error.exception))

    def test_refuses_non_web_or_missing_payment_links(self):
        for url in ('', None, 'javascript:alert(1)', 'http://pay.shop.10086.cn/',
                    'https://user:password@pay.shop.10086.cn/', 'https://@pay.shop.10086.cn/',
                    'https://pay.shop.10086.cn.example.com/', 'https://unconfirmed.example/',
                    'https://other.cmpay.com/', 'https://sub.cashier.cmpay.com/',
                    'https://cashier.cmpay.com.example.com/'):
            with self.subTest(url=url):
                self.session.pay_url = url
                with self.assertRaises(CouponCashierError) as error:
                    self.client.continue_payment(ORDER, '30')
                self.assertEqual(error.exception.code, 'CASHIER_LINK_UNAVAILABLE')

    def test_preserves_official_returned_cmpay_cashier_url_without_widening_domain_scope(self):
        self.session.pay_url = 'https://cashier.cmpay.com/api/wap/v1/cashier/unionWapDir?token=virtual-official-ticket'
        result = self.client.continue_payment(ORDER, '30')
        self.assertEqual(result['payment_url'], self.session.pay_url)
        self.assertEqual(result['amount'], '30.00')

    def test_request_crypto_and_sign_match_the_official_h4_function_offline(self):
        sources = Path(__file__).resolve().parents[1]/'analysis'/'coupons-cache'
        inner = sources/'reapp-recharge_H5module_inner.js'
        common = sources/'order-chunk-common.js'
        if not shutil.which('node') or not inner.is_file() or not common.is_file():
            self.skipTest('Retained official source and Node are required')
        flow = SimpleNamespace(client=self.session, current=lambda: None, snapshot=SNAPSHOT,
                               xk='11111111-2222-3333-4444-555555555555', bearer='')
        with patch('coupon_cashier_h5.time.time', return_value=1700000000), patch(
                'coupon_cashier_h5.secrets.choice', side_effect=lambda alphabet: '1' if len(alphabet) == 10 else '6'):
            self.client._request(flow, AUTH_PATH, {}, auth=True)
        script = r"""
const vm = require('node:vm');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const ctx = {window: {}, document: {scripts: []}, navigator: {userAgent: ''}};
vm.createContext(ctx);
vm.runInContext(input.inner, ctx, {timeout: 1000});
ctx._0x33810a = () => ctx.window.recharge_H5module_inner.CryptoJS;
ctx.input = input;
const start = input.common.indexOf('var _0x244f45=');
const end = input.common.indexOf('},0x13624:function', start);
if (start < 0 || end < 0) throw Error('Official H4 source not found');
vm.runInContext(input.common.slice(start, end), ctx, {timeout: 1000});
vm.runInContext(`
  var seed=input.xk+'_'+input.path+'_1700000000000_11111111';
  var body=_0xe8972f(JSON.stringify({appToken:input.cookie}), {urlSafe:true});
  var headerToken=_0xe8972f(seed);
  var sign=_0x33810a().MD5(_0xe8972f(seed,{urlSafe:true})+'_1700000000000_11111111_null').toString();
`, ctx);
process.stdout.write(JSON.stringify({body:ctx.body, xToken:ctx.headerToken, sign:ctx.sign}));
"""
        run = subprocess.run(['node', '-e', script], input=json.dumps({
            'inner': inner.read_text(encoding='utf-8'), 'common': common.read_text(encoding='utf-8'),
            'xk': flow.xk, 'path': AUTH_PATH, 'cookie': SNAPSHOT['cookie']}, ensure_ascii=False),
            capture_output=True, text=True, encoding='utf-8', timeout=5, check=True)
        official = json.loads(run.stdout)
        _, sent_body, headers = self.session.calls[0]
        self.assertEqual(official['body'], _encrypt(json.dumps(sent_body, ensure_ascii=False, separators=(',', ':'))))
        self.assertEqual(official['xToken'], headers['x-token'])
        self.assertEqual(official['sign'], headers['x-sign'])


if __name__ == '__main__':
    unittest.main()
