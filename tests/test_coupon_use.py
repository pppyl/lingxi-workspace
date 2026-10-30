import base64
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import hmac
import json
import logging
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from curl_cffi import requests

from coupon_client import CouponError, GCM_KEY, _cbc_decrypt, _cbc_encrypt, _item
from coupon_use import CouponUseError, CouponUseManager


PHONE = '13800000000'
LOGIN = 'account-fixture'
SNAPSHOT = {'phone': PHONE, 'cookie': 'JSESSIONID=private-session; UID=private_app_uid',
            'profile': {'imei': 'fake-imei'}, 'credential_revision': 1}
ROW = {'pCardNo': 'private-exchange-code', 'pcardId': 'fixture', 'stallName': '2元话费兑换券',
       'busiType': 'hf01', 'pCardType': '004', 'cardVersion': '2', 'smsCheck': '1',
       'useChannel': '11|', 'timeFlag': '1', 'couponStatus': '8', 'couponValue': '2',
       'valueFormat': '2', 'expireTime': '20261130235959', 'stallInfo': '先领券再充值'}


def encoded(data, *, gcm=False):
    text = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
    if gcm:
        nonce = b'123456789abc'
        inner = base64.b64encode(nonce + AESGCM(GCM_KEY).encrypt(nonce, text.encode(), None))
    else:
        inner = _cbc_encrypt(text).encode()
    return {'retCode': '000000', 'data': {'outParam': base64.b64encode(inner).decode()}}


class Storage:
    def __init__(self, data=None):
        self.data, self.writes, self.fail = deepcopy(data or {}), [], False

    def load_coupon_use_data(self):
        return deepcopy(self.data)

    def save_coupon_use_data(self, data):
        if self.fail:
            raise OSError('fixture database unavailable')
        self.data = deepcopy(data)
        self.writes.append(deepcopy(data))


class Response:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status

    def json(self):
        return self.data


class Environment:
    def __init__(self, storage):
        self.storage, self.calls, self.sessions, self.sdk_calls = storage, [], [], []
        self.snapshot, self.row = deepcopy(SNAPSHOT), deepcopy(ROW)
        self.rule = {'operateId': 'official-operation', 'saleType': '7', 'saleCode': '30~1000:6'}
        self.amount_rule = {'minAmount': '10', 'maxAmount': '1000'}
        self.sms_response = {'serialNo': 'private-sms-serial', 'md5': 'private-sms-md5'}
        self.wallet_init = {'msgId': base64.b64encode(_cbc_encrypt('fixture-message-id').encode()).decode()}
        self.detail = {'smsCheck': '1', 'activityId': 'private-detail-activity'}
        self.final_response, self.final_error, self.before_final = None, None, None
        self.sdk_token, self.sdk_status = 'official-risk-fixture', 1

    def provider(self, login_id):
        if login_id != LOGIN:
            raise CouponUseError('账号不存在。', 'LOGIN_MISSING', 404)
        return deepcopy(self.snapshot)

    def client(self, provider, *, entry=None):
        env = self
        env.wallet_entry = entry
        class FakeClient:
            @contextmanager
            def _session(self):
                snapshot = provider()
                session = Session(env)
                env.sessions.append(session)
                def current():
                    if provider() != snapshot:
                        raise CouponError('凭据已更新。', 'APP_CREDENTIALS_CHANGED', 409)
                try:
                    yield session, {'number': snapshot['phone'], 'province': '851', 'login_type': '0',
                        'msg_id': base64.b64encode(_cbc_encrypt('fixture-message-id').encode()).decode()}, current
                finally:
                    session.closed = True
        return FakeClient()

    def recharge(self):
        self.row.update(pCardNo='private-recharge-code', pCardType='001', smsCheck='0',
                        couponValue='6', valueFormat='6', stallName='6元话费加赠券')

    def rights(self):
        self.row.update(busiType='qy01', provPcardType='ll01', pCardType='004', smsCheck=None,
            stallName='100MB流量日包券', couponValue='100', valueFormat='100', unitFormat='MB',
            activityId='private-list-activity', stallId='private-list-goods', marketCode='private-market')

    @property
    def coupon_id(self):
        return _item(self.row)['id']

    @property
    def final_calls(self):
        return [call for call in self.calls if '/saveorder' in call[0] or '/rightsCouponsUse/' in call[0]]


class Session:
    def __init__(self, env):
        self.env, self.headers, self.closed = env, {}, False
        self.cookies = requests.Cookies()

    def request(self, method, url, **options):
        self.env.sdk_calls.append((method, url, deepcopy(self.headers),
            {cookie.name: cookie.value for cookie in self.cookies.jar}, options))
        return Response({'status': self.env.sdk_status, 'data': self.env.sdk_token})

    def post(self, url, *, json, **options):
        if 'inParam' in json:
            if any(path in url for path in ('/pcardAndGiftQry/', '/pcardInfoQry/')):
                blob = base64.b64decode(base64.b64decode(json['inParam']))
                body = globals()['json'].loads(AESGCM(GCM_KEY).decrypt(blob[:12], blob[12:], None))
            else:
                body = globals()['json'].loads(_cbc_decrypt(base64.b64decode(json['inParam'])))
        else:
            body = deepcopy(json)
        self.env.calls.append((url, body, deepcopy(json), options))
        if '/pcardAndGiftQry/' in url:
            return Response(encoded({'busiData': [{'batchInfo': [deepcopy(self.env.row)]}]}, gcm=True))
        if '/pcardInfoQry/' in url:
            return Response(encoded(self.env.detail, gcm=True))
        if '/commonSms/' in url:
            return Response(encoded({}))
        if '/ruleinfo/' in url:
            return Response(encoded(self.env.rule))
        if '/payrule/' in url:
            return Response(encoded({'amountRule': self.env.amount_rule}))
        if '/initMessage?wel=' in url:
            return Response(encoded({}))
        if '/initMessage?' in url:
            return Response(encoded(self.env.wallet_init))
        if '/pcardCheck/' in url or '/pcardvalid/' in url:
            return Response(encoded({}))
        if '/pcardsms/' in url:
            return Response(encoded(self.env.sms_response))
        if '/saveorder' in url or '/rightsCouponsUse/' in url:
            if self.env.before_final:
                self.env.before_final()
            if self.env.final_error:
                raise self.env.final_error
            response = self.env.final_response
            return Response(response if response is not None else encoded({'orderId': 'real-fixture-order'}))
        raise AssertionError('Unexpected endpoint '+url)


