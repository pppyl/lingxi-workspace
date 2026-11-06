from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from flask import Flask
from jinja2 import DictLoader

from app_login import AppLoginError
from bill_query import BillError, BillQueries, BILLS_PATH, BALANCE_PATH, normalize_bill, register_bills
from usage_query import register_usage
from services_query import register_services
from feature_control import FeatureControl
from recharge.cmcc import SHANGHAI

NOW = datetime(2026, 11, 6, 9, 45, tzinfo=SHANGHAI)
SNAPSHOT = {'phone':'13800000000','cookie':'JSESSIONID=private','credential_revision':1}
BILL = {'inBill':'202611','billCycleStartDate':'2026-11-01','billCycleEndDate':'2026-11-06',
        'totalBill':'100.00','reductionBill':'-30.00','realConsumeBill':'70.00','realPayBill':'60.00',
        'billPayInfoMsgRes':{'billPaysum':'-10.00','payInfoListRes':[
            {'billPayname':'4','billPaytypesum':'-10.00','payInfoRes':[
                {'billPayCode':'2','billPayInfoSum':'10.00'}]}]},
        'billSaveInfoRes':{'billSaveSum':'40.00','saveInfoMsgRes':[{'billSaveName':'套餐优惠','billSaveValue':'40.00'}]},
        'billMaterialRes':[{'billEntries':'01','billEntriesOPValue':'100.00','billEntriesSaveValue':'-30.00',
            'billEntriesValue':'70.00','thirdBillMaterialInfo':[{'thirdItemsName':'基础套餐',
                'thirdItemsValue':'100.00','thirdItemsSaveValue':'-30.00','thirdItemsRealValue':'70.00'}]}],
        'billInfoAllRes':[{'allRealValue':'80.00','allRaalPay':'65.00'}],
        'billInfoMsg':'号码13800000000的合账说明%实时费用仅供参考'}


