import base64
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from flask import Flask

from app_login import AppLoginError
from recharge.cmcc import SHANGHAI
from services_query import (SERVICES_PATH, BINDINGS_PATH, CANCEL_GATEWAY_HOST, CANCEL_SMS_PATH, CANCEL_PATH, ServicesError,
                            ServicesQueries, normalize_services, register_services)

NOW = datetime(2026, 11, 6, 15, 30, tzinfo=SHANGHAI)
SNAPSHOT = {'phone': '13800000000', 'cookie': 'JSESSIONID=private', 'credential_revision': 1}
PRODUCT = {'productName': '5G套餐', 'productCode': 'P1', 'codeType': '01', 'busiFee': '128.000元/月',
           'actualPayFee': '68.000元/月', 'majorType': '0', 'freeType': '0', 'busiState': '0',
           'orderTime': '20261031120000', 'validDate': '2026-11-01 00:00:00', 'expireDate': '长期有效',
           'busiInfomsg': '<p>号码13800000000的说明</p><script>private script text</script><p>继续有效</p>',
           'businessdetailsList': [{'title': '套餐详情', 'titleValue': 0, 'businessdetails': [
               {'bubtitle': '适用范围', 'vlues': '国内通用'}, {'remark': '按月生效'}]}],
           'contractList': {'contractDetailsList': [{'contractName': '优惠合约', 'contractId': 'C1',
                'validDate': '20261101000000', 'expireDate': '20271031235959', 'cancelState': '1'}]},
           'token': 'never expose token', 'couponCode': 'never expose voucher', 'instanceId': 'private instance'}


def body_for(plan_type='0', scoped='', categories=None):
    categories = categories if categories is not None else {'01': [deepcopy(PRODUCT)], '02': [
        {'productName': '视频业务', 'busiFee': None, 'busiState': '3', 'unsubscribeTime': '20261101093000'}],
        '03': [{'productName': '基础功能', 'busiFee': '0元/月', 'busiState': '0'}],
        '04': [{'productName': '流量活动', 'busiFee': '--', 'busiState': '2', 'expireDate': '20261130235959'}]}
    return {'busiType': scoped, 'allBusiQryList': [{'allBusiInfoList': [{'planType': plan_type,
        'busiTypeInfoList': [{'busiID': '13800000000', 'busiValue': 'private address', 'busiInfoList': [
            {'productType': kind, 'productInfoList': rows} for kind, rows in categories.items()]}]}]}]}


