"""Port regression vectors and mocked New Youth/AI requests, without live data."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock,patch
from urllib.parse import urlsplit,parse_qs

from http_transport import requests,RequestException
from mobile_rights_gateway import API_PATH,API_PATHS,GatewayError,page_signature,request_params
from mobile_rights_points import PointClaim,PointClaimError,QUERY_PATH,CLAIM_PATH
from mobile_rights_youth import NewYouthClient,XQNError
from tests.test_mobile_rights import Response

PROFILE={'parameter_name':'6daEXCun','compression_flag':1,'inner_key_hex':bytes(range(16)).hex(),
         'config32_hex':bytes(range(32)).hex(),'outer_header_hex':'',
         'record':{'flags':51,'request_type':1,'build_id':1,'config_number':0x12345678,'sequence':42,'charset_hex':''}}
REJECT={'X_RESULTCODE':'-1','X_RESULTINFO':'尊敬的用户，请使用安全方式进行访问噢！'}


class ExtraProtocolTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.addCleanup(self.folder.cleanup)
        self.config=Path(self.folder.name)/'gateway.json'
        self.config.write_text(json.dumps({'schema_version':2,'profiles':{path:deepcopy(PROFILE) for path in API_PATHS}}),encoding='utf-8')
        setting=patch('mobile_rights_gateway.CONFIG_PATH',self.config);setting.start();self.addCleanup(setting.stop)
        self.ss=requests.Session();self.addCleanup(self.ss.close);self.ss.cookies.set('WAP_SID','synthetic-auth',domain='wap.gz.10086.cn',path='/waps')
    def test_gateway_output_matches_independent_original_synthetic_vector(self):
        result=request_params(API_PATH,{'pageName':'index.html','channel':'E004','ajax_randomcode':'0.1234567890123456'})
        expected='Rp98IGlqEqqrqGWlrA3ocq9tcaaJkGZAlpEsmrVKmSGRDf7QopgI3lyci6ERIqBPcFYOZpAAWcI._H9rPHBEVI5WMHpPXUe29wKiTrvST3ILOx2H.NbR5N0qB_xvpq8ZIH6V0fDnS4K_vLLqzVX9viAnQsUc9rBsuVCZGezjSPAEqq12Gf96PG'
        self.assertEqual(result,{'6daEXCun':expected})
    def test_python_page_signature_matches_reference_des_for_member_and_point_pages(self):
        vectors={
            'https://wap.gz.10086.cn/waps/static/activity/pointClaim/index.html':'5aaf0abfefb7e076b8af5ad9906f5586dc02e40de1a8bc8487dab8a3ed8db4bc',
            'https://wap.gz.10086.cn/waps/static/activity/apRight/verticalClassVip/index.html?actId=N2KEQ0':'455f55aca2e4b39d0b51469dc1c9979002c5a8877ad430900165118bbecd3082'}
        for url,expected in vectors.items():self.assertEqual(hashlib.sha256(page_signature(url).encode()).hexdigest(),expected)
    def test_path_and_nonce_are_part_of_the_encrypted_payload_and_wrapping_cannot_repeat(self):
        business={'ajax_randomcode':'0.123','AbleDrawScore':'10'}
        query=request_params(QUERY_PATH,business,query_prefix='undefined&')
        claim=request_params(CLAIM_PATH,business)
        self.assertNotEqual(query,claim)
        with self.assertRaises(GatewayError):request_params(API_PATH,{'6daEXCun':'already-encrypted','ajax_randomcode':'0.123'})
    def test_youth_query_and_explicit_sms_pin_the_selected_coupon_and_confirmation(self):
        self.ss.get=Mock(side_effect=[Response({'X_RESULTCODE':'0','verticalClassVips':[{'vId':'member','productName':'青年会员','apRightCoupons':[{'id':'coupon'}]}]}),Response({'X_RESULTCODE':'0'}),Response({'X_RESULTCODE':'0','resultCode':'0000'})])
        youth=NewYouthClient(self.ss)
        self.assertEqual(youth.getInfo_xqn(),[{'v_id':'member','coupon_id':'coupon','product_name':'青年会员'}])
        self.assertEqual(self.ss.get.call_count,1,'只读查询不能自动发短信或领取')
        with self.assertRaises(XQNError):youth.send_sms('member','foreign')
        youth.send_sms('member','coupon');self.assertEqual(self.ss.get.call_count,2)
        with self.assertRaises(XQNError):youth.lq_xqn('123456','member','different')
        self.assertEqual(youth.lq_xqn('123456','member','coupon'),'新青年瑞幸领取成功')
        with self.assertRaises(XQNError):youth.lq_xqn('123456','member','coupon')
        self.assertEqual(self.ss.get.call_count,3)
    def test_youth_fallback_keeps_raw_query_compatible_with_curl_without_reordering_or_new_nonce(self):
        self.ss.get=Mock(side_effect=[Response(REJECT),Response({'X_RESULTCODE':'0','verticalClassVips':[]})])
        self.assertEqual(NewYouthClient(self.ss).getInfo_xqn(),[])
        first,second=self.ss.get.call_args_list
        self.assertIn('6daEXCun',first.kwargs['params']);self.assertIsNone(second.kwargs['params'])
        self.assertTrue(urlsplit(second.args[0]).query.startswith('undefined&'))
        params=parse_qs(urlsplit(second.args[0]).query)
        self.assertIn(params['ajax_randomcode'][0]+'=0',second.kwargs['headers']['Cookie'])
        self.assertEqual(first.kwargs['headers']['Cookie'],second.kwargs['headers']['Cookie'])
    def test_sms_network_uncertainty_consumes_cooldown_without_automatic_resend(self):
        youth=NewYouthClient(self.ss);youth._coupons={('member','coupon'):{}}
        self.ss.get=Mock(side_effect=RequestException('lost'))
        with self.assertRaises(XQNError):youth.send_sms('member','coupon')
        with self.assertRaises(XQNError) as raised:youth.send_sms('member','coupon')
        self.assertEqual(raised.exception.code,429);self.assertGreater(raised.exception.retry_after,0);self.ss.get.assert_called_once()
        self.assertIsNone(youth._selected)
    def test_points_read_does_not_claim_and_claim_uses_server_queried_amount_once(self):
        self.ss.get=Mock(side_effect=[Response({'X_RESULTCODE':'0','AbleDrawScore':'12.50','TotalBalance':'25'}),Response({'X_RESULTCODE':'0','RetVal':'Y'})])
        points=PointClaim(self.ss);info=points.get_info()
        self.assertEqual(info['pending_amount'],'12.50');self.assertTrue(info['can_claim']);self.assertEqual(self.ss.get.call_count,1)
        with self.assertRaises(PointClaimError):points.claim('not-this-query')
        result=points.claim(info['claim_id']);self.assertEqual(result['claimed_amount'],'12.50')
        with self.assertRaises(PointClaimError):points.claim(info['claim_id'])
        self.assertEqual(self.ss.get.call_count,2)
    def test_points_timeout_or_expired_qualification_cannot_be_resubmitted(self):
        self.ss.get=Mock(side_effect=[Response({'X_RESULTCODE':'0','AbleDrawScore':'10','TotalBalance':'25'}),RequestException('lost')])
        points=PointClaim(self.ss);info=points.get_info()
        with self.assertRaises(PointClaimError):points.claim(info['claim_id'])
        with self.assertRaises(PointClaimError):points.claim(info['claim_id'])
        self.assertEqual(self.ss.get.call_count,2)
        points._snapshot={'claim_id':'old','amount':'10','expires':time.monotonic()-1}
        with self.assertRaises(PointClaimError):points.claim('old')
        self.assertEqual(self.ss.get.call_count,2)
    def test_point_fallback_uses_updated_auth_cookies_but_never_replays_existing_business_results(self):
        def get(url,**kwargs):
            if self.ss.get.call_count==1:
                self.ss.cookies.set('WAP_SID','rotated',domain='wap.gz.10086.cn',path='/waps')
                return Response(REJECT)
            self.assertIsNone(kwargs['params']);self.assertTrue(urlsplit(url).query.startswith('undefined&'))
            self.assertIn('WAP_SID=rotated',kwargs['headers']['Cookie'])
            return Response({'X_RESULTCODE':'0','AbleDrawScore':'0','TotalBalance':'99'})
        self.ss.get=Mock(side_effect=get);self.assertEqual(PointClaim(self.ss).get_info()['total_balance'],'99')
        self.assertEqual(self.ss.get.call_count,2)
        self.ss.get=Mock(return_value=Response({**REJECT,'AbleDrawScore':'10'}))
        with self.assertRaises(PointClaimError):PointClaim(self.ss).get_info()
        self.ss.get.assert_called_once()


if __name__=='__main__':unittest.main()
