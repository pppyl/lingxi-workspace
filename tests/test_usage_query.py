from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from flask import Flask

from app_login import AppLoginError
from recharge.cmcc import SHANGHAI
from usage_query import (COUNTRY_PATH, PLAN_PATH, REMAIN_PATH, SHARE_PATH, UsageError,
                         UsageQueries, normalize_usage, register_usage, _present_usage)

NOW = datetime(2026, 11, 6, 9, 45, tzinfo=SHANGHAI)
SNAPSHOT = {'phone': '13800000000', 'cookie': 'JSESSIONID=private', 'credential_revision': 1}
REMAIN = {'newPlanRemainQryRes': {
    'planRemianFlowListRes': {'limitType': '01', 'state': '01', 'limitRate': '1',
        'limitRuleRes': '流量记录可能存在延迟', 'planRemianFlowRes': [
        {'flowtype': '0', 'unit': '04', 'flowSumNum': '10.000', 'flowUsdNum': '1.250', 'flowRemainNum': '8.750',
         'planRemianFlowInfoRes': [{'planName': '通用包', 'plantype': '01', 'expireFlag': '0', 'isMultiTerm': '0',
            'validDate': '2026-11-01 00:00:00', 'expireDate': '2026-11-30 23:59:59',
            'planSpecialInfo': '手机号13800000000使用说明', 'appInfoRes': [{'appType': '适用APP', 'appTag': '视频、音乐'}],
            'businessdetailsList': [{'title': '套餐规则', 'titleValue': '结转使用',
                'businessdetails': [{'bubtitle': '范围', 'vlues': '全国'}, {'remark': '不可赠送'}]}],
            'flowInfoRes': [{'flowName': '结转流量', 'unit': '03', 'totalRes': '1024.000', 'usedRes': '0.000',
                             'remainRes': '1024.000', 'flowContinueFlag': '0', 'gATFlag': '0'}]}]},
        {'flowtype': '1', 'unit': '04', 'flowSumNum': '9999', 'flowUsdNum': '0.000', 'flowRemainNum': 'N',
         'planRemianFlowInfoRes': [{'planName': '视频定向包', 'plantype': '03',
                                  'flowInfoRes': [{'flowName': '定向流量', 'unit': '04', 'totalRes': 'N',
                                                   'usedRes': '0', 'remainRes': 'N'}]}]}],
        'outPlanInfoRes': [{'outPlanTitle': '超套流量', 'unit': '04', 'usageAmount': '0.500'}]},
    'planRemianVoiceListRes': {'planRemianVoiceInfoRes': [
        {'voicetype': '0', 'unit': '01', 'voiceSumNum': '1000', 'voiceUsdNum': '0', 'voiceRemainNum': '1000',
         'planRemianVoiceRes': [{'planName': '通话包', 'voiceInfoRes': [
             {'voiceName': '国内通话', 'unit': '01', 'totalRes': '1000', 'usedRes': '0', 'remainRes': '1000'}]}]}],
        'outPlanInfoRes': [{'outPlanTitle': '超套通话', 'unit': '01', 'usageAmount': '2.50'}]},
    'planRemianMSGInfoListRes': {'planRemianMSGRes': [
        {'mSGType': '0', 'mSGSum': '100', 'mSGuserd': '100', 'mSGresSum': '0'},
        {'mSGType': '1', 'mSGSum': 'N', 'mSGuserd': '0', 'mSGresSum': 'N'}],
        'planRemianMSGInfoRes': [{'planName': '短信包', 'msgInfoRes': [
            {'planRemianMSGName': '短信', 'unit': '02', 'totalRes': '100', 'usedRes': '100', 'remainRes': '0'}]}]},
    'planRemianOtherListRes': {'planRemianOtherInfoRes': [{'planName': '会员包', 'otherName': '会员次数',
        'unit': '05', 'totalRes': '3', 'usedRes': '0', 'remainRes': '--', 'continueFlag': '1'}]},
}}
PLANS = {'planQryInfos': [{'type': '1', 'curPlanId': 'now', 'curPlanName': '家庭套餐',
    'busiInfoList': [{'type': '1', 'busiInfo': [{'unit': '05', 'busiNum': '20.0000'}]},
                     {'type': '0', 'busiInfo': [{'unit': '01', 'busiNum': 'N'}]}],
    'reservePlanInfo': {'reservePlanId': 'next', 'reservePlanName': '下月套餐', 'busiInfomsg': '下月生效'}}]}