class ServicesQueryTests(unittest.TestCase):
    def make_service(self, *, scoped='', **kwargs):
        flow = SimpleNamespace(cookie=SNAPSHOT['cookie'])
        def call(path, payload):
            self.assertEqual(path, SERVICES_PATH)
            body = body_for(payload['planType'], scoped)
            return 200, {'retCode': '000000', 'rspBody': deepcopy(body)}, {}
        flow.call = Mock(side_effect=call)
        factory = Mock(return_value=flow)
        service = ServicesQueries(lambda _: deepcopy(SNAPSHOT), app_factory=factory, now=lambda: NOW, **kwargs)
        return service, flow, factory

    def test_official_payloads_reuse_one_transport_without_refetching_all_category_response(self):
        service, flow, factory = self.make_service()
        original, transports = flow.call.side_effect, []
        def call(path, payload):
            transports.append(flow.transport)
            return original(path, payload)
        flow.call.side_effect = call
        with patch('services_query.requests.Session') as create:
            first, second = Mock(), Mock()
            create.return_value.__enter__.side_effect = [first, second]
            service.query('a'); service.query('a')
        self.assertEqual(transports, [first, second])
        self.assertEqual(factory.call_count, 2, 'a later click queries fresh state using existing credentials')
        self.assertEqual(create.return_value.__exit__.call_count, 2)
        self.assertEqual([entry.args for entry in flow.call.call_args_list], [
            (SERVICES_PATH, {'cellNum': SNAPSHOT['phone'], 'bgnMonth': '2026-11', 'endMonth': '2026-11', 'planType': '0', 'busiType': '01'})]*2)

    def test_official_prices_dates_states_types_and_recursive_details_are_preserved(self):
        source = body_for(); before = deepcopy(source)
        groups = normalize_services(source, '0', '01', NOW)
        self.assertEqual(source, before, 'formatting must not mutate official results')
        self.assertEqual([group['name'] for group in groups], ['手机 · 我的套餐', '手机 · 增值业务', '手机 · 基础功能', '手机 · 活动'])
        item = groups[0]['items'][0]
        self.assertEqual(item['name'], '5G套餐'); self.assertEqual(item['fee'], '128.000元/月')
        self.assertEqual(item['status'], '已订')
        fields = {field['name']: field['value'] for field in item['fields']}
        self.assertEqual(fields['实际消费'], '68.000元/月')
        self.assertEqual(fields['费用标记'], '减免')
        self.assertEqual(fields['生效时间'], '2026-11-01 00:00:00')
        self.assertEqual(fields['失效时间'], '长期有效'); self.assertEqual(fields['套餐类型'], '主套餐')
        self.assertIn('138****0000', fields['业务说明']); self.assertIn('继续有效', fields['业务说明'])
        self.assertNotIn('script text', fields['业务说明'])
        self.assertEqual(item['children'][0]['name'], '套餐详情')
        self.assertEqual(item['children'][0]['fields'][0]['value'], '0')
        self.assertEqual(item['children'][0]['fields'][1], {'name': '适用范围', 'value': '国内通用'})
        self.assertEqual(item['children'][1]['name'], '优惠合约')
        self.assertEqual(item['children'][1]['status'], '已退订')
        self.assertIsNone(groups[1]['items'][0]['fee'])
        self.assertEqual(groups[1]['items'][0]['status'], '已失效')
        self.assertEqual(groups[2]['items'][0]['fee'], '0元/月')
        self.assertEqual(groups[3]['items'][0]['status'], '即将到期')
        for secret in (SNAPSHOT['phone'], 'private address', 'private instance', 'never expose token', 'never expose voucher'):
            self.assertNotIn(secret, str(groups))

    def test_pending_cancelled_unknown_states_and_base64_html_are_not_misreported(self):
        encoded = base64.b64encode('<p>下月生效</p><a href="https://example.invalid/?token=secret">说明</a>'.encode()).decode()
        rows = [{'productName': '下月套餐', 'busiFee': '以实际资费为准', 'validDate': '20261201000000', 'majorType': '0', 'busiState': '0'},
                {'productName': '已撤销套餐', 'validDate': '20261201000000', 'majorType': '0', 'unsubscribeFlag': '0'},
                {'productName': '撤销中套餐', 'validDate': '20261201000000', 'majorType': '0', 'unsubscribeFlag': '2'},
                {'productName': '不明状态', 'busiState': '99', 'busiInfomsg': encoded},
                {'productName': '说明含隐私', 'busiInfomsg': '+8613800000000 token=secret couponCode=private 券码：private-voucher'},
                {'productName': '未来时间未知', 'validDate': 'invalid', 'expireDate': '2026-99-99', 'busiFee': 0}]
        result = normalize_services(body_for(categories={'01': rows}), '0', '01', NOW)[0]['items']
        self.assertEqual(result[0]['status'], '待生效'); self.assertEqual(result[0]['fee'], '以实际资费为准')
        self.assertEqual(result[1]['status'], '已撤销'); self.assertEqual(result[2]['status'], '撤销中')
        self.assertIsNone(result[3]['status'])
        self.assertEqual(result[3]['fields'][0]['value'], '下月生效\n说明')
        self.assertNotIn('secret', str(result)); self.assertNotIn('private', str(result)); self.assertNotIn('+8613800000000', str(result))
        self.assertEqual(result[-1]['fee'], '0', 'an official unitless zero is never expanded to a guessed 0元')

    def test_category_scoped_response_queries_all_tabs_but_does_not_duplicate_products(self):
        service, flow, _ = self.make_service(scoped='01')
        result = service.query('a')
        queries = [call.args[1] for call in flow.call.call_args_list if call.args[0] == SERVICES_PATH]
        self.assertEqual([payload['busiType'] for payload in queries], ['01', '02', '03', '04'])
        self.assertEqual(len(result['groups']), 4)
        self.assertEqual(len(result['groups'][0]['items']), 1)

    def test_explicit_linked_records_are_reused_without_discovery_or_extra_type_queries(self):
        service, flow, _ = self.make_service()
        body = body_for()
        body['allBusiQryList'][0]['allBusiInfoList'].extend(
            body_for(kind)['allBusiQryList'][0]['allBusiInfoList'][0] for kind in ('1', '2'))
        flow.call.side_effect = lambda *_: (200, {'retCode': '000000', 'rspBody': deepcopy(body)}, {})
        result = service.query('a')
        self.assertEqual([call.args[1]['planType'] for call in flow.call.call_args_list], ['0'])
        self.assertEqual(len(result['groups']), 12)
        self.assertEqual(result['groups'][4]['name'], '固话 · 我的套餐')
        self.assertEqual(result['groups'][8]['name'], '宽带 · 我的套餐')

    def test_format_and_scope_mismatches_are_explicit_before_additional_requests(self):
        service, flow, _ = self.make_service()
        for body, code in (({}, 'SERVICES_FORMAT'), (body_for('2'), 'SERVICES_SCOPE_MISMATCH'),
                           ({'allBusiQryList': ['invalid']}, 'SERVICES_FORMAT')):
            flow.call.reset_mock(); flow.call.side_effect = lambda *_, body=body: (200, {'retCode': '000000', 'rspBody': body}, {})
            with self.subTest(body=body), self.assertRaises(ServicesError) as raised:
                service.query('a')
            self.assertEqual(raised.exception.code, code); self.assertEqual(flow.call.call_count, 1)
        service, flow, _ = self.make_service()
        flow.call.side_effect = lambda *_: (200, {'retCode': '000000', 'rspBody': {'allBusiQryList': []}}, {})
        result = service.query('a')
        self.assertEqual(result['groups'], []); self.assertIn('官方暂未返回已订业务。', result['notes'])

    def test_same_account_queries_merge_and_other_accounts_use_independent_clients(self):
        entered, release, joined = Event(), Event(), Event()
        snapshots = {'a': deepcopy(SNAPSHOT), 'b': {**SNAPSHOT, 'phone': '13900000000', 'cookie': 'JSESSIONID=second'}}
        clients = []
        def factory(snapshot):
            client = SimpleNamespace(cookie=snapshot['cookie'], phone=snapshot['phone'])
            def call(path, payload):
                self.assertEqual(payload['cellNum'], client.phone)
                if path == SERVICES_PATH and client.phone == SNAPSHOT['phone']:
                    entered.set(); self.assertTrue(release.wait(3))
                self.assertEqual(path, SERVICES_PATH)
                return 200, {'retCode': '000000', 'rspBody': body_for()}, {}
            client.call = Mock(side_effect=call); clients.append(client)
            return client
        service = ServicesQueries(lambda key: deepcopy(snapshots[key]), app_factory=factory, now=lambda: NOW)
        with ThreadPoolExecutor(max_workers=3) as pool:
            first = pool.submit(service.query, 'a'); self.assertTrue(entered.wait(2))
            pending = service.pending['a']; original_result = pending.result
            def waiter():
                joined.set()
                return original_result()
            pending.result = waiter
            second = pool.submit(service.query, 'a'); self.assertTrue(joined.wait(2))
            other = pool.submit(service.query, 'b')
            release.set(); result = first.result(); self.assertEqual(result, second.result()); other.result()
        self.assertEqual([client.call.call_count for client in clients], [1, 1])
        self.assertIsNot(clients[0].transport, clients[1].transport)
        result['groups'].clear(); self.assertTrue(second.result()['groups'])

    def test_missing_or_explicitly_rejected_credentials_restore_once_and_read_latest_snapshot(self):
        recover = Mock(); service, flow, factory = self.make_service(recover=recover)
        fresh = {**SNAPSHOT, 'cookie': 'JSESSIONID=fresh', 'credential_revision': 2}
        service.credentials = Mock(side_effect=[{**SNAPSHOT, 'cookie': ''}, deepcopy(fresh)])
        flow.cookie = fresh['cookie']
        service.query('a'); recover.assert_called_once_with({**SNAPSHOT, 'cookie': ''})
        self.assertEqual(factory.call_args.args[0], fresh)
        service.credentials = Mock(side_effect=[deepcopy(SNAPSHOT), deepcopy(fresh)])
        original, calls = flow.call.side_effect, 0
        def expired(path, payload):
            nonlocal calls
            calls += 1
            return (200, {'retCode': '410000'}, {}) if calls == 1 else original(path, payload)
        flow.call.side_effect = expired
        service.query('a'); self.assertEqual(recover.call_count, 2)
        service.credentials = lambda _: deepcopy(SNAPSHOT)
        flow.call.reset_mock(); flow.call.side_effect = lambda *_: (200, {'retCode': '400007'}, {})
        with self.assertRaises(ServicesError) as raised:
            service.query('a')
        self.assertEqual(raised.exception.status, 401); self.assertEqual(flow.call.call_count, 2)
        self.assertEqual(recover.call_count, 3, 'one bounded recovery per user query')

    def test_recovery_cannot_switch_selected_phone(self):
        service, flow, factory = self.make_service(recover=Mock())
        service.credentials = Mock(side_effect=[{**SNAPSHOT, 'cookie': ''}, {**SNAPSHOT, 'phone': '13900000000'}])
        with self.assertRaises(ServicesError) as raised:
            service.query('a')
        self.assertEqual(raised.exception.code, 'SERVICES_LOGIN_REQUIRED')
        factory.assert_not_called(); flow.call.assert_not_called()

    def test_rate_business_network_and_json_errors_do_not_login_or_repeat_requests(self):
        recover = Mock(); service, flow, _ = self.make_service(recover=recover)
        for code in ('-800', '8053', '400003', '228000'):
            flow.call.reset_mock(); flow.call.side_effect = lambda *_, code=code: (200, {'retCode': code}, {})
            with self.subTest(code=code), self.assertRaises(ServicesError) as raised:
                service.query('a')
            self.assertEqual(flow.call.call_count, 1)
            self.assertEqual(raised.exception.status, 429 if code in ('-800', '8053') else 502)
        flow.call.side_effect = AppLoginError('network error')
        with self.assertRaises(ServicesError) as raised:
            service.query('a')
        self.assertEqual(raised.exception.code, 'SERVICES_NETWORK')
        flow.call.side_effect = lambda *_: (200, {'retCode': '000000', 'rspBody': 'invalid json'}, {})
        with self.assertRaises(ServicesError) as raised:
            service.query('a')
        self.assertEqual(raised.exception.code, 'SERVICES_FORMAT'); recover.assert_not_called()

    def test_secondary_rate_limit_keeps_prior_businesses_and_stops_remaining_queries(self):
        recover = Mock(); service, flow, _ = self.make_service(scoped='01', recover=recover)
        original = flow.call.side_effect
        flow.call.side_effect = lambda path, payload: ((200, {'retCode': '-800'}, {}) if payload.get('busiType') == '02' else original(path, payload))
        result = service.query('a')
        self.assertEqual(len(result['groups']), 1)
        self.assertEqual(result['groups'][0]['name'], '手机 · 我的套餐')
        self.assertIn('增值业务未更新', result['notes'][0]); self.assertEqual(flow.call.call_count, 2)
        recover.assert_not_called()

    def test_secondary_auth_restores_that_read_only_request_without_replaying_primary(self):
        recover = Mock(); service, flow, _ = self.make_service(scoped='01', recover=recover)
        original, failed = flow.call.side_effect, False
        def expired(path, payload):
            nonlocal failed
            if payload.get('busiType') == '02' and not failed:
                failed = True
                return 200, {'retCode': '400009'}, {}
            return original(path, payload)
        flow.call.side_effect = expired
        result = service.query('a')
        self.assertEqual(len(result['groups']), 4); recover.assert_called_once_with(SNAPSHOT)
        types = [call.args[1].get('busiType') for call in flow.call.call_args_list]
        self.assertEqual(types, ['01', '02', '02', '03', '04'])

    def test_cookie_rotation_returns_to_shared_storage_without_initialization(self):
        accept = Mock(); service, flow, _ = self.make_service(accept_credentials=accept)
        flow.cookie = 'JSESSIONID=rotated'
        service.query('a'); accept.assert_called_once_with(SNAPSHOT, flow)
        self.assertEqual({call.args[0] for call in flow.call.call_args_list}, {SERVICES_PATH})

    def test_register_route_only_queries_selected_shared_account_on_user_request(self):
        app = Flask('services-routes'); credentials = Mock(return_value=deepcopy(SNAPSHOT))
        accounts = lambda: [{'id': 'a', 'status': 'active', 'phone_masked': '138****0000'}]
        register_services(app, accounts, credentials)
        queries = app.extensions['services_queries']; queries.query = Mock(return_value={'groups': [], 'notes': []})
        credentials.assert_not_called(); queries.query.assert_not_called()
        client = app.test_client()
        self.assertEqual(client.get('/api/services/query?login_id=other').status_code, 404)
        response = client.get('/api/services/query?login_id=a')
        self.assertEqual(response.status_code, 200); self.assertEqual(response.json['account']['id'], 'a')
        queries.query.assert_called_once_with('a')
        queries.query.side_effect = ServicesError('请重新登录', 'SERVICES_LOGIN_REQUIRED', 401)
        self.assertEqual(client.get('/api/services/query?login_id=a').status_code, 401)


