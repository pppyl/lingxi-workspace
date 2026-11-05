import base64
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import unittest
from unittest.mock import Mock, patch
from threading import Event

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from curl_cffi import requests

from coupon_client import (APP_TOKEN_PATH, ENTRY, GCM_KEY, ORIGIN, CouponClient, CouponError,
                           _cbc_encrypt, _payload)
from credential_cache import CredentialCache
from app_login import AppCredentialError

SNAPSHOT = {'phone': '13800000000', 'cookie': 'JSESSIONID=fake-app-secret',
            'profile': {}, 'credential_revision': 1}


def encoded(data, *, gcm=False):
    plain = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
    if gcm:
        nonce = b'123456789abc'
        inner = base64.b64encode(nonce+AESGCM(GCM_KEY).encrypt(nonce, plain.encode(), None))
    else:
        inner = _cbc_encrypt(plain).encode()
    return {'retCode': '000000', 'data': {'outParam': base64.b64encode(inner).decode()}}


class Response:
    def __init__(self, data=None, *, status=200, url=ENTRY, headers=None):
        self.status_code, self.url, self.headers = status, url, headers or {}
        self.data = data

    def json(self):
        return self.data


class Session:
    def __init__(self):
        self.headers, self.calls, self.posts, self.closed = {}, [], [], False
        self.number, self.fail_code, self.bad_cipher = SNAPSHOT['phone'], '', False
        self.redirect = ENTRY
        self.pages = 1
        self.status_code = '8'
        self.cookies = requests.Cookies()
        self.msg_id = base64.b64encode(_cbc_encrypt('official-msg-id').encode()).decode()

    def get(self, url, **options):
        self.calls.append((url, options))
        if '/getArtifact2' in url:
            return Response(status=302, url=url, headers={'location': self.redirect})
        if '/auth/loginfo' in url:
            return Response(encoded({'loginValue': self.number[:3]+'******'+self.number[-2:],
                'safeCode': self.number[3:9], 'userType': '4', 'provice': '100', 'msgId': self.msg_id}))
        return Response()

    def post(self, url, *, json, **options):
        self.posts.append((url, json, options))
        blob = base64.b64decode(base64.b64decode(json['inParam']))
        body = globals()['json'].loads(AESGCM(GCM_KEY).decrypt(blob[:12], blob[12:], None))
        if self.fail_code:
            return Response({'retCode': self.fail_code})
        if self.bad_cipher:
            return Response({'retCode': '000000', 'data': {'outParam': 'invalid'}})
        history = body['status'] != '1,8,9,21'
        row = {'pCardNo': 'private-coupon-number-'+body['status']+body.get('qryType', ''),
               'pcardId': 'test', 'stallName': '2元话费满减券', 'couponValue': '2',
               'unitFormat': '元', 'expireTime': '20261105235959', 'busiType': 'hf01',
               'couponStatus': body['status'].split(',')[0] if history else self.status_code,
               'marketName': 'APP专属', 'stallInfo': '充30元及以上可用', 'secret': 'not-public'}
        return Response(encoded({'busiData': [{'batchInfo': [row]}], 'totalNum': 1,
                                 'totlePage': self.pages}, gcm=True))

    def close(self):
        self.closed = True