class BillQueryTests(unittest.TestCase):
    def test_single_unnamed_payment_detail_can_render_compactly_without_losing_sign_or_source(self):
        body={'historyBillInfo':[{'inBill':'202611','billPayInfoMsgRes':{'payInfoListRes':[
            {'billPayname':'4','billPaytypesum':'-20.10','payInfoRes':[{'billPayCode':'2','billPayInfoSum':'20.10'}]},
        ]}}]}
        item=normalize_bill(body,{},'202611',NOW)['sections'][0]['items'][0]
        self.assertTrue(item['compact_details'])
        self.assertEqual(item['name'],'活动返费');self.assertEqual(item['amount'],'-20.10')
        self.assertEqual(item['children'][0]['name'],'返费项目 2')
        self.assertEqual(item['children'][0]['amount'],'20.10')

    def test_distinct_names_amounts_or_multiple_payment_details_remain_expandable(self):
        for details in (
            [{'billPayName':'套餐优惠返费','billPayInfoSum':'20.10'}],
            [{'billPayCode':'2','billPayInfoSum':'15.00'}],
            [{'billPayCode':'1','billPayInfoSum':'10.00'},{'billPayCode':'2','billPayInfoSum':'10.10'}],
            [{'billPayCode':'2','billPayInfoSum':None}],
        ):
            with self.subTest(details=details):
                body={'historyBillInfo':[{'inBill':'202611','billPayInfoMsgRes':{'payInfoListRes':[
                    {'billPayname':'4','billPaytypesum':'-20.10','payInfoRes':details},
                ]}}]}
                item=normalize_bill(body,{},'202611',NOW)['sections'][0]['items'][0]
                self.assertFalse(item.get('compact_details',False));self.assertEqual(len(item['children']),len(details))

    def test_bill_and_balance_share_one_connection_but_next_query_is_isolated(self):
        service,flow,_=self.make_service()
        original=flow.call.side_effect;transports=[]
        def record(path,payload):
            transports.append(flow.transport)
            return original(path,payload)
        flow.call.side_effect=record
        with patch('bill_query.requests.Session') as create:
            first,second=Mock(),Mock()
            create.return_value.__enter__.side_effect=[first,second]
            service.query('a','202611');service.query('b','202611')
            self.assertEqual(create.call_count,2)
            self.assertEqual(create.return_value.__exit__.call_count,2)
        self.assertEqual(transports,[first,first,second,second])

    def make_service(self, **kwargs):
        flow=SimpleNamespace(cookie=SNAPSHOT['cookie'])
        def call(path, payload):
            body={'historyBillInfo':[deepcopy(BILL)]} if path==BILLS_PATH else {'curFee':'50.00','oweFee':'0.00'}
            return 200, {'retCode':'000000','rspBody':body}, {}
        flow.call=Mock(side_effect=call)
        factory=Mock(return_value=flow)
        service=BillQueries(lambda _:deepcopy(SNAPSHOT), app_factory=factory, now=lambda:NOW, **kwargs)
        return service,flow,factory

    def test_native_payload_and_each_active_query_reuses_cookie_without_initialization(self):
        service,flow,factory=self.make_service()
        first=service.query('a','202611');second=service.query('a','202611')
        self.assertEqual(first['payable'],'60.00')
        self.assertEqual(second['balance'],'50.00')
        self.assertEqual(factory.call_count,2,'separate user clicks query fresh data')
        for call in flow.call.call_args_list[::2]:
            self.assertEqual(call.args,(BILLS_PATH,{'cellNum':SNAPSHOT['phone'],'bgnMonth':'2026-11','endMonth':'2026-11'}))
        self.assertEqual([call.args[0] for call in flow.call.call_args_list],[BILLS_PATH,BALANCE_PATH]*2)

    def test_money_mapping_preserves_detail_sign_zero_missing_and_official_notes(self):
        result=normalize_bill({'historyBillInfo':[deepcopy(BILL)]},{'curFee':'0'},'202611',NOW)
        self.assertEqual([result[k] for k in ('total','payable','original','discount','rebate','balance')],
                         ['70.00','60.00','100.00','30.00','10.00','0.00'])
        item=result['items'][0]
        self.assertEqual(item['name'],'套餐及固定费')
        self.assertEqual(item['discount'],'-30.00')
        self.assertEqual(item['children'][0]['amount'],'70.00')
        self.assertEqual(result['sections'][0]['items'][0]['name'],'活动返费')
        self.assertEqual(result['sections'][1]['items'][0]['amount'],'40.00')
        self.assertNotIn(SNAPSHOT['phone'],str(result))
        result=normalize_bill({'historyBillInfo':[{'inBill':'202611','realPayBill':'--'}]}, {}, '202611', NOW)
        self.assertIsNone(result['payable']);self.assertIsNone(result['balance'])
        self.assertEqual(result['items'],[])

    def test_query_window_rolls_over_year_and_rejects_future_or_old_dates_before_network(self):
        service,flow,_=self.make_service()
        self.assertEqual(service.months()[0],'202611');self.assertEqual(service.months()[-1],'202511')
        for month in (None,'2026-11','202612','202510','../../private'):
            with self.subTest(month=month),self.assertRaises(BillError):service.query('a',month)
        flow.call.assert_not_called()

    def test_no_data_is_explicit_and_a_wrong_month_never_appears_under_selected_month(self):
        result=normalize_bill({'historyBillInfo':[]},{},'202611',NOW)
        self.assertIn('暂未返回',result['message']);self.assertIsNone(result['payable'])
        with self.assertRaises(BillError) as raised:normalize_bill({'historyBillInfo':[BILL]}, {}, '202610', NOW)
        self.assertEqual(raised.exception.code,'BILLS_MONTH_MISMATCH')

    def test_concurrent_same_account_month_is_merged_and_other_accounts_are_isolated(self):
        service,flow,_=self.make_service()
        entered,release=Event(),Event();original=flow.call.side_effect
        def slow(path,payload):
            if path==BILLS_PATH:
                entered.set();self.assertTrue(release.wait(3))
            return original(path,payload)
        flow.call.side_effect=slow
        with ThreadPoolExecutor(max_workers=3) as executor:
            first=executor.submit(service.query,'a','202611');self.assertTrue(entered.wait(2))
            second=executor.submit(service.query,'a','202611')
            other=executor.submit(service.query,'b','202611')
            release.set()
            result=first.result();self.assertEqual(second.result(),result);other.result()
        self.assertEqual(sum(call.args[0]==BILLS_PATH for call in flow.call.call_args_list),2)
        result['items'].clear();self.assertTrue(second.result()['items'],'callers receive isolated data')

    def test_auth_rejection_recovers_once_and_reads_latest_shared_credentials(self):
        recover=Mock();service,flow,_=self.make_service(recover=recover)
        normal=flow.call.side_effect;calls=0
        def expired(path,payload):
            nonlocal calls
            calls+=1
            return (200,{'retCode':'410000'}, {}) if calls==1 else normal(path,payload)
        flow.call.side_effect=expired
        self.assertEqual(service.query('a','202611')['payable'],'60.00');recover.assert_called_once_with(SNAPSHOT)
        flow.call.side_effect=lambda *_:(200,{'retCode':'400007'}, {})
        with self.assertRaises(BillError) as raised:service.query('a','202611')
        self.assertEqual(raised.exception.status,401)
        self.assertEqual(recover.call_count,2,'one recovery in each request, no retry loop')

    def test_format_rejection_and_rate_limit_never_trigger_login_or_retry(self):
        recover=Mock();service,flow,_=self.make_service(recover=recover)
        for code in ('400003','-800','8053'):
            flow.call.reset_mock();flow.call.side_effect=lambda *_,code=code:(200,{'retCode':code,'retDesc':'请求未完成'}, {})
            with self.assertRaises(BillError) as raised:service.query('a','202611')
            self.assertEqual(flow.call.call_count,1)
            if code!='400003':self.assertIn('尝试更换服务ip再试',str(raised.exception))
        recover.assert_not_called()

    def test_secondary_balance_failure_keeps_bill_and_does_not_reauthenticate(self):
        recover=Mock();service,flow,_=self.make_service(recover=recover);normal=flow.call.side_effect
        def fail(path,payload):
            if path==BALANCE_PATH:raise AppLoginError('transport failed')
            return normal(path,payload)
        flow.call.side_effect=fail
        result=service.query('a','202611')
        self.assertEqual(result['payable'],'60.00');self.assertIsNone(result['balance'])
        self.assertIn('余额未更新',result['notes'][-1]);recover.assert_not_called()

    def test_app_cookie_rotation_is_returned_to_shared_storage_without_login(self):
        accept=Mock();service,flow,_=self.make_service(accept_credentials=accept)
        flow.cookie='JSESSIONID=rotated'
        service.query('a','202611');accept.assert_called_once_with(SNAPSHOT,flow)

    def test_routes_have_no_background_queries_and_feature_switch_blocks_read_requests(self):
        app=Flask('bill-routes');features=FeatureControl(app)
        app.jinja_loader=DictLoader({'bills.html':'信息查询','feature_disabled.html':'{{ feature_title }}已关闭'})
        provider=Mock(return_value=deepcopy(SNAPSHOT))
        accounts=lambda:[{'id':'a','status':'active','name':'test','phone_masked':'138****0000'}]
        register_bills(app,accounts,provider)
        register_usage(app,accounts,provider)
        register_services(app,accounts,provider)
        query=app.extensions['bill_queries'];query.query=Mock(return_value={'month':'202611','items':[]})
        usage=app.extensions['usage_queries'];usage.query=Mock(return_value={'overview':[],'groups':[]})
        services=app.extensions['services_queries'];services.query=Mock(return_value={'groups':[]})
        client=app.test_client()
        for path in ('/information','/bills'):
            self.assertEqual(client.get(path).get_data(as_text=True),'信息查询')
        self.assertEqual(client.get('/api/bills/accounts').status_code,200)
        query.query.assert_not_called();provider.assert_not_called()
        self.assertEqual(client.get('/api/bills/query?login_id=other&month=202611').status_code,404)
        self.assertEqual(client.get('/api/bills/query?login_id=a&month=202611').status_code,200)
        self.assertEqual(client.get('/api/usage/query?login_id=a').status_code,200)
        self.assertEqual(client.get('/api/services/query?login_id=a').status_code,200)
        features.set_enabled('bills',False)
        self.assertEqual(client.get('/api/bills/query?login_id=a&month=202611').status_code,423)
        self.assertEqual(client.get('/api/usage/query?login_id=a').status_code,423)
        self.assertEqual(client.get('/api/services/query?login_id=a').status_code,423)
        for path in ('/information','/bills'):
            self.assertEqual(client.get(path).get_data(as_text=True),'信息查询已关闭')
        query.query.assert_called_once_with('a','202611')
        usage.query.assert_called_once_with('a')
        services.query.assert_called_once_with('a')


if __name__=='__main__':
    unittest.main()