class ServiceCancellationTests(unittest.TestCase):
    def setUp(self):
        self.now = NOW
        self.snapshots = {'a': deepcopy(SNAPSHOT), 'b': {**SNAPSHOT, 'phone': '13900000000', 'cookie': 'JSESSIONID=second'}}
        self.product = {'productName': '流量会员', 'productCode': 'P-private', 'codeType': '01', 'instanceId': 'instance-private',
                        'cancelFlag': '0', 'unsubscribeFlag': '1', 'contractFlag': '1', 'busiState': '0',
                        'busiReturnEffTime': '2', 'effTimeType': '0|1|3', 'offLineFlag': '1',
                        'busiInfomsg': '<p>按月生效，不影响主套餐。</p>'}
        self.bindings = {'uniQryRecList': [{'prodInfo': [
            {'bunessName': '关联优惠', 'busiInfomsg': '1'}, {'bunessName': '绑定业务', 'busiInfomsg': '0'},
            {'bunessName': '同时影响', 'busiInfomsg': '2'}]}]}
        self.recover, self.clients = Mock(), []
        self.handler = None
        def factory(snapshot):
            client = SimpleNamespace(cookie=snapshot['cookie'], phone=snapshot['phone'])
            def call(path, payload):
                if self.handler is not None:
                    result = self.handler(client, path, payload)
                    if result is not None:
                        return result
                if path == SERVICES_PATH:
                    body = body_for(categories={'02': [deepcopy(self.product)]})
                    body['allBusiQryList'][0]['allBusiInfoList'][0]['busiTypeInfoList'][0]['busiID'] = client.phone
                    return 200, {'retCode': '000000', 'rspBody': body}, {}
                if path == BINDINGS_PATH:
                    return 200, {'retCode': '000000', 'rspBody': deepcopy(self.bindings)}, {}
                if path in (CANCEL_SMS_PATH, CANCEL_PATH):
                    return 200, {'retCode': '000000'}, {}  # Neither success requires rspBody.
                raise AssertionError('Unexpected official path: '+path)
            client.call = Mock(side_effect=call)
            self.clients.append(client)
            return client
        self.factory = Mock(side_effect=factory)
        self.service = ServicesQueries(lambda key: deepcopy(self.snapshots.get(key)), recover=self.recover,
                                      app_factory=self.factory, now=lambda: self.now)
        self.cancel = self.service.cancellations

    def action(self, login_id='a'):
        return self.service.query(login_id)['groups'][0]['items'][0]['cancel']['action_id']

    def prepared(self, login_id='a'):
        return self.cancel.prepare(login_id, self.action(login_id))

    def sent(self, effective='1'):
        prepared = self.prepared()
        self.cancel.sms('a', prepared['cancel_id'], effective)
        return prepared

    def calls(self, path):
        return [call.args[1] for client in self.clients for call in client.call.call_args_list if call.args[0] == path]

    def test_cancel_preparation_sms_and_submit_share_the_official_it_gateway_and_legacy_encryption(self):
        prepared=self.prepared()
        query_flow,cancel_flow=self.clients
        self.assertFalse(hasattr(query_flow,'gateway_host'),'不修改已验证的普通查询默认线路')
        self.assertEqual(cancel_flow.gateway_host,CANCEL_GATEWAY_HOST)
        self.assertEqual(cancel_flow.request_version,2)
        self.cancel.sms('a',prepared['cancel_id'],'0')
        result=self.cancel.confirm('a',prepared['cancel_id'],'0','123456',True)
        self.assertEqual(result['status'],'success')
        self.assertEqual([call.args[0] for call in cancel_flow.call.call_args_list],[BINDINGS_PATH,CANCEL_SMS_PATH,CANCEL_PATH])
        self.assertEqual(cancel_flow.gateway_host,'https://clientaccess.10086.cn')
        self.assertEqual(cancel_flow.request_version,2)

    def test_query_returns_only_opaque_owned_actions_and_collapses_duplicate_instances(self):
        self.handler = lambda client, path, payload: (200, {'retCode': '000000', 'rspBody': body_for(categories={'02': [deepcopy(self.product), deepcopy(self.product)]})}, {}) if path == SERVICES_PATH else None
        items = self.service.query('a')['groups'][0]['items']
        self.assertTrue(items[0]['cancel']['available'])
        self.assertEqual(items[0]['cancel']['action_id'], items[1]['cancel']['action_id'])
        for private in ('P-private', 'instance-private', SNAPSHOT['phone'], 'JSESSIONID'):
            self.assertNotIn(private, str(items))
        with self.assertRaises(ServicesError) as raised:
            self.cancel.prepare('b', items[0]['cancel']['action_id'])
        self.assertEqual(raised.exception.code, 'SERVICES_ACTION_EXPIRED')
        self.assertEqual(self.calls(BINDINGS_PATH), [])

    def test_official_scope_flags_missing_identifiers_and_contracts_are_not_sent_to_normal_cancel(self):
        cases = [({'majorType': '0'}, '01', '主套餐'), ({'majorType': '2'}, '01', '家庭套餐'),
                 ({'contractFlag': '0'}, '02', '合约'), ({'unsubscribeFlag': '0'}, '02', '已退订'),
                 ({'unsubscribeFlag': '2'}, '02', '退订中'), ({'busiState': '3'}, '02', '已失效'),
                 ({'expireDate': '20261101000000'}, '02', '已失效'), ({'cancelFlag': '1'}, '02', '未向'),
                 ({'productCode': ''}, '02', '标识'), ({'codeType': '02', 'sPCode': 'S', 'bizCode': ''}, '02', '标识'),
                 ({'codeType': '99'}, '02', '标识'), ({'instanceId': ''}, '02', '业务实例')]
        for changes, category, reason in cases:
            self.product.update(changes)
            source = body_for(categories={category: [deepcopy(self.product)]})
            self.handler = lambda client, path, payload, source=source: (200, {'retCode': '000000', 'rspBody': deepcopy(source)}, {}) if path == SERVICES_PATH else None
            item = self.service.query('a')['groups'][0]['items'][0]
            with self.subTest(changes=changes):
                self.assertFalse(item['cancel']['available']); self.assertNotIn('action_id', item['cancel'])
                self.assertIn(reason, item['cancel']['reason'])
            self.setUp()
        self.handler = lambda client, path, payload: (200, {'retCode': '000000', 'rspBody': body_for('1', categories={'02': [deepcopy(self.product)]})}, {}) if path == SERVICES_PATH else None
        # normalize_services itself rejects a mismatched sole linked scope, while
        # an explicit linked record alongside the selected mobile stays read-only.
        mobile = body_for(categories={'02': [deepcopy(self.product)]})
        mobile['allBusiQryList'][0]['allBusiInfoList'].extend(body_for('1', categories={'02': [deepcopy(self.product)]})['allBusiQryList'][0]['allBusiInfoList'])
        self.handler = lambda *args: (200, {'retCode': '000000', 'rspBody': deepcopy(mobile)}, {})
        self.assertFalse(self.service.query('a')['groups'][1]['items'][0]['cancel']['available'])

    def test_prepare_reuses_read_only_context_and_discloses_official_linked_impacts_without_sms(self):
        action = self.action()
        prepared = self.cancel.prepare('a', action)
        self.assertEqual(prepared['phone_masked'], '138****0000')
        self.assertEqual(prepared['effective_options'], [{'value': '0', 'label': '下月失效'}, {'value': '1', 'label': '立即失效'}, {'value': '3', 'label': '次日失效'}])
        self.assertEqual(prepared['bound_services'], ['绑定业务', '同时影响'])
        self.assertEqual(prepared['related_services'], ['关联优惠', '同时影响'])
        self.assertEqual(len(prepared['warnings']), 3); self.assertIn('不影响主套餐', prepared['description'])
        self.assertEqual(self.calls(BINDINGS_PATH), [{'cellNum': SNAPSHOT['phone'], 'bindedFlag': '2', 'instanceId': 'instance-private'}])
        self.assertEqual(self.cancel.prepare('a', action), prepared)
        self.assertEqual(len(self.calls(BINDINGS_PATH)), 1)
        self.assertEqual(self.calls(CANCEL_SMS_PATH), []); self.assertEqual(self.calls(CANCEL_PATH), [])
        self.assertFalse(self.cancel.busy_login('a')); self.recover.assert_not_called()

    def test_sms_and_confirm_keep_original_flow_when_shared_credentials_rotate(self):
        prepared = self.prepared(); bound_flow = self.clients[-1]
        self.snapshots['a'] = {**SNAPSHOT, 'cookie': 'JSESSIONID=background', 'credential_revision': 99}
        self.cancel.sms('a', prepared['cancel_id'], '0')
        self.assertTrue(self.cancel.busy_login('a')); self.assertFalse(self.cancel.busy_login('b'))
        result = self.cancel.confirm('a', prepared['cancel_id'], '0', '123456', True)
        self.assertEqual(result['status'], 'success'); self.assertFalse(self.cancel.busy_login('a'))
        self.assertIs(self.clients[-1], bound_flow); self.assertEqual(self.factory.call_count, 2)
        self.assertEqual(bound_flow.cookie, SNAPSHOT['cookie'])
        self.assertEqual(self.calls(CANCEL_SMS_PATH), [{'cellNum': SNAPSHOT['phone'], 'busType': '1', 'productName': '流量会员,绑定业务,同时影响', 'productType': '01', 'effectiveType': '2', 'productId': 'P-private', 'spid': '', 'bizCode': ''}])
        self.assertEqual(self.calls(CANCEL_PATH), [{'cellNum': SNAPSHOT['phone'], 'businessName': '流量会员', 'productType': '01', 'productId': 'P-private', 'spid': '', 'bizCode': '', 'smsCode': '123456', 'instanceId': 'instance-private', 'busiReturnEffTime': '0'}])
        self.recover.assert_not_called()

    def test_sp_business_and_missing_effective_time_use_their_own_official_values(self):
        self.product.update(codeType='02', productCode='', sPCode='SP-private', bizCode='B-private', busiReturnEffTime='', effTimeType='')
        prepared = self.prepared()
        self.assertEqual(prepared['effective_options'], [{'value': 'default', 'label': '失效时间以短信通知为准'}])
        self.cancel.sms('a', prepared['cancel_id'], 'default')
        self.cancel.confirm('a', prepared['cancel_id'], 'default', '123456', True)
        self.assertEqual(self.calls(CANCEL_SMS_PATH)[0]['effectiveType'], '3')
        final = self.calls(CANCEL_PATH)[0]
        self.assertEqual((final['productType'], final['productId'], final['spid'], final['bizCode']), ('02', '', 'SP-private', 'B-private'))
        self.assertNotIn('busiReturnEffTime', final, 'undefined in native request is omitted, not guessed as next-day')

    def test_sms_cooldown_effective_option_binding_and_five_minute_expiry(self):
        prepared = self.sent('1'); cancel_id = prepared['cancel_id']
        with self.assertRaises(ServicesError) as raised: self.cancel.sms('a', cancel_id, '3')
        self.assertEqual(raised.exception.code, 'SERVICES_SMS_COOLDOWN')
        with self.assertRaises(ServicesError) as raised: self.cancel.confirm('a', cancel_id, '3', '123456', True)
        self.assertEqual(raised.exception.code, 'SERVICES_SMS_MISMATCH')
        self.assertEqual(self.calls(CANCEL_PATH), [])
        self.now += timedelta(seconds=60)
        self.cancel.sms('a', cancel_id, '3')
        self.assertEqual([call['effectiveType'] for call in self.calls(CANCEL_SMS_PATH)], ['1', '5'])
        self.now += timedelta(minutes=5)
        self.assertFalse(self.cancel.busy_login('a'))
        with self.assertRaises(ServicesError) as raised: self.cancel.confirm('a', cancel_id, '3', '123456', True)
        self.assertEqual(raised.exception.code, 'SERVICES_SMS_EXPIRED'); self.assertEqual(self.calls(CANCEL_PATH), [])

    def test_identity_exit_or_phone_change_blocks_bound_sms_and_submit(self):
        prepared = self.sent()
        self.snapshots.pop('a')
        with self.assertRaises(ServicesError) as raised: self.cancel.confirm('a', prepared['cancel_id'], '1', '123456', True)
        self.assertEqual(raised.exception.code, 'SERVICES_LOGIN_REQUIRED')
        self.snapshots['a'] = {**SNAPSHOT, 'phone': '13700000000'}
        with self.assertRaises(ServicesError): self.cancel.sms('a', prepared['cancel_id'], '1')
        self.assertEqual(self.calls(CANCEL_PATH), []); self.assertEqual(len(self.calls(CANCEL_SMS_PATH)), 1)

    def test_preparation_recovers_only_explicit_auth_once_and_never_recovers_after_sending_sms(self):
        action = self.action()
        self.snapshots['a']['cookie'] = ''
        self.recover.side_effect = lambda snapshot: self.snapshots['a'].update(cookie='JSESSIONID=fresh', credential_revision=2)
        prepared = self.cancel.prepare('a', action)
        self.recover.assert_called_once(); self.assertEqual(self.clients[-1].cookie, 'JSESSIONID=fresh')
        self.handler = lambda client, path, payload: (200, {'retCode': '410000'}, {}) if path == CANCEL_SMS_PATH else None
        with self.assertRaises(ServicesError) as raised: self.cancel.sms('a', prepared['cancel_id'], '1')
        self.assertEqual(raised.exception.code, 'SERVICES_LOGIN_REQUIRED'); self.assertEqual(self.recover.call_count, 1)
        self.assertEqual(len(self.calls(CANCEL_SMS_PATH)), 1); self.assertFalse(self.cancel.busy_login('a'))

    def test_binding_failure_or_malformed_impact_blocks_sms_and_is_not_retried_as_a_mutation(self):
        action = self.action()
        for value in ({'retCode': '-800'}, {'retCode': '400003'}, {'retCode': '000000', 'rspBody': {'uniQryRecList': [{'prodInfo': ['bad']}]}}):
            self.handler = lambda client, path, payload, value=value: (200, deepcopy(value), {}) if path == BINDINGS_PATH else None
            with self.subTest(value=value), self.assertRaises(ServicesError): self.cancel.prepare('a', action)
        self.assertEqual(len(self.calls(BINDINGS_PATH)), 3); self.assertEqual(self.cancel.sessions, {})
        self.assertEqual(self.calls(CANCEL_SMS_PATH), []); self.assertEqual(self.calls(CANCEL_PATH), [])
        self.recover.assert_not_called()

    def test_explicit_sms_validation_errors_allow_correction_but_business_mismatch_requires_new_sms(self):
        prepared = self.sent(); cancel_id = prepared['cancel_id']
        self.handler = lambda client, path, payload: (200, {'retCode': '312008', 'retDesc': '验证码错误'}, {}) if path == CANCEL_PATH else None
        with self.assertRaises(ServicesError) as raised: self.cancel.confirm('a', cancel_id, '1', '000000', True)
        self.assertEqual(raised.exception.code, '312008'); self.assertTrue(self.cancel.busy_login('a'))
        self.handler = lambda client, path, payload: (200, {'retCode': '312009', 'retDesc': '验证码与业务不符'}, {}) if path == CANCEL_PATH else None
        with self.assertRaises(ServicesError) as raised: self.cancel.confirm('a', cancel_id, '1', '111111', True)
        self.assertEqual(raised.exception.code, '312009')
        with self.assertRaises(ServicesError) as raised: self.cancel.confirm('a', cancel_id, '1', '222222', True)
        self.assertEqual(raised.exception.code, 'SERVICES_SMS_EXPIRED'); self.assertEqual(len(self.calls(CANCEL_PATH)), 2)
        self.now += timedelta(minutes=1); self.handler = None
        self.cancel.sms('a', cancel_id, '1')
        self.assertEqual(self.cancel.confirm('a', cancel_id, '1', '333333', True)['status'], 'success')

    def test_unconfirmed_requests_invalid_codes_or_unoffered_options_never_submit(self):
        prepared = self.sent(); cancel_id = prepared['cancel_id']
        for effective, code, confirmed in [('1', '123456', False), ('1', '12345', True), ('1', 123456, True), ('9', '123456', True)]:
            with self.subTest(effective=effective, code=code, confirmed=confirmed), self.assertRaises(ServicesError):
                self.cancel.confirm('a', cancel_id, effective, code, confirmed)
        self.assertEqual(self.calls(CANCEL_PATH), [])

    def test_success_accepted_and_uncertain_results_are_terminal_even_after_new_query(self):
        for code, status in [('000000', 'success'), ('900000', 'accepted'), ('999999', 'unknown'), (None, 'unknown')]:
            self.setUp(); prepared = self.sent(); cancel_id = prepared['cancel_id']
            self.handler = lambda client, path, payload, code=code: (200, {'retCode': code} if code else {}, {}) if path == CANCEL_PATH else None
            first = self.cancel.confirm('a', cancel_id, '1', '123456', True)
            self.assertEqual(first['status'], status)
            self.assertEqual(self.cancel.confirm('a', cancel_id, '1', '123456', True), first)
            item = self.service.query('a')['groups'][0]['items'][0]
            self.assertFalse(item['cancel']['available']); self.assertNotIn('action_id', item['cancel'])
            self.assertEqual(len(self.calls(CANCEL_PATH)), 1)
            self.assertFalse(self.cancel.busy_login('a'))
        self.setUp(); prepared = self.sent()
        def disconnected(client, path, payload):
            if path == CANCEL_PATH: raise AppLoginError('connection lost after send')
        self.handler = disconnected
        self.assertEqual(self.cancel.confirm('a', prepared['cancel_id'], '1', '123456', True)['status'], 'unknown')
        self.assertFalse(self.service.query('a')['groups'][0]['items'][0]['cancel']['available'])

    def test_two_concurrent_confirmations_across_refreshed_action_ids_submit_one_request(self):
        first = self.sent(); second = self.prepared(); self.cancel.sms('a', second['cancel_id'], '1')
        entered, release = Event(), Event()
        def hold(client, path, payload):
            if path == CANCEL_PATH:
                entered.set(); self.assertTrue(release.wait(3))
                return 200, {'retCode': '000000'}, {}
        self.handler = hold
        with ThreadPoolExecutor(max_workers=2) as pool:
            initial = pool.submit(self.cancel.confirm, 'a', first['cancel_id'], '1', '123456', True)
            self.assertTrue(entered.wait(2))
            following = pool.submit(self.cancel.confirm, 'a', second['cancel_id'], '1', '123456', True)
            self.assertTrue(self.cancel.busy_login('a'))
            release.set()
            self.assertEqual(initial.result(), following.result())
        self.assertEqual(len(self.calls(CANCEL_PATH)), 1)

    def test_old_month_action_and_expired_operation_require_fresh_reading_without_sending(self):
        action = self.action(); self.now = self.now.replace(month=12)
        with self.assertRaises(ServicesError) as raised: self.cancel.prepare('a', action)
        self.assertEqual(raised.exception.code, 'SERVICES_ACTION_EXPIRED')
        self.setUp(); prepared = self.prepared(); self.now += timedelta(minutes=30)
        with self.assertRaises(ServicesError) as raised: self.cancel.sms('a', prepared['cancel_id'], '1')
        self.assertEqual(raised.exception.code, 'SERVICES_CANCEL_EXPIRED'); self.assertEqual(self.calls(CANCEL_SMS_PATH), [])

    def test_json_post_routes_require_owned_account_and_ignore_injected_product_parameters(self):
        app = Flask('cancel-routes')
        accounts = lambda: [{'id': 'a', 'status': 'active', 'phone_masked': '138****0000'}]
        with patch('services_query.AppLoginSession.from_credentials', self.factory):
            register_services(app, accounts, lambda key: deepcopy(self.snapshots.get(key)))
        client = app.test_client()
        self.assertEqual(client.get('/api/services/cancel/prepare').status_code, 405)
        self.assertEqual(client.post('/api/services/cancel/prepare', json=[]).status_code, 400)
        self.assertEqual(client.post('/api/services/cancel/prepare', json={'login_id': 'b', 'action_id': 'x'}).status_code, 404)
        query = client.get('/api/services/query?login_id=a').json
        action = query['groups'][0]['items'][0]['cancel']['action_id']
        prepared = client.post('/api/services/cancel/prepare', json={'login_id': 'a', 'action_id': action, 'productCode': 'injected'}).json
        self.assertEqual(prepared['account']['id'], 'a'); cancel_id = prepared['cancel_id']
        sent = client.post('/api/services/cancel/sms', json={'login_id': 'a', 'cancel_id': cancel_id, 'effective_type': '1', 'productCode': 'injected'})
        self.assertEqual(sent.status_code, 200); self.assertEqual(sent.json['retry_after'], 60)
        invalid = client.post('/api/services/cancel/confirm', json={'login_id': 'a', 'cancel_id': cancel_id, 'effective_type': '1', 'sms_code': '123456'})
        self.assertEqual(invalid.status_code, 400); self.assertEqual(self.calls(CANCEL_PATH), [])
        final = client.post('/api/services/cancel/confirm', json={'login_id': 'a', 'cancel_id': cancel_id, 'effective_type': '1', 'sms_code': '123456', 'confirmed': True, 'productCode': 'injected'})
        self.assertEqual(final.status_code, 200); self.assertEqual(final.json['status'], 'success')
        self.assertEqual(self.calls(CANCEL_PATH)[0]['productId'], 'P-private')


if __name__ == '__main__':
    unittest.main()