class CouponClientTests(unittest.TestCase):
    def setUp(self):
        self.session = Session()
        self.provider = Mock(return_value=deepcopy(SNAPSHOT))
        self.flow = Mock()
        self.flow.call.return_value = (200, {'retCode': '000000', 'rspBody': {'token': 'private-app-ticket'}}, {})
        self.client = CouponClient(self.provider, session_factory=lambda: self.session, app_factory=lambda _: self.flow)

    def test_codec_matches_independent_node_crypto_vectors(self):
        self.assertEqual(_cbc_encrypt('13800000000'), 'nEbWjmkRAC/DZsDPUfuZxg==')
        self.assertEqual(_payload({'channelId': '11'})['inParam'],
                         'Wm5oMlRrMVpTRlJyWjI5Q0lFTUMzaExIVndZbWlzeUpqWnFWYm03QmVqaFNQTHhlVlgyNjBvcU40Qk5XeFE9PQ==')

    def test_app_sso_and_nested_list_are_normalized_without_coupon_secrets(self):
        result = self.client.query(sort='recent')
        self.flow.call.assert_called_once_with(APP_TOKEN_PATH,
                {'sourceId': '019020', 'url': ENTRY, 'cellNum': SNAPSHOT['phone']})
        self.assertEqual(self.session.calls[0][1]['params']['channelID'], '12014')
        self.assertEqual(len(self.session.posts), 1)
        item = result['items'][0]
        self.assertEqual((item['name'], item['amount'], item['category'], item['status']),
                         ('2元话费满减券', '2', 'fee', 'available'))
        self.assertEqual(item['expires_at'], '2026-11-05T23:59:59+08:00')
        self.assertEqual(item['received_at'], '')  # No invented receive date.
        self.assertEqual((result['total'], result['has_more']), (1, False))
        for secret in ['private-coupon-number', 'private-app-ticket', 'fake-app-secret', 'not-public', 'pCardNo']:
            self.assertNotIn(secret, json.dumps(result))
        self.assertNotIn('Cookie', self.session.headers)
        self.assertTrue(self.session.closed)

    def test_request_uses_dynamic_nonce_and_official_available_sort(self):
        self.client.query(sort='oldest')
        _, payload, _ = self.session.posts[0]
        blob = base64.b64decode(base64.b64decode(payload['inParam']))
        self.assertEqual(blob[:12], payload['msgId'].encode()[:12])
        body = json.loads(AESGCM(GCM_KEY).decrypt(blob[:12], blob[12:], None))
        self.assertEqual(body['sortType'], '5')
        self.assertEqual(body['loginType'], '0')
        self.assertEqual(body['channelId'], '11')
        self.assertNotIn('page', body)
        self.assertNotIn('qryType', body)

    def test_history_keeps_both_status_buckets_and_both_data_sources(self):
        self.session.pages = 2
        result = self.client.query(state='history', page=1)
        self.assertEqual(len(result['items']), 4)
        self.assertTrue(result['has_more'])
        bodies = []
        for _, payload, _ in self.session.posts:
            blob = base64.b64decode(base64.b64decode(payload['inParam']))
            bodies.append(json.loads(AESGCM(GCM_KEY).decrypt(blob[:12], blob[12:], None)))
        self.assertEqual({(b['status'], b['qryType'], b['page']) for b in bodies},
                         {('10', '0', 0), ('10', '1', 0), ('4,5,11,22', '0', 0), ('4,5,11,22', '1', 0)})

    def test_other_account_returned_by_sso_never_reaches_list(self):
        self.session.number = '13900000001'
        with self.assertRaises(CouponError) as error:
            self.client.query()
        self.assertEqual(error.exception.code, 'ACCOUNT_MISMATCH')
        self.assertEqual(self.session.posts, [])
        self.assertTrue(self.session.closed)

    def test_expired_app_login_with_empty_body_is_not_reported_as_bad_json(self):
        for code in ('400007', '420000'):
            with self.subTest(code=code):
                self.flow.call.return_value = (200, {'retCode': code, 'rspBody': '',
                                                     'retDesc': 'private account details'}, {})
                with self.assertRaises(CouponError) as error:
                    self.client.query()
                self.assertEqual((error.exception.code, error.exception.status), ('APP_LOGIN_REQUIRED', 401))
                self.assertIsNone(error.exception.__cause__)
                self.assertNotIn('private', str(error.exception))
                self.assertEqual(self.session.calls, [])
                self.assertTrue(self.session.closed)

    def test_primary_timeout_recovers_once_then_uses_new_credentials_for_wallet_auth(self):
        expired = (200, {'retCode':'410000','rspBody':''}, {})
        success = self.flow.call.return_value
        self.flow.call.side_effect = [expired,success]
        snapshots = []
        self.client.app_factory = lambda snapshot:(snapshots.append(snapshot) or self.flow)
        def recover(snapshot):
            self.assertEqual(snapshot['cookie'],SNAPSHOT['cookie'])
            self.provider.return_value = {**SNAPSHOT,'cookie':'JSESSIONID=renewed','credential_revision':2}
        self.client.app_session_recover = Mock(side_effect=recover)
        self.assertEqual(len(self.client.query()['items']),1)
        self.client.app_session_recover.assert_called_once()
        self.assertEqual([snapshot['cookie'] for snapshot in snapshots],[SNAPSHOT['cookie'],'JSESSIONID=renewed'])
        self.assertEqual(self.flow.call.call_count,2)
        self.assertEqual(len(self.session.posts),1)

    def test_official_maintenance_does_not_renew_identity_or_request_login(self):
        self.flow.call.return_value=(200,{'retCode':'000001','rspBody':None},{})
        self.client.app_session_recover=Mock()
        with self.assertRaises(CouponError) as error:self.client.query()
        self.assertEqual((error.exception.code,error.exception.status),('UPSTREAM_UNAVAILABLE',503))
        self.assertIn('公告',str(error.exception))
        self.client.app_session_recover.assert_not_called()
        self.assertEqual(self.session.calls,[])

    def test_wallet_query_maintenance_response_does_not_reauthenticate(self):
        self.session.fail_code='999996'
        self.client.app_session_recover=Mock()
        with self.assertRaises(CouponError) as error:self.client.query()
        self.assertEqual(error.exception.code,'UPSTREAM_UNAVAILABLE')
        self.flow.call.assert_called_once()
        self.client.app_session_recover.assert_not_called()


    def test_timeout_after_recovery_is_not_retried_again_or_reported_as_missing_token(self):
        self.flow.call.return_value = (200, {'retCode':'410000','rspBody':''}, {})
        self.client.app_session_recover = Mock()
        with self.assertRaises(CouponError) as error:self.client.query()
        self.assertEqual((error.exception.code,error.exception.status),('APP_SESSION_EXPIRED',401))
        self.assertIn('410000',str(error.exception))
        self.assertEqual(self.flow.call.call_count,2)
        self.client.app_session_recover.assert_called_once()
        self.assertFalse(self.session.posts)

    def test_expired_read_with_recovery_disabled_never_renews_primary_identity(self):
        self.flow.call.return_value = (200, {'retCode':'410000','rspBody':''}, {})
        self.client.app_session_recover = Mock()
        with self.assertRaises(CouponError):self.client.query(recover_login=False)
        self.client.app_session_recover.assert_not_called()
        self.flow.call.assert_called_once()

    def test_valid_cached_wallet_never_renews_an_unusable_primary_cookie(self):
        self.session.cookies.set('wallet-session','valid')
        self.client.credential_cache = CredentialCache()
        self.client.query()
        self.flow.call.reset_mock()
        self.flow.call.return_value = (200, {'retCode':'410000','rspBody':''}, {})
        self.client.app_session_recover = Mock()
        self.client.query()
        self.flow.call.assert_not_called()
        self.client.app_session_recover.assert_not_called()

    def test_concurrent_wallet_bootstrap_shares_one_primary_recovery(self):
        cache, entered, finish = CredentialCache(), Event(), Event()
        flow=Mock()
        flow.call.side_effect=[(200,{'retCode':'410000','rspBody':''},{}),self.flow.call.return_value]
        recover=Mock(side_effect=lambda snapshot:(entered.set(),finish.wait(2)))
        def query(_):
            return CouponClient(lambda:SNAPSHOT,session_factory=Session,app_factory=lambda _:flow,
                credential_cache=cache,app_session_recover=recover).query()
        with ThreadPoolExecutor(max_workers=6) as pool:
            tasks=[pool.submit(query,index) for index in range(6)]
            self.assertTrue(entered.wait(1));finish.set()
            self.assertTrue(all(len(task.result()['items'])==1 for task in tasks))
        recover.assert_called_once()
        self.assertEqual(flow.call.call_count,2)

    def test_primary_rotation_does_not_abort_verified_same_phone_wallet(self):
        original = self.session.get
        def get(url, **options):
            response = original(url, **options)
            if '/auth/loginfo' in url:
                self.provider.return_value = {**SNAPSHOT, 'credential_revision': 2, 'cookie': 'JSESSIONID=new'}
            return response
        self.session.get = get
        result = self.client.query()
        self.assertEqual(len(result['items']),1)
        self.assertEqual(len(self.session.posts),1)
        self.flow.call.assert_called_once()

    def test_metadata_only_refresh_keeps_the_existing_wallet_session_valid(self):
        with self.client._session() as (_, user, current):
            self.provider.return_value = {**SNAPSHOT, 'credential_revision': 2,
                                          'app_context': {'refreshed_at': 123}, 'token': 'new-business-token'}
            current()
            self.assertEqual(user['number'], SNAPSHOT['phone'])
        self.assertTrue(self.session.closed)

    def test_external_redirect_is_rejected_without_visiting_it(self):
        self.session.redirect = 'https://other.example/collect'
        with self.assertRaises(CouponError) as error:
            self.client.query()
        self.assertEqual(error.exception.code, 'SSO_REDIRECT')
        self.assertEqual(len(self.session.calls), 1)

    def test_unknown_status_is_not_misrepresented_as_usable(self):
        self.session.status_code = '9'
        item = self.client.query()['items'][0]
        self.assertEqual(item['status'], '官方状态 9')

    def test_activity_auth_error_does_not_claim_empty_wallet(self):
        self.session.fail_code = '500003'
        with self.assertRaises(CouponError) as error:
            self.client.query()
        self.assertEqual((error.exception.code, error.exception.status), ('COUPON_LOGIN_REQUIRED', 401))

    def test_bad_ciphertext_is_a_query_error_and_session_is_closed(self):
        self.session.bad_cipher = True
        with self.assertRaises(CouponError) as error:
            self.client.query()
        self.assertEqual(error.exception.code, 'UPSTREAM_FORMAT')
        self.assertTrue(self.session.closed)

    def test_network_errors_do_not_expose_the_secret_request_url(self):
        self.session.get = Mock(side_effect=requests.RequestsError('https://example/?token=private'))
        with self.assertRaises(CouponError) as error:
            self.client.query()
        self.assertEqual(error.exception.code, 'UPSTREAM_NETWORK')
        self.assertNotIn('private', str(error.exception))
        self.assertTrue(self.session.closed)