class CouponUseTests(unittest.TestCase):
    def setUp(self):
        logger = logging.getLogger('coupon_use')
        handler = logging.NullHandler()
        logger.addHandler(handler)
        previous_propagate = logger.propagate
        logger.propagate = False
        self.addCleanup(logger.removeHandler, handler)
        self.addCleanup(setattr, logger, 'propagate', previous_propagate)
        self.storage = Storage()
        self.env = Environment(self.storage)
        self.factory = patch('coupon_use.CouponClient', side_effect=self.env.client)
        self.factory.start()
        self.addCleanup(self.factory.stop)
        self.manager = CouponUseManager(self.env.provider, self.storage)
        self.addCleanup(self.close_sessions)

    def close_sessions(self):
        for operation in self.manager.operations.values():
            self.manager._close(operation)

    def prepare(self):
        return self.manager.prepare(LOGIN, self.env.coupon_id)

    def confirm(self, *args, **kwargs):
        kwargs.setdefault('risk_token', 'official-risk-fixture')
        kwargs.setdefault('risk_lid', 'official-lid-fixture')
        operation = self.manager.operations.get(args[0])
        if operation and operation.kind in ('exchange', 'rights') and operation.result is None and not kwargs.get('prepare_risk'):
            self.env.sdk_token = kwargs['risk_token']
            self.manager.confirm(args[0], sms_code=kwargs.get('sms_code', ''), prepare_risk=True)
            self.manager.wallet_sdk(args[0], 'GET', '',
                {'User-Agent': kwargs.get('browser_user_agent') or 'Fixture browser SDK'}, None, kwargs['risk_lid'])
        return self.manager.confirm(*args, **kwargs)

    def exchange(self):
        preview = self.prepare()
        self.manager.sms(preview['operation_id'])
        return self.confirm(preview['operation_id'], sms_code='123456')

    def test_prepare_is_read_only_and_never_exposes_redemption_credentials(self):
        preview = self.prepare()
        self.assertEqual((preview['kind'], preview['requires_sms'], preview['account_masked']),
                         ('exchange', True, '138****0000'))
        self.assertEqual(len(self.env.calls), 3)
        self.assertTrue(all(any(path in call[0] for path in ('/pcardAndGiftQry/','/ruleinfo/','/initMessage?')) for call in self.env.calls))
        self.assertEqual(self.storage.data, {})
        for secret in [PHONE, 'private-exchange-code', 'private-session', 'official-operation']:
            self.assertNotIn(secret, json.dumps(preview))

    def test_equity_flow_coupon_uses_detail_sms_requirement_and_native_sms_template(self):
        self.env.rights()
        item = _item(self.env.row)
        self.assertEqual((item['category'], item['use_kind']), ('flow', 'rights'))
        preview = self.prepare()
        self.assertEqual((preview['kind'], preview['requires_sms'], preview['coupon_unit']), ('rights', True, 'MB'))
        detail_call = next(call for call in self.env.calls if '/pcardInfoQry/' in call[0])
        phone_path = base64.b64encode(_cbc_encrypt(PHONE).encode()).decode()
        self.assertTrue(detail_call[0].endswith('/'+phone_path))
        self.assertEqual(detail_call[1], {'loginNo': PHONE, 'channelId': '11', 'batchId': '',
            'loginType': '0', 'pcardPasswd': 'private-exchange-code', 'pcardType': '004', 'provincode': '851'})
        self.assertFalse(any('/ruleinfo/' in call[0] for call in self.env.calls))
        self.manager.sms(preview['operation_id'])
        sms = next(call for call in self.env.calls if '/commonSms/' in call[0])
        self.assertTrue(sms[0].endswith('/'+phone_path))
        self.assertEqual(sms[1], {'phoneNo': PHONE, 'msgType': '05', 'pcardName': '', 'pcardNo': '',
            'busiCode': '', 'busiName': '', 'busiActId': 'private-detail-activity',
            'provPcardId': 'private-list-goods', 'ifProvSendMsg': '1', 'useChannelId': '11', 'marketCode': 'private-market'})
        self.assertTrue(sms[2].get('msgId'))
        self.assertEqual(self.env.final_calls, [])
        self.assertNotIn('private-', json.dumps(preview))

    def test_equity_flow_exchange_keeps_list_and_detail_lineage_without_fee_order_requests(self):
        self.env.rights()
        preview = self.prepare()
        self.manager.sms(preview['operation_id'])
        self.env.final_response = encoded({'twiceSmsVerify': True})
        result = self.confirm(preview['operation_id'], sms_code='123456')
        self.assertEqual(result['state'], 'accepted')
        self.assertIn('回复确认', result['message'])
        final, = self.env.final_calls
        phone_path = base64.b64encode(_cbc_encrypt(PHONE).encode()).decode()
        self.assertTrue(final[0].endswith('/rightsCouponsUse/'+phone_path))
        self.assertEqual(final[1], {'customerId': PHONE, 'loginType': '0', 'activityId': 'private-list-activity',
            'pcardPassword': 'private-exchange-code', 'busiType': 'qy01', 'pcardType': '004',
            'channelId': '11', 'goodsId': 'private-list-goods', 'smsCode': '123456'})
        self.assertTrue(final[2].get('msgId'))
        self.assertFalse(any('/pcardvalid/' in call[0] or '/pcardsms/' in call[0] or '/saveorder' in call[0] for call in self.env.calls))
        self.assertEqual(self.confirm(preview['operation_id'], sms_code='123456'), result)
        self.assertEqual(len(self.env.final_calls), 1)

    def test_equity_flow_without_sms_and_unknown_submission_does_not_repeat(self):
        self.env.rights()
        self.env.detail['smsCheck'] = '0'
        preview = self.prepare()
        self.assertFalse(preview['requires_sms'])
        self.env.final_error = requests.RequestsError('connection lost')
        result = self.confirm(preview['operation_id'])
        self.assertEqual(result['state'], 'unknown')
        self.assertEqual(self.confirm(preview['operation_id']), result)
        self.assertEqual(len(self.env.final_calls), 1)

    def test_equity_flow_agreement_and_missing_sms_context_are_not_submitted(self):
        self.env.rights()
        self.env.detail['agreementName'] = '官方协议'
        with self.assertRaises(CouponUseError) as error:
            self.prepare()
        self.assertEqual(error.exception.code, 'VERIFICATION_REQUIRED')
        self.env.detail.pop('agreementName')
        self.env.detail.pop('activityId')
        with self.assertRaises(CouponUseError) as error:
            self.prepare()
        self.assertEqual(error.exception.code, 'RULE_MISSING')
        self.assertEqual(self.env.final_calls, [])

    def test_unavailable_and_wrong_selection_never_request_rules(self):
        self.env.row['timeFlag'] = '2'
        with self.assertRaises(CouponUseError) as error:
            self.prepare()
        self.assertEqual(error.exception.code, 'COUPON_NOT_AVAILABLE')
        self.assertTrue(self.env.sessions[-1].closed)
        self.env.calls.clear()
        with self.assertRaises(CouponUseError):
            self.manager.prepare(LOGIN, 'a'*24)
        self.assertEqual(len(self.env.calls), 1)

    def test_sms_and_redemption_use_cbc_protocol_and_commit_before_post(self):
        def before_final():
            self.assertEqual(next(iter(self.storage.data.values()))['state'], 'submitted')
        self.env.before_final = before_final
        result = self.exchange()
        self.assertEqual(result['state'], 'accepted')
        sms = next(call for call in self.env.calls if '/pcardsms/' in call[0])
        valid = next(call for call in self.env.calls if '/pcardvalid/' in call[0])
        self.assertEqual(sms[1], {'phoneNo': PHONE, 'pcardName': ROW['stallName'], 'pcardAmount': '2',
                                 'pcardNo': ROW['pCardNo'], 'channel': '02'})
        self.assertEqual(valid[1], {'sms_code': '123456', 'serial_no': 'private-sms-serial', 'channel': '02'})
        final = self.env.final_calls[0]
        encrypted_phone = base64.b64encode(_cbc_encrypt(PHONE).encode()).decode()
        self.assertTrue(final[0].endswith('/saveorder/'+encrypted_phone+'?provinceId=851'))
        self.assertTrue(sms[0].endswith('/pcardsms/'+encrypted_phone))
        self.assertTrue(next(call[0] for call in self.env.calls if '/ruleinfo/' in call[0]).endswith('/ruleinfo/'+encrypted_phone))
        self.assertTrue(valid[0].endswith('/pcardvalid/'+encrypted_phone))
        for call in (sms, valid, final):
            self.assertEqual(set(call[2]), {'inParam', 'msgId'})
            self.assertEqual(json.loads(_cbc_decrypt(base64.b64decode(call[2]['inParam']))), call[1])
            marker = _cbc_decrypt(base64.b64decode(call[2]['msgId']))
            self.assertTrue(marker.endswith('fixture-message-id'))
            self.assertTrue(marker.removesuffix('fixture-message-id').isdigit())
        self.assertEqual((final[1]['amount'], final[1]['chargeMoney'], final[1]['choseMoney']), (0, '2', '2'))
        self.assertEqual(final[1]['md5'], 'private-sms-md5')
        self.assertTrue(self.env.sessions[0].closed)
        self.assertEqual([next(iter(data.values()))['state'] for data in self.storage.writes], ['submitted', 'accepted'])

    def test_sms_is_explicit_and_required_code_cannot_be_skipped(self):
        preview = self.prepare()
        with self.assertRaises(CouponUseError) as error:
            self.confirm(preview['operation_id'], sms_code='123456')
        self.assertEqual(error.exception.code, 'SMS_REQUIRED')
        self.assertFalse(any('/pcardsms/' in call[0] for call in self.env.calls))

    def test_official_risk_token_is_session_cookie_only_and_not_persisted_or_in_body(self):
        preview = self.prepare()
        self.manager.sms(preview['operation_id'])
        token = '0001#official-sdk-fixture'
        result = self.confirm(preview['operation_id'], sms_code='123456', risk_token=token)
        self.assertEqual(result['state'], 'accepted')
        cookie = next(cookie for cookie in self.env.sessions[0].cookies.jar if cookie.name == 'stc-rrt-token')
        self.assertEqual((cookie.name, cookie.value, cookie.domain, cookie.path, cookie.secure),
            ('stc-rrt-token', token, 'touch.10086.cn', '/i', True))
        self.assertNotIn(token, json.dumps(self.env.final_calls))
        self.assertNotIn(token, json.dumps(self.storage.data))
        self.assertEqual(self.env.sessions[0].headers['X-Requested-With'], 'XMLHttpRequest')

    def test_successful_sms_resend_restarts_local_operation_lifetime(self):
        preview = self.prepare()
        operation = self.manager.operations[preview['operation_id']]
        operation.expires = 600
        with patch('coupon_use.time.monotonic', return_value=400):
            self.manager.sms(operation.id)
        self.assertEqual(operation.expires, 1000)
        with patch('coupon_use.time.monotonic', return_value=700):
            result = self.confirm(operation.id, sms_code='123456')
        self.assertEqual(result['state'], 'accepted')

    def test_live_sms_operation_defers_only_its_account_until_finished_or_expired(self):
        preview = self.prepare()
        operation = self.manager.operations[preview['operation_id']]
        self.assertTrue(self.manager.busy_login(LOGIN))
        self.assertFalse(self.manager.busy_login('another-account'))
        self.manager.sms(operation.id)
        self.assertTrue(self.manager.busy_login(LOGIN))
        self.assertEqual(self.confirm(operation.id, sms_code='123456')['state'], 'accepted')
        self.assertFalse(self.manager.busy_login(LOGIN))

        # A different coupon may start another session; an abandoned window
        # never defers renewal beyond the operation's existing ten-minute TTL.
        self.env.row['pCardNo'] = 'another-private-exchange-code'
        fresh = self.prepare()
        pending = self.manager.operations[fresh['operation_id']]
        self.assertTrue(self.manager.busy_login(LOGIN))
        with patch('coupon_use.time.monotonic', return_value=pending.expires):
            self.assertFalse(self.manager.busy_login(LOGIN))

    def test_exchange_validates_sms_before_sdk_and_keeps_validation_until_submission(self):
        preview = self.prepare()
        operation_id = preview['operation_id']
        self.manager.sms(operation_id)
        before = len(self.env.calls)
        ready = self.manager.confirm(operation_id, sms_code='123456', prepare_risk=True)
        self.assertEqual(ready['state'], 'risk_required')
        self.assertEqual(len(self.env.calls), before+1)
        self.assertIn('/pcardvalid/', self.env.calls[-1][0])
        self.assertEqual(self.env.final_calls, [])
        self.assertEqual(self.storage.data, {})
        result = self.confirm(operation_id, sms_code='123456', browser_user_agent='Browser SDK user agent')
        self.assertEqual(result['state'], 'accepted')
        self.assertEqual(sum('/pcardvalid/' in call[0] for call in self.env.calls), 1)
        self.assertIn('/saveorder/', self.env.calls[-1][0])
        self.assertEqual(self.env.sessions[0].headers['User-Agent'], 'Browser SDK user agent')

    def test_missing_sdk_result_does_not_submit_or_repeat_successful_sms_validation(self):
        preview = self.prepare()
        operation_id = preview['operation_id']
        self.manager.sms(operation_id)
        self.manager.confirm(operation_id, sms_code='123456', prepare_risk=True)
        with self.assertRaises(CouponUseError) as error:
            self.manager.confirm(operation_id, sms_code='123456')
        self.assertEqual(error.exception.code, 'RISK_INITIALIZATION_FAILED')
        self.assertEqual(self.env.final_calls, [])
        self.assertEqual(sum('/pcardvalid/' in call[0] for call in self.env.calls), 1)
        self.manager.sms(operation_id)
        self.manager.confirm(operation_id, sms_code='654321', prepare_risk=True)
        self.assertEqual(sum('/pcardvalid/' in call[0] for call in self.env.calls), 2)

    def test_sdk_request_reuses_wallet_login_cookie_and_restores_business_headers(self):
        preview = self.prepare()
        operation_id = preview['operation_id']
        operation = self.manager.operations[operation_id]
        client = operation.client
        client.cookies.set('jsessionid-cmcc', 'private-wallet-session', domain='touch.10086.cn', path='/')
        self.manager.wallet_sdk(operation_id, 'GET', '', {'User-Agent': 'Real Browser'}, None, 'sdk-lid')
        with self.assertRaises(CouponUseError) as error:
            self.manager.confirm(operation_id, sms_code='123456')
        self.assertEqual(error.exception.code, 'SMS_REQUIRED')
        self.assertEqual(self.env.final_calls, [])
        self.env.sdk_calls.clear()
        self.manager.sms(operation_id)
        ready = self.manager.confirm(operation_id, sms_code='123456', prepare_risk=True)
        self.assertEqual(ready['sdk_server'], '/coupon-verification/wallet/udid/c1?operation_id='+operation_id)
        saved_headers = dict(client.headers)
        headers = {'User-Agent': 'Real Browser', 'Origin': 'http://localhost:5500', 'param': 'sdk-opaque'}
        self.manager.wallet_sdk(operation_id, 'GET', 'appId=official', headers, None, 'sdk-lid')
        method, url, actual_headers, cookies, options = self.env.sdk_calls[0]
        self.assertEqual(method, 'GET')
        self.assertTrue(url.endswith('/i/mobile/tfk/mgt/decision-engine/udid/c1?appId=official'))
        self.assertEqual(actual_headers, headers)
        self.assertEqual(cookies['jsessionid-cmcc'], 'private-wallet-session')
        self.assertEqual(cookies['_zw_kvani5r'], 'sdk-lid')
        self.assertEqual(client.headers, saved_headers)
        result = self.manager.confirm(operation_id, sms_code='123456', risk_token=self.env.sdk_token, risk_lid='sdk-lid')
        self.assertEqual(result['state'], 'accepted')
        self.assertEqual(client.headers['User-Agent'], 'Real Browser')
        self.assertEqual(sum('/pcardvalid/' in call[0] for call in self.env.calls), 1)
        self.assertNotIn('private-wallet-session', json.dumps(self.storage.data))

    def test_final_cookie_requires_c1_data_matching_the_browser_callback(self):
        preview = self.prepare()
        operation_id = preview['operation_id']
        self.manager.sms(operation_id)
        self.manager.confirm(operation_id, sms_code='123456', prepare_risk=True)
        self.env.sdk_token = 'actual-c1-server-data'
        self.manager.wallet_sdk(operation_id, 'GET', '', {'User-Agent': 'Real Browser'}, None, 'sdk-lid')
        self.assertFalse(any(c.name == 'stc-rrt-token' for c in self.env.sessions[0].cookies.jar))
        with self.assertRaises(CouponUseError) as error:
            self.manager.confirm(operation_id, sms_code='123456', risk_token='different-callback-value', risk_lid='sdk-lid')
        self.assertEqual(error.exception.code, 'RISK_INITIALIZATION_FAILED')
        self.assertEqual(self.env.final_calls, [])
        result = self.manager.confirm(operation_id, sms_code='123456', risk_token='actual-c1-server-data', risk_lid='sdk-lid')
        self.assertEqual(result['state'], 'accepted')
        token = next(c.value for c in self.env.sessions[0].cookies.jar if c.name == 'stc-rrt-token')
        self.assertEqual(token, 'actual-c1-server-data')
        self.assertNotIn('actual-c1-server-data', json.dumps(self.storage.data))

    def test_http200_c1_rejection_never_uses_browser_fallback_token_to_submit(self):
        preview = self.prepare()
        operation_id = preview['operation_id']
        self.manager.sms(operation_id)
        self.manager.confirm(operation_id, sms_code='123456', prepare_risk=True)
        self.env.sdk_status = 0
        self.manager.wallet_sdk(operation_id, 'GET', '', {'User-Agent': 'Real Browser'}, None, 'sdk-lid')
        with self.assertRaises(CouponUseError) as error:
            self.manager.confirm(operation_id, sms_code='123456', risk_token='browser-fallback', risk_lid='sdk-lid')
        self.assertEqual(error.exception.code, 'RISK_INITIALIZATION_FAILED')
        self.assertEqual(self.env.final_calls, [])

    def test_official_sdk_status_two_is_success_and_uses_server_data(self):
        self.env.sdk_status = 2
        result = self.exchange()
        self.assertEqual(result['state'], 'accepted')
        token = next(c.value for c in self.env.sessions[0].cookies.jar if c.name == 'stc-rrt-token')
        self.assertEqual(token, self.env.sdk_token)

    def test_wallet_initialization_uses_plain_app_channel_and_replaces_old_loginfo_seed(self):
        self.env.wallet_init['msgId'] = base64.b64encode(_cbc_encrypt('rotated-native-seed').encode()).decode()
        preview = self.prepare()
        init = next(call for call in self.env.calls if '/initMessage?' in call[0])
        self.assertRegex(init[0], r'/initMessage\?\d{13}$')
        self.assertEqual(set(init[2]), {'data', 'channel'})
        self.assertEqual(init[2]['channel'], '12')
        assertion = json.loads(_cbc_decrypt(init[2]['data']))
        self.assertEqual(assertion['UID'], 'private_app_uid')
        self.assertEqual(assertion['sign'], hmac.new(b'', b'B3A128FAprivate_app_uidB3A128FA', hashlib.md5).hexdigest())
        operation = self.manager.operations[preview['operation_id']]
        self.assertEqual(operation.user['msg_id'], self.env.wallet_init['msgId'])
        self.manager.sms(preview['operation_id'])
        self.confirm(preview['operation_id'], sms_code='123456')
        for call in self.env.calls[self.env.calls.index(init)+1:]:
            marker = _cbc_decrypt(base64.b64decode(call[2]['msgId']))
            self.assertTrue(marker.endswith('rotated-native-seed'))
        final = self.env.final_calls[0][1]
        self.assertEqual(final['channel'], '11')
        for path in ('/pcardsms/', '/pcardvalid/'):
            self.assertEqual(next(call[1]['channel'] for call in self.env.calls if path in call[0]), '02')

    def test_wallet_missing_native_seed_stops_before_any_sms_or_exchange(self):
        self.env.wallet_init = {}
        with self.assertRaises(CouponUseError) as error:
            self.prepare()
        self.assertEqual(error.exception.code, 'WALLET_INIT_FAILED')
        self.assertFalse(any('/pcardsms/' in call[0] or '/saveorder/' in call[0] for call in self.env.calls))

    def test_exchange_preserves_native_app_attribution_from_shared_cookie(self):
        result = self.exchange()
        body = self.env.final_calls[0][1]
        self.assertEqual((body['wtAc'], body['wtAcId']), ('_', 'private-session'))
        self.assertEqual(body['smsCheck'], self.env.row['smsCheck'])
        self.assertNotIn('private-session', json.dumps(result))
        self.assertNotIn('private-session', json.dumps(self.storage.data))

    def test_native_attribution_uses_first_cookie_not_touch_session_or_random_token(self):
        self.env.snapshot['cookie'] = 'UID=native-first==; JSESSIONID=another-session'
        self.exchange()
        self.assertEqual(self.env.final_calls[0][1]['wtAcId'], 'native-first')

    def test_attribution_is_from_this_official_entry_and_keeps_account_session_identity(self):
        entry = 'https://touch.10086.cn/i/mobile/mycoupons.html?channelId=P12345678901&yx=fixture-entry'
        self.manager.wallet_entry = entry
        self.exchange()
        self.assertEqual(self.env.wallet_entry, entry)
        final = self.env.final_calls[-1][1]
        self.assertEqual(final['wtAc'], 'P12345678901_fixture-entry')
        self.assertEqual(final['wtAcId'], 'private-session')
        self.assertNotIn('private-session', json.dumps(self.storage.data))

    def test_500002_preserves_official_reason_but_redacts_submission_secrets(self):
        self.env.final_response = {'retCode': '500002',
            'retMsg': '参数错误 '+PHONE+' private-exchange-code private-session 123456 private-sms-serial'}
        with self.assertLogs('coupon_use', level='WARNING') as diagnostic:
            result = self.exchange()
        self.assertIn('官方说明：参数错误', result['message'])
        for secret in (PHONE, 'private-exchange-code', 'private-session', '123456', 'private-sms-serial'):
            self.assertNotIn(secret, result['message'])
            self.assertNotIn(secret, diagnostic.output[0])
        for secret in ('official-risk-fixture', 'official-lid-fixture', 'private-sms-md5'):
            self.assertNotIn(secret, diagnostic.output[0])
        facts = json.loads(next(line for line in diagnostic.output if 'coupon_exchange_500002 ' in line)
                           .split('coupon_exchange_500002 ', 1)[1])
        self.assertEqual(facts['fields']['operateId']['type'], 'str')
        self.assertTrue(facts['sms_verified'])
        self.assertGreater(facts['risk_cookie_lengths']['stc-rrt-token'], 0)

    def test_invalid_risk_token_stops_before_any_upstream_request(self):
        for token in (None, {}, 'x'*8193, 'x\r\ny', 'x;y'):
            with self.subTest(token_type=type(token).__name__), self.assertRaises(CouponUseError) as error:
                self.confirm('unused', risk_token=token)
            self.assertEqual(error.exception.code, 'INVALID_RISK_TOKEN')
        self.assertEqual(self.env.calls, [])
        self.assertEqual(self.env.final_calls, [])

    def test_sdk_lid_cookie_is_forwarded_to_wallet_only_and_never_stored(self):
        preview = self.prepare()
        self.manager.sms(preview['operation_id'])
        token, lid = 'official-risk-result', 'official-sdk-lid-value'
        self.confirm(preview['operation_id'], sms_code='123456', risk_token=token, risk_lid=lid)
        cookies = {cookie.name: cookie for cookie in self.env.sessions[0].cookies.jar}
        self.assertEqual(set(cookies), {'stc-rrt-token', '_zw_kvani5r'})
        self.assertEqual(cookies['_zw_kvani5r'].value, lid)
        self.assertEqual(cookies['_zw_kvani5r'].domain, 'touch.10086.cn')
        self.assertEqual(cookies['_zw_kvani5r'].path, '/')
        self.assertFalse(cookies['_zw_kvani5r'].secure)
        self.assertNotIn(lid, json.dumps(self.storage.data))
        self.assertNotIn(lid, json.dumps(self.env.final_calls))

    def test_sms_without_optional_md5_can_be_verified_and_does_not_invent_a_digest(self):
        self.env.sms_response = {'serialNo': 'private-sms-serial'}
        result = self.exchange()
        self.assertEqual(result['state'], 'accepted')
        self.assertNotIn('md5', self.env.final_calls[0][1])

    def test_missing_sms_serial_never_submits_exchange(self):
        self.env.sms_response = {'md5': 'private-sms-md5'}
        preview = self.prepare()
        with self.assertRaises(CouponUseError) as error:
            self.manager.sms(preview['operation_id'])
        self.assertEqual(error.exception.code, 'SMS_FORMAT')
        self.assertEqual(self.env.final_calls, [])

    def test_full_reduction_coupon_has_recharge_entry_and_submits_actual_payment(self):
        self.env.recharge()
        self.env.row.update(pCardType='005', stallName='2元话费满减券', couponValue='2', valueFormat='2')
        self.env.rule.update(saleType=10, saleCode='20~5000:2')
        self.env.amount_rule = {'minAmount': 1, 'maxAmount': 5000}
        self.assertEqual(_item(self.env.row)['use_kind'], 'recharge')
        preview = self.prepare()
        self.assertIn('实付 18 元', preview['choices'][0]['label'])
        self.confirm(preview['operation_id'], charge_amount='20')
        body = json.loads(base64.b64decode(self.env.final_calls[0][1]['param']))
        self.assertEqual((body['amount'], body['chargeMoney'], body['choseMoney']), (18, '20.00', '20'))
        self.assertNotIn('newAmount', body)
        self.assertEqual(next(iter(self.storage.data.values()))['charge_amount'], '18.00')

    def test_special_reduction_preserves_official_amount_and_separate_net_fields(self):
        self.env.recharge()
        self.env.row.update(pCardType='005', stallName='2元话费立减券')
        self.env.rule.update(saleType=15, saleCode='20~1000:2')
        preview = self.prepare()
        self.confirm(preview['operation_id'], charge_amount='20')
        body = json.loads(base64.b64decode(self.env.final_calls[0][1]['param']))
        self.assertEqual((body['amount'], body['newAmount'], body['jkCardFee']), ('20.00', '18.00', '2.00'))

    def test_small_exchange_without_sms_uses_official_no_sms_branch(self):
        self.env.row['smsCheck'] = '0'
        preview = self.prepare()
        self.assertFalse(preview['requires_sms'])
        self.assertEqual(self.confirm(preview['operation_id'])['state'], 'accepted')
        self.assertFalse(any('/pcardsms/' in call[0] or '/pcardvalid/' in call[0] for call in self.env.calls))
        self.assertNotIn('smsCheck', self.env.final_calls[0][1])

    def test_duplicate_confirm_and_second_prepared_operation_do_not_submit_twice(self):
        self.env.row['smsCheck'] = '0'
        first, second = self.prepare(), self.prepare()
        result = self.confirm(first['operation_id'])
        self.assertEqual(self.confirm(first['operation_id']), result)
        with self.assertRaises(CouponUseError) as error:
            self.confirm(second['operation_id'])
        self.assertEqual(error.exception.code, 'ALREADY_SUBMITTED')
        with self.assertRaises(CouponUseError):
            self.prepare()
        self.assertEqual(len(self.env.final_calls), 1)

    def test_persistence_contains_no_account_sms_session_or_coupon_secrets(self):
        self.exchange()
        persisted = json.dumps(self.storage.data)
        for secret in [PHONE, LOGIN, self.env.coupon_id, 'private-exchange-code', '123456',
                       'private-session', 'private-sms', 'fake-imei']:
            self.assertNotIn(secret, persisted)
        self.assertEqual(len(next(iter(self.storage.data))), 64)

    def test_recovery_of_submitted_state_blocks_replay_after_restart(self):
        key = hashlib.sha256(json.dumps([LOGIN, self.env.coupon_id], ensure_ascii=False,
                                       separators=(',', ':')).encode()).hexdigest()
        self.storage.data = {key: {'state': 'submitted', 'operation_id': 'interrupted', 'result': None}}
        restarted = CouponUseManager(self.env.provider, self.storage)
        self.assertEqual(self.storage.data[key]['state'], 'unknown')
        with self.assertRaises(CouponUseError):
            restarted.prepare(LOGIN, self.env.coupon_id)
        self.assertEqual(self.env.calls, [])

    def test_timeout_after_submission_is_unknown_and_cannot_be_retried(self):
        self.env.final_error = requests.RequestsError('private URL and coupon code')
        result = self.exchange()
        self.assertEqual(result['state'], 'unknown')
        self.assertNotIn('private', result['message'])
        with self.assertRaises(CouponUseError):
            self.prepare()
        self.assertEqual(len(self.env.final_calls), 1)

    def test_malformed_success_and_missing_result_code_are_unknown_after_post(self):
        for response in [{'retCode': '000000', 'data': []}, {'data': {}},
                         {'retCode': '000000', 'data': {'outParam': 'invalid'}}]:
            with self.subTest(response=response):
                self.storage.data = {}
                self.manager.data = {}
                self.env.final_response = response
                self.assertEqual(self.exchange()['state'], 'unknown')

    def test_explicit_official_rejection_is_retryable_but_not_silently_retried(self):
        self.env.final_response = {'retCode': '543007'}
        self.assertEqual(self.exchange()['state'], 'failed')
        self.assertEqual(len(self.env.final_calls), 1)
        self.assertIn('operation_id', self.prepare())

    def test_database_failure_before_submission_prevents_upstream_order(self):
        preview = self.prepare()
        self.manager.sms(preview['operation_id'])
        self.storage.fail = True
        with self.assertRaises(OSError):
            self.confirm(preview['operation_id'], sms_code='123456')
        self.assertEqual(self.env.final_calls, [])

    def test_rotated_credentials_expired_preview_or_changed_coupon_stop_before_submission(self):
        preview = self.prepare()
        self.env.snapshot['credential_revision'] = 2
        with self.assertRaises(CouponError) as error:
            self.manager.sms(preview['operation_id'])
        self.assertEqual(error.exception.code, 'APP_CREDENTIALS_CHANGED')
        self.env.snapshot['credential_revision'] = 1
        self.manager.operations[preview['operation_id']].expires = 0
        with self.assertRaises(CouponUseError) as error:
            self.manager.sms(preview['operation_id'])
        self.assertEqual(error.exception.code, 'OPERATION_EXPIRED')
        self.env.recharge()
        fresh = self.prepare()
        self.env.row['couponValue'] = '3'
        with self.assertRaises(CouponUseError) as error:
            self.confirm(fresh['operation_id'], charge_amount='30')
        self.assertEqual(error.exception.code, 'COUPON_CHANGED')
        self.assertEqual(self.env.final_calls, [])

    def test_recharge_preview_uses_rule_bands_and_sends_no_sms_or_orders(self):
        self.env.recharge()
        self.env.rule['saleCode'] = '30~50:6,100~200:8'
        preview = self.prepare()
        self.assertEqual(preview['kind'], 'recharge')
        self.assertFalse(preview['requires_sms'])
        self.assertEqual([choice['value'] for choice in preview['choices']], ['30'])
        self.assertIn('到账 36 元', preview['choices'][0]['label'])
        check = next(call for call in self.env.calls if '/pcardCheck/' in call[0])
        self.assertEqual(check[1], {'phoneNo': PHONE, 'busiType': 'hf01', 'pcardNo': self.env.row['pCardNo'], 'channel': '11'})
        self.assertIn('inParam', check[2])
        self.assertEqual(self.env.final_calls, [])

    def test_recharge_order_uses_signed_30_paid_36_credit_and_returns_manual_payment(self):
        self.env.recharge()
        preview = self.prepare()
        result = self.confirm(preview['operation_id'], charge_amount='30')
        self.assertEqual((result['state'], result['order_id']), ('payment_required', 'real-fixture-order'))
        self.assertNotIn('payment_url', result)
        final = self.env.final_calls[0]
        encrypted_phone = base64.b64encode(_cbc_encrypt(PHONE).encode()).decode()
        self.assertTrue(final[0].endswith('/saveorderh5/'+encrypted_phone))
        self.assertIn('inParam', final[2])
        text = base64.b64decode(final[1]['param']).decode()
        body = json.loads(text)
        self.assertEqual((body['amount'], body['chargeMoney'], body['choseMoney']), (30, '36.00', '30'))
        self.assertEqual((body['channel'], body['payWay'], body['uid'], body['appUid'], body['appImei']),
                         ('11', 'SDK', 'private_app_uid', 'private_app_uid', 'fake-imei'))
        self.assertEqual(body['numFlag'], '1')
        signature = hmac.new(b'kk9n54Vh2Bc8aXChLHYPTQ==', ('123456'+text+'123456').encode(), hashlib.md5).hexdigest()
        self.assertEqual(final[1]['payhmcc'], signature)
        self.assertEqual(next(iter(self.storage.data.values()))['order_id'], 'real-fixture-order')

    def test_recharge_rejects_arbitrary_amount_and_changed_rule_before_order(self):
        self.env.recharge()
        preview = self.prepare()
        with self.assertRaises(CouponUseError) as error:
            self.confirm(preview['operation_id'], charge_amount='24')
        self.assertEqual(error.exception.code, 'INVALID_AMOUNT')
        self.env.rule['saleCode'] = '30~1000:5'
        with self.assertRaises(CouponUseError) as error:
            self.confirm(preview['operation_id'], charge_amount='30')
        self.assertEqual(error.exception.code, 'RULE_CHANGED')
        self.assertEqual(self.env.final_calls, [])

    def test_unimplemented_recharge_rule_or_second_verification_is_not_guessed(self):
        self.env.recharge()
        self.env.rule['saleType'] = '9'
        with self.assertRaises(CouponUseError) as error:
            self.prepare()
        self.assertEqual(error.exception.code, 'RULE_UNSUPPORTED')
        self.env.rule['saleType'] = '7'
        self.env.row['smsCheck'] = '1'
        with self.assertRaises(CouponUseError) as error:
            self.prepare()
        self.assertEqual(error.exception.code, 'VERIFICATION_REQUIRED')
        self.assertEqual(self.env.final_calls, [])

    def test_recharge_success_without_order_number_is_unknown(self):
        self.env.recharge()
        self.env.final_response = encoded({})
        preview = self.prepare()
        self.assertEqual(self.confirm(preview['operation_id'], charge_amount='30')['state'], 'unknown')
        self.assertEqual(len(self.env.final_calls), 1)

    def test_official_captcha_challenge_resumes_same_session_only_after_user_token(self):
        self.env.recharge()
        preview = self.prepare()
        operation_id = preview['operation_id']
        self.env.final_response = {'retCode': '531027', 'retMsg': 'untrusted upstream account details'}
        result = self.confirm(operation_id, charge_amount='30')
        self.assertEqual((result['state'], result['verification']), ('verification_required', 'captcha'))
        self.assertNotIn('untrusted', result['message'])
        self.assertFalse(self.env.sessions[0].closed)
        self.assertIsNone(self.manager.operations[operation_id].result)
        self.assertEqual(next(iter(self.storage.data.values()))['state'], 'failed')
        calls_before_retry = len(self.env.calls)
        with self.assertRaises(CouponUseError) as error:
            self.confirm(operation_id, charge_amount='30')
        self.assertEqual(error.exception.code, 'CAPTCHA_REQUIRED')
        self.assertEqual(len(self.env.calls), calls_before_retry)

        token = 'private-user-completed-official-captcha-token'
        self.env.final_response = None
        paid = self.confirm(operation_id, charge_amount='30', captcha_token=token)
        self.assertEqual(paid['state'], 'payment_required')
        self.assertEqual(len(self.env.sessions), 1)
        self.assertTrue(self.env.sessions[0].closed)
        self.assertEqual(sum('/initMessage' in call[0] for call in self.env.calls), 1)
        second_body = json.loads(base64.b64decode(self.env.final_calls[1][1]['param']))
        self.assertEqual(second_body['fkToken'], token)
        self.assertEqual((second_body['amount'], second_body['chargeMoney']), (30, '36.00'))
        self.assertNotIn(token, json.dumps(self.storage.data))
        self.assertNotIn(token, json.dumps(paid))
        self.assertEqual(self.confirm(operation_id, charge_amount='30', captcha_token=token), paid)
        resumed = self.prepare()
        self.assertEqual(resumed['state'], 'payment_required')
        self.assertEqual(resumed['order']['order_id'], paid['order_id'])
        self.assertEqual(len(self.env.final_calls), 2)

    def test_environment_rejection_is_explicit_and_does_not_offer_payment_or_auto_retry(self):
        self.env.recharge()
        preview = self.prepare()
        self.env.final_response = {'retCode': '531022'}
        result = self.confirm(preview['operation_id'], charge_amount='30')
        self.assertEqual((result['state'], result['code']), ('failed', '531022'))
        self.assertIn('充值环境异常', result['message'])
        self.assertNotIn('order_id', result)
        self.assertNotIn('payment_url', result)
        self.assertTrue(self.env.sessions[0].closed)
        self.assertEqual(self.confirm(preview['operation_id'], charge_amount='30'), result)
        self.assertEqual(len(self.env.final_calls), 1)
        self.assertFalse(self.manager.busy_login(LOGIN))

        self.env.snapshot['credential_revision'] += 1
        self.env.final_response = None
        fresh = self.prepare()
        self.assertNotEqual(fresh['operation_id'], preview['operation_id'])
        self.assertEqual(len(self.env.final_calls), 1, 'Preparing after login does not submit an order')
        self.assertEqual(self.confirm(fresh['operation_id'], charge_amount='30')['state'], 'payment_required')
        self.assertEqual(len(self.env.final_calls), 2)

    def test_only_explicit_captcha_codes_keep_session_open_for_another_user_confirmation(self):
        self.env.recharge()
        preview = self.prepare()
        self.env.final_response = {'retCode': '531023'}
        first = self.confirm(preview['operation_id'], charge_amount='30')
        self.assertEqual(first['state'], 'verification_required')
        self.env.final_response = {'retCode': '531034'}
        failed = self.confirm(preview['operation_id'], charge_amount='30', captcha_token='official-token')
        self.assertEqual(failed['state'], 'failed')
        self.assertTrue(self.env.sessions[0].closed)
        self.assertEqual(self.confirm(preview['operation_id'], charge_amount='30',
                                             captcha_token='another-token'), failed)
        self.assertEqual(len(self.env.final_calls), 2)

    def test_captcha_token_cannot_retry_unknown_submission_and_has_bounded_string_input(self):
        self.env.recharge()
        preview = self.prepare()
        for token in (None, 123, {}, 'x'*4097):
            with self.subTest(type=type(token).__name__):
                with self.assertRaises(CouponUseError) as error:
                    self.confirm(preview['operation_id'], charge_amount='30', captcha_token=token)
                self.assertEqual(error.exception.code, 'INVALID_CAPTCHA_TOKEN')
        self.assertEqual(self.env.final_calls, [])
        self.env.final_error = requests.RequestsError('connection lost')
        unknown = self.confirm(preview['operation_id'], charge_amount='30')
        self.assertEqual(unknown['state'], 'unknown')
        self.assertEqual(self.confirm(preview['operation_id'], charge_amount='30',
                                             captcha_token='official-token'), unknown)
        self.assertEqual(len(self.env.final_calls), 1)

    def test_recharge_initializes_official_uid_session_before_rules(self):
        self.env.recharge()
        self.prepare()
        init = next(call for call in self.env.calls if '/initMessage?wel=' in call[0])
        plain = json.loads(_cbc_decrypt(init[1]['data']))
        self.assertEqual(plain['UID'], 'private_app_uid')
        self.assertEqual(plain['deviceInfo'], {'sb': '', 'sn': '', 'osType': '', 'packageName': '',
                                             'imei': 'fake-imei', 'loginProvince': '', 'loginCity': ''})
        shortened = _cbc_decrypt(base64.b64decode(init[0].split('?wel=')[1]))
        self.assertEqual(shortened, PHONE[:2]+PHONE[3:])
        init_index = self.env.calls.index(init)
        check_index = next(i for i, call in enumerate(self.env.calls) if '/pcardCheck/' in call[0])
        self.assertLess(init_index, check_index)

    def test_wallet_initialization_uses_login_device_and_account_region(self):
        self.env.snapshot['profile'].update(provinceCode='851', cityCode='0851')
        self.prepare()
        init = next(call for call in self.env.calls if '/initMessage?' in call[0])
        device = json.loads(_cbc_decrypt(init[1]['data']))['deviceInfo']
        self.assertEqual(device, {'sb': 'Xiaomi', 'sn': '2210132C', 'osType': 'android',
            'packageName': 'com.greenpoint.android.mc10086.activity', 'imei': 'fake-imei',
            'loginProvince': '851', 'loginCity': '0851'})

    def test_wallet_imei_fallback_uses_existing_android_id_from_login_context(self):
        self.env.snapshot['profile'].pop('imei')
        self.env.snapshot['app_context'] = {'device_id': '0123456789abcdef'}
        self.prepare()
        init = next(call for call in self.env.calls if '/initMessage?' in call[0])
        device = json.loads(_cbc_decrypt(init[1]['data']))['deviceInfo']
        self.assertEqual(device['imei'], 'AndroidId:0123456789abcdef0')

    def test_original_official_javascript_matches_init_and_order_bytes_offline(self):
        """Execute retained official CryptoJS and request builders with a fake XHR.

        Original H5 module SHA256:
        5d6999cba7e43525f8146fa765abb5054b62df4ab3e8bd0ba2b3c010abf80ca6.
        No HTTP or browser APIs are made available to the JavaScript sandbox.
        """
        sources = Path(__file__).resolve().parents[1]/'analysis'/'coupons-cache'
        names = ['reapp-recharge_H5module_inner.js', 'reapp-recharge-decoded.js',
                 'reapp-publicClient-decoded.js']
        if not shutil.which('node') or not all((sources/name).is_file() for name in names):
            self.skipTest('Retained official scripts and Node are required for offline source comparison')
        self.env.recharge()
        self.env.row.update(couponValue='7', valueFormat='7')
        self.env.rule['saleCode'] = '30~1000:7'
        self.env.snapshot['profile'].update(loginProvince='851', loginCity='851')
        preview = self.prepare()
        self.confirm(preview['operation_id'], charge_amount='30', captcha_token='fixture-official-token')
        program = r"""
const vm = require('node:vm');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const captured = [];
function FakeXHR() {
  this.open = (type, url) => { this.request = {type, url, headers: {}}; };
  this.setRequestHeader = (key, value) => { this.request.headers[key] = value; };
  this.send = body => { this.request.body = body; captured.push(this.request); };
}
const context = {window: {}, document: {scripts: []}, navigator: {userAgent: ''},
                 XMLHttpRequest: FakeXHR, console: {log() {}}};
vm.createContext(context);
vm.runInContext(input.module, context, {timeout: 1000});
const module = context.window.recharge_H5module_inner;
context.recharge_H5module_inner = module;
function originalFunction(name) {
  const start = input.page.indexOf('function '+name+'(');
  if (start < 0) throw Error('Original function missing: '+name);
  const next = input.page.indexOf('function ', start+9);
  return input.page.slice(start, next < 0 ? undefined : next);
}
for (const name of ['_0x2336ae', '_0x30868c', '_0x2d8861', '_0x356596', '_0x27f21d', '_0x156e80']) {
  vm.runInContext(originalFunction(name), context, {timeout: 1000});
}
const floatingStart = input.publicClient.indexOf("'floating':function(")+"'floating':".length;
const floatingEnd = input.publicClient.indexOf(",'versionJudgmentPlus':", floatingStart);
if (floatingStart < 12 || floatingEnd < 0) throw Error('Original floating function missing');
const floating = vm.runInContext('('+input.publicClient.slice(floatingStart, floatingEnd)+')', context);
context.publicClient = {floating, getQueryString() { return null; }};
context.input = input;
context.nativeUser = {...input.profile, phoneNumber: input.phone};
context._0x276bab = context.window;
Object.assign(context, {
  _0x5590ed: input.phone, _0x35e395: input.phone, _0x14fc31: false, _0x5f45e9: '',
  _0x451629: context.nativeUser, _0x169d93: null, _0x58c43d: false, _0x5c992d: 0,
  _0x3f4d98: input.operateId, _0x29e001: false, _0x2e73c4: 30, _0x3aec08: '',
  _0x29537b: '', _0x3544d8: 30, _0x5c1b05: input.couponCode, _0x261cf2: '',
  _0x844acf: '', _0x412597: '', _0x216ac2: '', _0x37f339: input.captchaToken, _0x24644f: null,
  _0x4d56f7: '', _0x3d723c: input.uid, _0x1794a7: '', _0x5248fd: '',
  _0x117320: false, _0x1c7c63: '-1', _0x50a83f: false, _0x578c9d: '',
  _0x5c8924: ['871'], _0x5dd1d1: '851', _0x2d93ff: 'not-selected', _0x381a34: null
});
context.$ = () => ({text: () => '37.00'});
context._0x9ba9a5 = body => module.saveRechargeOrder({
  reqBody: body, userInfo: context.nativeUser, noEncrypt: true, success() {}, error() {}
});
vm.runInContext(`
  var sign = _0x356596(_0x30868c, _0x2336ae)(input.uid, '', '');
  var data = recharge_H5module_inner.encryptByAES(JSON.stringify({
    UID: input.uid, sign: sign, deviceInfo: recharge_H5module_inner.getUserBaseInfo(nativeUser)
  }));
  recharge_H5module_inner.getAssertion({
    type: 'post', noEncrypt: true, userInfo: nativeUser,
    reqBody: {data: data, cellNum: input.phone.substring(0, 2)+input.phone.substring(3)},
    success() {}, error() {}
  });
  _0x156e80();
`, context, {timeout: 1000});
process.stdout.write(JSON.stringify(captured));
"""
        inputs = dict(zip(('module', 'page', 'publicClient'),
                          ((sources/name).read_text(encoding='utf-8') for name in names)))
        self.assertEqual(hashlib.sha256(inputs['module'].encode()).hexdigest(),
                         '5d6999cba7e43525f8146fa765abb5054b62df4ab3e8bd0ba2b3c010abf80ca6')
        inputs.update(phone=PHONE, profile=self.env.snapshot['profile'], uid='private_app_uid',
                      operateId=self.env.rule['operateId'], couponCode=self.env.row['pCardNo'],
                      captchaToken='fixture-official-token')
        run = subprocess.run(['node', '-e', program], input=json.dumps(inputs, ensure_ascii=False),
                             capture_output=True, text=True, encoding='utf-8', timeout=5, check=True)
        official_init, official_order = json.loads(run.stdout)
        python_init = next(call for call in self.env.calls if '/initMessage?wel=' in call[0])
        python_order = self.env.final_calls[0]
        for official, actual in [(official_init, python_init), (official_order, python_order)]:
            self.assertEqual(official['url'], actual[0])
            self.assertEqual(official['body'], json.dumps(actual[2], separators=(',', ':')))


if __name__ == '__main__':
    unittest.main()