COUNTRY = {'flowPacketUsageInfoList': [{'productName': '境外包', 'allFlows': '2048.1234', 'usedFlows': '1.0001',
    'leftFlows': '2047.1233', 'allVoice': '20', 'usedVoice': '0', 'leftVoice': '20',
    'alldays': '5', 'usedDays': '0', 'leftDays': '5', 'prodeffTIMSI': '20261101000000',
    'prodexpireTIMSI': '20261130235959', 'prodfirstTIMSI': '20261102090000', 'prodendTime': '20261107235959', 'prodStat': '02'}],
    'standardTariffUsageInfoList': [{'visitDate': '20261104', 'visitInfoList': [
        {'standardProductName': '漫游标准资费', 'visitFlows': '0.0010'}]}]}


class UsageQueryTests(unittest.TestCase):
    def test_used_meter_ratio_uses_source_units_before_display_conversion(self):
        item={'unit':'MB','total':'10240','used':'512','remaining':'9728'}
        _present_usage(item)
        self.assertEqual(item['display']['total'],{'value':'10','unit':'GB'})
        self.assertEqual(item['display']['used'],{'value':'512','unit':'MB'})
        self.assertEqual(item['used_percent'],'5.00')
        self.assertEqual(item['used'],'512')

    def test_used_meter_distinguishes_zero_usage_from_unknown_or_unlimited(self):
        for total,used,unlimited in ((None,'5',False),('100',None,False),('0','0',False),('100','101',False),('100','0',True)):
            item={'unit':'分钟','total':total,'used':used,'remaining':None,'unlimited':unlimited}
            _present_usage(item)
            self.assertIsNone(item['used_percent'])
        for used,expected in (('0','0.00'),('100','100.00')):
            item={'unit':'条','total':'100','used':used,'remaining':None}
            _present_usage(item)
            self.assertEqual(item['used_percent'],expected)

    def test_display_units_are_readable_without_changing_official_precision(self):
        item={'unit':'MB','total':'30720.00','used':'0.00','remaining':'30720.00','children':[]}
        _present_usage(item)
        self.assertEqual(item['display']['total'],{'value':'30','unit':'GB'})
        self.assertEqual(item['display']['used'],{'value':'0','unit':'MB'})
        self.assertEqual(item['remaining_percent'],'100.00')
        self.assertEqual(item['remaining'],'30720.00')
        partial={'unit':'MB','total':'10240.00','used':'3573.64','remaining':'6666.36'}
        _present_usage(partial)
        self.assertEqual(partial['display']['used'],{'value':'3.48','unit':'GB'})
        self.assertEqual(partial['display']['remaining'],{'value':'6.51','unit':'GB'})
        self.assertEqual(partial['remaining_percent'],'65.10')
        self.assertEqual(partial['used'],'3573.64')

    def test_display_preserves_tiny_allowances_and_does_not_invent_wrapper_totals(self):
        child={'unit':'GB','total':'0.0001','used':'0','remaining':'0.0001'}
        wrapper={'unit':'','total':None,'used':None,'remaining':None,'children':[child]}
        _present_usage(wrapper)
        self.assertEqual(wrapper['display'],{})
        self.assertIsNone(wrapper['remaining_percent'])
        self.assertEqual(child['display']['remaining'],{'value':'104.85','unit':'KB'})
        tiny={'unit':'MB','used':'0.00000001','remaining':None,'total':None}
        _present_usage(tiny)
        self.assertEqual(tiny['display']['used'],{'value':'<0.01','unit':'KB'})
        self.assertEqual(tiny['used'],'0.00000001')

    def test_progress_requires_a_finite_consistent_official_total_and_remainder(self):
        for total,remaining,unlimited in ((None,'5',False),('0','0',False),('10','11',False),('10','-1',False),('10','5',True)):
            item={'unit':'MB','total':total,'used':'0','remaining':remaining,'unlimited':unlimited}
            _present_usage(item)
            self.assertIsNone(item['remaining_percent'])
        zero={'unit':'条','total':'100','used':'100','remaining':'0'}
        _present_usage(zero)
        self.assertEqual(zero['display']['remaining'],{'value':'0','unit':'条'})
        self.assertEqual(zero['remaining_percent'],'0.00')

    def make_service(self, **kwargs):
        flow = SimpleNamespace(cookie=SNAPSHOT['cookie'])
        def call(path, payload):
            return 200, {'retCode': '000000', 'rspBody': deepcopy(
                {REMAIN_PATH: REMAIN, PLAN_PATH: PLANS, COUNTRY_PATH: COUNTRY, SHARE_PATH: {}}[path])}, {}
        flow.call = Mock(side_effect=call)
        factory = Mock(return_value=flow)
        service = UsageQueries(lambda _: deepcopy(SNAPSHOT), app_factory=factory, now=lambda: NOW, **kwargs)
        return service, flow, factory

    def test_all_protocol_payloads_share_connection_and_repeated_click_queries_fresh(self):
        service, flow, factory = self.make_service()
        transports, original = [], flow.call.side_effect
        def call(path, payload):
            transports.append(flow.transport)
            return original(path, payload)
        flow.call.side_effect = call
        with patch('usage_query.requests.Session') as create:
            first, second = Mock(), Mock()
            create.return_value.__enter__.side_effect = [first, second]
            service.query('a'); service.query('b')
        self.assertEqual(transports, [first]*4+[second]*4)
        self.assertEqual(factory.call_count, 2)
        self.assertEqual(create.return_value.__exit__.call_count, 2)
        self.assertEqual([entry.args for entry in flow.call.call_args_list[:4]], [
            (REMAIN_PATH, {'cellNum': SNAPSHOT['phone']}), (PLAN_PATH, {'cellNum': SNAPSHOT['phone']}),
            (COUNTRY_PATH, {'cellNum': SNAPSHOT['phone'], 'currentPeriodStartTm': '20261101000000',
                            'currentPeriodEndTm': '20261130235959'}), (SHARE_PATH, {'cellNum': SNAPSHOT['phone']})])

    def test_shared_usage_uses_kb_mb_gb_units_and_subcard_shows_only_selected_member(self):
        detail = {'resourcesCode': '04', 'planName': '家庭共享包', 'unit': '04', 'shareTotalRes': '20000',
                  'shareUsedRes': '18000.500', 'shareRemainRes': '1999.500', 'shareUsedResInfo': [
                      {'usedTerm': SNAPSHOT['phone'], 'unit': '03', 'usedRes': '1024.000'},
                      {'usedTerm': '13900000000', 'unit': '05', 'usedRes': '1.50'}]}
        aggregate = {'resourcesCodeAll': '04', 'unit': '05', 'shareUsedRes': '1.5000', 'allShareDetailInfo': [
            {'usedTerms': SNAPSHOT['phone'], 'unit': '05', 'usedResSum': '0.0000'},
            {'usedTerms': '13900000000', 'unit': '04', 'usedResSum': '1536.000'}]}
        share = {'userShareInfoRes': [{'cardstate': '0', 'shareDetailInfo': [detail], 'allUserShareInfo': [aggregate]}]}
        main = normalize_usage({'newPlanRemainQryRes': {}}, {}, {}, NOW, share=share, phone=SNAPSHOT['phone'])['groups'][0]
        self.assertEqual(main['name'], '共享主卡用量')
        row = main['items'][0]
        self.assertEqual(row['unit'], 'MB'); self.assertFalse(row['unlimited'])
        self.assertEqual(row['total'], '20000'); self.assertEqual(row['remaining'], '1999.500')
        self.assertEqual([child['unit'] for child in row['children']], ['KB', 'GB'])
        self.assertEqual(row['children'][0]['name'], '138****0000')
        self.assertEqual(main['items'][1]['used'], '1.5000')
        self.assertNotIn(SNAPSHOT['phone'], str(main)); self.assertNotIn('13900000000', str(main))
        share['userShareInfoRes'][0]['cardstate'] = '1'
        sub = normalize_usage({'newPlanRemainQryRes': {}}, {}, {}, NOW, share=share, phone=SNAPSHOT['phone'])['groups'][0]
        self.assertEqual(sub['name'], '共享副卡用量')
        self.assertEqual(len(sub['items'][0]['children']), 1)
        self.assertEqual(len(sub['items'][1]['children']), 1)
        self.assertEqual(sub['items'][1]['children'][0]['used'], '0.0000')

    def test_fields_scopes_precision_zero_and_notes_preserve_native_meaning(self):
        result = normalize_usage(REMAIN, PLANS, COUNTRY, NOW)
        common, directed, voice, sms, mms = result['overview']
        self.assertEqual([common[key] for key in ('unit', 'total', 'used', 'remaining')],
                         ['MB', '10240.000', '1792.000', '8960.000'])
        self.assertEqual(voice['used'], '2.50')
        self.assertEqual(sms['remaining'], '0')
        self.assertEqual(sms['used'], '100')
        for row in (directed, mms):
            self.assertTrue(row['unlimited']); self.assertIsNone(row['remaining']); self.assertIsNone(row['total'])
        detail = result['groups'][0]['items'][0]
        child = detail['children'][0]
        self.assertEqual(child['remaining'], '1024.000'); self.assertEqual(child['used'], '0.000')
        self.assertEqual(child['unit'], 'MB')
        for label in ('国内通用流量', '可共享', '上月结转', '国内(含港澳台)'):
            self.assertIn(label, child['scope'])
        self.assertEqual(detail['start_at'], '2026-11-01 00:00:00')
        self.assertEqual(detail['end_at'], '2026-11-30 23:59:59')
        for text in ('套餐规则', '结转使用', '范围：全国', '不可赠送', '视频、音乐'):
            self.assertIn(text, detail['description'])
        self.assertNotIn(SNAPSHOT['phone'], str(result))
        self.assertIn('当前流量已限速：1Mbps', result['notes'])
        self.assertEqual(result['queried_at'], NOW.isoformat())

    def test_auxiliary_interfaces_keep_their_own_unit_dictionary_and_never_supply_fake_remainders(self):
        result = normalize_usage(REMAIN, PLANS, COUNTRY, NOW)
        package = next(group for group in result['groups'] if group['name'] == '套餐信息')['items'][0]
        self.assertEqual(package['name'], '家庭套餐')
        self.assertEqual(package['children'][0]['unit'], 'GB')
        self.assertEqual(package['children'][0]['total'], '20.0000')
        self.assertIsNone(package['children'][0]['remaining'])
        self.assertTrue(package['children'][1]['unlimited'])
        self.assertEqual(package['children'][2]['name'], '下月套餐')
        international = result['groups'][-1]['items']
        self.assertEqual(international[0]['children'][0]['total'], '2048.1234')
        self.assertEqual(international[0]['children'][0]['unit'], 'MB')
        self.assertEqual(international[0]['children'][1]['used'], '0')
        self.assertEqual(international[0]['children'][2]['unit'], '天')
        self.assertIn('激活时间：20261102090000', international[0]['description'])
        self.assertEqual(international[1]['used'], '0.0010')
        self.assertIsNone(international[1]['total'])
        other = next(group for group in result['groups'] if group['name'] == '其他资源')['items'][0]
        self.assertIsNone(other['remaining']); self.assertEqual(other['used'], '0')

    def test_empty_invalid_and_special_day_lifetime_data_are_explicit(self):
        result = normalize_usage({'newPlanRemainQryRes': {}}, {}, {}, NOW)
        self.assertEqual(len(result['overview']), 3)
        self.assertTrue(all(row['remaining'] is None for row in result['overview']))
        self.assertIn('官方暂未返回余量明细。', result['notes'])
        for body in ({}, {'newPlanRemainQryRes': {'planRemianVoiceListRes': ['bad']}},
                     {'newPlanRemainQryRes': {'planRemianFlowListRes': {'planRemianFlowRes': {1: 'bad'}}}}):
            with self.subTest(body=body), self.assertRaises(UsageError):
                normalize_usage(body, {}, {}, NOW)
        body = deepcopy(REMAIN)
        plan = body['newPlanRemainQryRes']['planRemianFlowListRes']['planRemianFlowRes'][0]['planRemianFlowInfoRes'][0]
        plan['plantype'] = '06'
        plan['planParaInfoRes'] = {'unit': '04', 'totalRes': '10', 'usedRes': '3.50'}
        plan['dayPlanParaInfoRes'] = {'unit': '03', 'totalRes': '500', 'usedRes': '0', 'remainRes': '500'}
        item = normalize_usage(body, {}, {}, NOW)['groups'][0]['items'][0]
        self.assertEqual([child['name'] for child in item['children']], ['结转流量（本月）', '当日用量', '产品累计用量'])
        self.assertIsNone(item['total'], 'overlapping child quotas must never be added')

    def test_concurrent_same_account_is_merged_other_accounts_remain_isolated(self):
        service, flow, _ = self.make_service()
        entered, release, joined = Event(), Event(), Event()
        original = flow.call.side_effect
        def slow(path, payload):
            if path == REMAIN_PATH:
                entered.set(); self.assertTrue(release.wait(3))
            return original(path, payload)
        flow.call.side_effect = slow
        with ThreadPoolExecutor(max_workers=3) as pool:
            first = pool.submit(service.query, 'a'); self.assertTrue(entered.wait(2))
            pending = service.pending['a']
            original_result = pending.result
            def wait_for_leader():
                joined.set()
                return original_result()
            pending.result = wait_for_leader
            second = pool.submit(service.query, 'a'); self.assertTrue(joined.wait(2))
            other = pool.submit(service.query, 'b')
            release.set()
            result = first.result(); self.assertEqual(result, second.result()); other.result()
        self.assertEqual(sum(call.args[0] == REMAIN_PATH for call in flow.call.call_args_list), 2)
        result['groups'].clear(); self.assertTrue(second.result()['groups'])

    def test_selected_phone_cookie_and_client_never_leak_between_accounts(self):
        snapshots = {'a': deepcopy(SNAPSHOT), 'b': {**SNAPSHOT, 'phone': '13900000000', 'cookie': 'JSESSIONID=second'}}
        clients = []
        def factory(snapshot):
            flow = SimpleNamespace(cookie=snapshot['cookie'], phone=snapshot['phone'])
            def call(path, payload):
                self.assertEqual(payload['cellNum'], flow.phone)
                self.assertEqual(flow.cookie, snapshots['a' if flow.phone == SNAPSHOT['phone'] else 'b']['cookie'])
                return 200, {'retCode': '000000', 'rspBody': {'newPlanRemainQryRes': {}} if path == REMAIN_PATH else {}}, {}
            flow.call = Mock(side_effect=call); clients.append(flow)
            return flow
        service = UsageQueries(lambda login_id: deepcopy(snapshots[login_id]), app_factory=factory, now=lambda: NOW)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(service.query, 'a'); second = pool.submit(service.query, 'b')
            first.result(); second.result()
        self.assertEqual({client.phone for client in clients}, {SNAPSHOT['phone'], '13900000000'})
        self.assertIsNot(clients[0].transport, clients[1].transport)
        self.assertEqual([client.call.call_count for client in clients], [4, 4])

    def test_auth_and_missing_cookie_restore_once_with_latest_same_account_snapshot(self):
        recover = Mock(); service, flow, factory = self.make_service(recover=recover)
        original, calls = flow.call.side_effect, 0
        fresh = {**SNAPSHOT, 'cookie': 'JSESSIONID=fresh', 'credential_revision': 2}
        flow.cookie = fresh['cookie']
        def expired(path, payload):
            nonlocal calls
            calls += 1
            return (200, {'retCode': '410000'}, {}) if calls == 1 else original(path, payload)
        flow.call.side_effect = expired
        service.credentials = Mock(side_effect=[deepcopy(SNAPSHOT), deepcopy(fresh)])
        service.query('a')
        recover.assert_called_once_with(SNAPSHOT)
        self.assertEqual(factory.call_args_list[-1].args[0], fresh)
        service.credentials = Mock(side_effect=[{**SNAPSHOT, 'cookie': ''}, deepcopy(fresh)])
        flow.call.side_effect = original
        service.query('a')
        self.assertEqual(recover.call_count, 2)
        service.credentials = lambda _: deepcopy(SNAPSHOT)
        flow.call.side_effect = lambda *_: (200, {'retCode': '400007'}, {})
        with self.assertRaises(UsageError) as raised:
            service.query('a')
        self.assertEqual(raised.exception.status, 401)
        self.assertEqual(recover.call_count, 3, 'no request may restore twice')

    def test_recovery_cannot_switch_phone_and_never_sends_another_accounts_query(self):
        service, flow, factory = self.make_service(recover=Mock())
        service.credentials = Mock(side_effect=[{**SNAPSHOT, 'cookie': ''}, {**SNAPSHOT, 'phone': '13900000000'}])
        with self.assertRaises(UsageError):
            service.query('a')
        factory.assert_not_called(); flow.call.assert_not_called()

    def test_rate_business_format_and_network_failures_never_trigger_login(self):
        recover = Mock(); service, flow, _ = self.make_service(recover=recover)
        for code in ('400003', '-800', '8053', '228000'):
            flow.call.reset_mock(); flow.call.side_effect = lambda *_, code=code: (200, {'retCode': code}, {})
            with self.subTest(code=code), self.assertRaises(UsageError) as raised:
                service.query('a')
            self.assertEqual(flow.call.call_count, 1)
            self.assertEqual(raised.exception.status, 429 if code in ('-800', '8053') else 502)
        for body in ('not JSON', ['bad']):
            flow.call.side_effect = lambda *_, body=body: (200, {'retCode': '000000', 'rspBody': body}, {})
            with self.assertRaises(UsageError) as raised:
                service.query('a')
            self.assertEqual(raised.exception.code, 'USAGE_FORMAT')
        flow.call.side_effect = AppLoginError('network failure')
        with self.assertRaises(UsageError) as raised:
            service.query('a')
        self.assertEqual(raised.exception.code, 'USAGE_NETWORK')
        recover.assert_not_called()

    def test_secondary_failure_keeps_primary_usage_and_stops_on_official_rate_limit(self):
        recover = Mock(); service, flow, _ = self.make_service(recover=recover)
        original = flow.call.side_effect
        def limited(path, payload):
            return (200, {'retCode': '-800'}, {}) if path == PLAN_PATH else original(path, payload)
        flow.call.side_effect = limited
        result = service.query('a')
        self.assertEqual(result['overview'][0]['remaining'], '8960.000')
        self.assertIn('套餐说明未更新', result['notes'][-1])
        self.assertEqual(flow.call.call_count, 2); recover.assert_not_called()

    def test_cookie_rotation_returns_to_shared_storage_without_relogin(self):
        accept = Mock(); service, flow, _ = self.make_service(accept_credentials=accept)
        flow.cookie = 'JSESSIONID=rotated'
        service.query('a'); accept.assert_called_once_with(SNAPSHOT, flow)

    def test_route_validates_one_shared_account_and_does_not_query_on_registration(self):
        app = Flask('usage-routes'); provider = Mock(return_value=deepcopy(SNAPSHOT))
        accounts = lambda: [{'id': 'a', 'status': 'active', 'phone_masked': '138****0000'}]
        register_usage(app, accounts, provider)
        query = app.extensions['usage_queries']; query.query = Mock(return_value={'overview': [], 'groups': [], 'notes': []})
        provider.assert_not_called(); query.query.assert_not_called()
        client = app.test_client()
        self.assertEqual(client.get('/api/usage/query?login_id=other').status_code, 404)
        response = client.get('/api/usage/query?login_id=a')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['account']['id'], 'a'); query.query.assert_called_once_with('a')
        query.query.side_effect = UsageError('请重新登录', 'USAGE_LOGIN_REQUIRED', 401)
        response = client.get('/api/usage/query?login_id=a')
        self.assertEqual(response.status_code, 401); self.assertEqual(response.json['code'], 'USAGE_LOGIN_REQUIRED')


if __name__ == '__main__':
    unittest.main()