class CouponApiTests(unittest.TestCase):
    def setUp(self):
        from app import create_app
        self.app = create_app()
        self.client = self.app.test_client()
        self.pool = self.app.extensions['unified_logins']

    def test_page_uses_shared_accounts_and_public_metadata_only(self):
        record = self.pool.remember(SNAPSHOT['phone'], SNAPSHOT['cookie'], {}, 'private-lingxi-token', name='Test')
        self.assertEqual(self.client.get('/coupons').status_code, 200)
        data = self.client.get('/api/coupons/accounts').get_json()
        self.assertEqual(data['accounts'][0]['id'], record.id)
        for secret in [SNAPSHOT['phone'], SNAPSHOT['cookie'], 'private-lingxi-token']:
            self.assertNotIn(secret, json.dumps(data))

    def test_invalid_query_and_missing_or_expired_account_never_start_sso(self):
        with patch('coupon_client.AppLoginSession.from_credentials') as flow:
            self.assertEqual(self.client.get('/api/coupons?state=wrong').status_code, 400)
            self.assertEqual(self.client.get('/api/coupons?page=0').status_code, 400)
            self.assertEqual(self.client.get('/api/coupons?sort=wrong').status_code, 400)
            self.assertEqual(self.client.get('/api/coupons?login_id=missing').status_code, 404)
            record = self.pool.remember(SNAPSHOT['phone'], SNAPSHOT['cookie'], {}, '')
            record.status = 'login_required'
            self.assertEqual(self.client.get('/api/coupons?login_id='+record.id).status_code, 401)
            flow.assert_not_called()

    def test_use_routes_reject_missing_selection_and_unknown_operation(self):
        self.assertEqual(self.client.post('/api/coupons/use/prepare', json={}).status_code, 400)
        self.assertEqual(self.client.post('/api/coupons/use/sms', json={'operation_id': 'missing'}).status_code, 404)
        self.assertEqual(self.client.post('/api/coupons/use/confirm', json={'operation_id': 'missing'}).status_code, 404)
        self.assertEqual(self.client.post('/api/coupons/use/confirm', json=[]).status_code, 400)

    def test_wallet_recovery_forces_identity_once_commits_new_primary_and_leaves_other_realms_cached(self):
        record=self.pool.remember(SNAPSHOT['phone'],SNAPSHOT['cookie'],{},'existing-token',
            app_context={'device_id':'fixture','xk':'key','refreshed_at':__import__('time').time()})
        manager=self.app.extensions['coupon_uses']
        snapshot={**self.pool.credentials(record.id),'id':record.id}
        cache=self.app.extensions['credential_cache']
        cache.entries[('independent-realm',record.id)]={'phone':record.phone,'expires_at':None,'data':{}}
        def refreshed(flow,**kwargs):
            flow.cookie='JSESSIONID=renewed';flow.refreshed_at=__import__('time').time()
        with patch('app.AppLoginSession.refresh',autospec=True,side_effect=refreshed) as refresh:
            manager.app_session_recover(record.id,snapshot)
            manager.app_session_recover(record.id,snapshot)
        refresh.assert_called_once()
        self.assertTrue(refresh.call_args.kwargs['force_identity'])
        self.assertEqual(record.cookie,'JSESSIONID=renewed')
        self.assertIn(('independent-realm',record.id),cache.entries)

    def test_failed_renewal_updates_account_status_in_query_error_without_sending_sms(self):
        record=self.pool.remember(SNAPSHOT['phone'],SNAPSHOT['cookie'],{},'',app_context={'device_id':'fixture','xk':'key'})
        def query(*args,**kwargs):
            snapshot={**self.pool.credentials(record.id),'id':record.id}
            self.app.extensions['coupon_uses'].app_session_recover(record.id,snapshot)
        with patch('coupon_use.CouponClient.query',side_effect=query),patch('app.AppLoginSession.refresh',
                side_effect=AppCredentialError('官方要求重新验证（420000）',code='420000',login_required=True)),\
                patch('app.AppLoginSession.send_sms') as sms:
            response=self.client.get('/api/coupons?login_id='+record.id)
        self.assertEqual(response.status_code,401)
        self.assertEqual(response.json['code'],'APP_LOGIN_REQUIRED')
        self.assertEqual(response.json['account']['status'],'login_required')
        self.assertEqual(record.status,'login_required')
        sms.assert_not_called()

    def test_live_otp_binding_prevents_primary_recovery(self):
        record=self.pool.remember(SNAPSHOT['phone'],SNAPSHOT['cookie'],{},'')
        manager=self.app.extensions['coupon_uses']
        with patch.object(manager,'busy_login',return_value=True),patch('app.AppLoginSession.refresh') as refresh:
            with self.assertRaises(CouponError) as error:manager.app_session_recover(record.id,self.pool.credentials(record.id))
        self.assertEqual(error.exception.code,'ACCOUNT_BUSY')
        refresh.assert_not_called()

    def test_confirm_route_forwards_official_captcha_token_without_echoing_it(self):
        manager = self.app.extensions['coupon_uses']
        token = 'private-official-captcha-token'
        with patch.object(manager, 'confirm', return_value={'state': 'payment_required', 'message': '请在 APP 付款'}) as confirm:
            response = self.client.post('/api/coupons/use/confirm', json={
                'operation_id': 'fixture-operation', 'charge_amount': '30', 'captcha_token': token},
                headers={'User-Agent': 'Actual browser SDK context'})
        self.assertEqual(response.status_code, 200)
        confirm.assert_called_once_with('fixture-operation', sms_code='', charge_amount='30', captcha_token=token,
                                       risk_token='', risk_lid='', prepare_risk=False, browser_user_agent='Actual browser SDK context')
        self.assertNotIn(token, response.get_data(as_text=True))

    def test_pending_orders_remain_visible_for_expired_account_without_sso(self):
        record = self.pool.remember(SNAPSHOT['phone'], SNAPSHOT['cookie'], {}, '')
        record.status = 'login_required'
        self.app.extensions['coupon_uses'].data.update({
            'own': {'login_id': record.id, 'state': 'payment_required', 'order_id': 'own-order'},
            'other': {'login_id': 'other-account', 'state': 'payment_required', 'order_id': 'other-order'},
            'closed': {'login_id': record.id, 'state': 'closed', 'order_id': 'closed-order'}})
        with patch('coupon_client.AppLoginSession.from_credentials') as flow:
            response = self.client.get('/api/coupons/pending?login_id='+record.id)
            self.assertEqual(response.status_code, 200)
            self.assertEqual([r['order_id'] for r in response.get_json()['pending_orders']], ['own-order'])
            self.assertEqual(self.client.get('/api/coupons/pending?login_id=missing').status_code, 404)
            flow.assert_not_called()

    def test_saved_web_link_has_qr_and_cannot_be_read_after_account_logout(self):
        record = self.pool.remember(SNAPSHOT['phone'], SNAPSHOT['cookie'], {}, '')
        record.status = 'login_required'
        payment = {'state': 'payment_required', 'order_id': 'own-order', 'amount': '30.00',
                   'payment_type': 'alipay_web', 'payment_url': 'https://mclient.alipay.com/cashierRoutePay.htm?awid=fixture'}
        self.app.extensions['coupon_uses'].data['own'] = {'login_id': record.id,
            'state': 'payment_required', 'order_id': 'own-order', 'payment': payment}
        with patch('coupon_use.CouponAlipay') as gateway:
            response = self.client.post('/api/coupons/use/payment', json={'login_id': record.id,'order_id':'own-order'})
            self.assertEqual(response.status_code, 200)
            data = response.get_json()
            self.assertEqual(data['payment_url'], payment['payment_url'])
            self.assertTrue(data['qr_image'].startswith('data:image/svg+xml;base64,'))
            self.pool.forget(record.phone)
            response = self.client.post('/api/coupons/use/payment', json={'login_id': record.id,'order_id':'own-order'})
            self.assertEqual(response.status_code, 404)
            self.assertNotIn('payment_url', response.get_json())
            gateway.assert_not_called()

    def test_payment_challenge_forwards_human_token_and_registers_only_provider_logo(self):
        record = self.pool.remember(SNAPSHOT['phone'], SNAPSHOT['cookie'], {}, '')
        result = {'state': 'verification_required', 'verification': 'captcha',
                  'challenge': {'app_id': 'official-payment-app', 'risk_id': 'official-risk'}, 'message': '请验证'}
        manager = self.app.extensions['coupon_uses']
        with patch.object(manager, 'payment', return_value=result) as payment:
            response = self.client.post('/api/coupons/use/payment', json={
                'login_id': record.id, 'order_id': 'existing-order', 'refresh': True, 'captcha_token': 'human-token'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), result)
        self.assertNotIn('qr_image', response.get_json())
        payment.assert_called_once_with(record.id, 'existing-order', refresh=True, captcha_token='human-token')
        self.assertIn('official-payment-app', self.app.extensions['coupon_captcha_app_ids'])
        self.assertNotIn('unknown-app', self.app.extensions['coupon_captcha_app_ids'])


if __name__ == '__main__':
    unittest.main()
