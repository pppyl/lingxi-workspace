from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
import tempfile
import hashlib
import time
import unittest
from unittest.mock import patch

from http_transport import requests
from login_storage import DatabaseLoginStorage
from recharge.cmcc import SHANGHAI
from star_day_client import StarDayClient, StarDayError
from star_day_scheduler import StarDayScheduler


class Response:
    status_code = 200
    def __init__(self, data, now=None):
        self.value = data
        self.headers = {'Date': format_datetime(now or datetime.now(SHANGHAI))}
    def json(self):
        return deepcopy(self.value)


class Session:
    def __init__(self, handler):
        self.headers, self.cookies = {}, requests.Cookies()
        self.handler, self.closed = handler, False
        self.posts = []
    def get(self, url, **kwargs):
        return self.handler('GET', url, kwargs)
    def post(self, url, **kwargs):
        self.posts.append(url)
        return self.handler('POST', url, kwargs)
    def close(self):
        self.closed = True


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = {'phone':'18200000314', 'cookie':'JSESSIONID=test; UID=example', 'credential_revision':1}
        self.calls, self.sessions = [], []
        self.signed, self.claimed, self.stock = False, False, 100
        self.now = datetime.now(SHANGHAI)
        self.rejection = None
        self.claim_record = [{'qyName':'月卡'}]
        self.hidden_product = False
        self.catalog_failure = False
        self.transferable = True
        self.tier = '4'
        self.family = [{'familyMemNum':'opaque-official-member', 'showFamilyMemNum':'138****0002','familyMemName':'父亲'}]
        self.client = StarDayClient(lambda:deepcopy(self.snapshot), worker_session_factory=self.session)
        @contextmanager
        def auth(*args, **kwargs):
            client = self.session()
            try: yield client, deepcopy(self.snapshot)
            finally: client.close()
        self.auth = patch('star_day_client.star_day_session', auth)
        self.auth.start();self.addCleanup(self.auth.stop)
    def session(self):
        s = Session(self.handle);self.sessions.append(s);return s
    def handle(self, method, url, kwargs):
        path = url.split('/xhmqqthy')[1]
        self.calls.append((method,path,kwargs))
        if path.endswith('/queryVipLevel.do'): data={'userLevel':self.tier,'phone':self.snapshot['phone']}
        elif path=='/paidpurchase/familyCircle.action': data={'familyList':deepcopy(self.family)}
        elif path=='/sign/info': data={'signDates':[{'dayNum':1,'signStatus':1 if self.signed else 0},{'dayNum':2,'signStatus':-1}]}
        elif path.endswith('/queryIndexReceive.do'): data={'prodList':deepcopy(self.claim_record) if self.claimed else [],'enjoycollectabletimes':0 if self.claimed else 1}
        elif path.startswith('/normal/normalNewProdList?phase='):
            if self.catalog_failure:return Response({'status':'0','msg':'目录暂不可用'},self.now)
            data={'prodList':[{'id':3453,'qyName':'会员月卡','stock':self.stock,'levelable':'1,2,3,4,8','isRandom':False,'isHide':self.hidden_product,'transferable':int(self.transferable)}]}
        elif path=='/sign/sign':
            if self.rejection:return Response({'status':'0','msg':'官方暂未允许签到'},self.now)
            self.signed=True;data={}
        elif path=='/normal/initNormalNewQy': self.claimed=True;data={}
        else: raise AssertionError(path)
        return Response({'status':'1','data':data},self.now)
    def test_signin_exact_json_and_confirmed_today_does_not_repeat(self):
        actions=[]
        result=self.client.signin(before_submit=lambda *a:actions.append(a) or True)
        self.assertEqual(result['today_state'],'signed')
        call=next(c for c in self.calls if c[1]=='/sign/sign')
        self.assertEqual(call[0],'POST');self.assertEqual(call[2]['json'],{'sspid':'75628'})
        self.assertNotIn('Content-Type',call[2]['headers'])
        self.client.signin(before_submit=lambda *a:self.fail('duplicate'))
        self.assertEqual(sum(c[1]=='/sign/sign' for c in self.calls),1)
        self.assertEqual(actions,[('signin',self.now.strftime('%Y%m%d'))])
    def test_global_tone_membership_and_cycle_count(self):
        result=self.client.inspect()
        self.assertEqual((result['tier'],result['today_state'],result['consecutive_days']),('4','pending',0))
        self.assertFalse(result['month_claimed'])

    def test_claimed_product_ids_are_resolved_to_official_names_without_claiming_again(self):
        self.claimed=True
        for record in ([3453],['3453'],[{'productId':3453}]):
            with self.subTest(record=record):
                self.claim_record=record
                result=self.client.inspect()
                self.assertTrue(result['month_claimed'])
                self.assertEqual(result['benefit_id'],'3453')
                self.assertEqual(result['benefit_name'],'会员月卡')
        self.assertFalse(any(c[1]=='/normal/initNormalNewQy' for c in self.calls))

    def test_hidden_claimed_product_still_has_a_name_but_is_not_offered_for_new_claims(self):
        self.claimed=True;self.claim_record=[3453];self.hidden_product=True
        self.assertEqual(self.client.inspect()['benefit_name'],'会员月卡')
        self.assertEqual(self.client.products(),[])

    def test_product_name_lookup_failure_keeps_confirmed_monthly_state_and_identifier(self):
        self.claimed=True;self.claim_record=[3453];self.catalog_failure=True
        result=self.client.inspect()
        self.assertTrue(result['month_claimed']);self.assertEqual(result['benefit_id'],'3453')
        self.assertEqual(result['benefit_name'],'')
        self.assertEqual(result['today_state'],'pending')

    def test_receipt_already_includes_name_or_is_unclaimed_does_not_read_the_catalog(self):
        self.client.inspect()
        self.claimed=True;self.claim_record=[{'productId':3453,'qyName':'官方已领名称'}]
        self.assertEqual(self.client.inspect()['benefit_name'],'官方已领名称')
        self.assertFalse(any('/normalNewProdList' in c[1] for c in self.calls))

    def test_official_non_global_tone_tier_is_distinct_from_authentication_failure(self):
        original=self.handle
        def profile(method,url,kwargs):
            if url.endswith('/queryVipLevel.do'):
                return Response({'status':'1','data':{'userLevel':'7','levelName':'非全球通用户','phone':self.snapshot['phone']}},self.now)
            return original(method,url,kwargs)
        with patch.object(self,'handle',profile):result=self.client.inspect()
        self.assertEqual(result['state'],'ineligible');self.assertFalse(result['eligible'])
        self.assertEqual(result['tier'],'7');self.assertIn('非全球通用户（等级 7）',result['message'])
        self.assertIsNone(result['month_claimed'])
    def test_uncertain_prior_submission_queries_but_never_submits(self):
        result=self.client.signin(before_submit=lambda *a:False)
        self.assertTrue(result['uncertain']);self.assertEqual(result['state'],'unknown')
        self.assertFalse(any(c[1]=='/sign/sign' for c in self.calls))
    def test_authoritative_rejection_is_not_marked_uncertain(self):
        self.rejection=True
        with self.assertRaises(StarDayError) as caught:self.client.signin(before_submit=lambda *a:True)
        self.assertTrue(caught.exception.rejected);self.assertFalse(caught.exception.uncertain)
    def test_metadata_revision_does_not_invalidate_session(self):
        snapshot=deepcopy(self.snapshot);self.snapshot['credential_revision']+=1
        self.client._current(snapshot)
        self.snapshot['cookie']='JSESSIONID=changed'
        with self.assertRaises(StarDayError):self.client._current(snapshot)
    def test_all_four_phases_and_stock_is_displayed_as_claimed_percent(self):
        self.stock=0;products=self.client.products()
        self.assertEqual([p['phase'] for p in products],[1,2,3,4]);self.assertEqual(products[0]['stock'],0)
    def test_reference_single_parallel_request_ignores_stock_and_hour_gate(self):
        self.stock=100
        target=datetime.now(SHANGHAI).replace(hour=9,minute=59,second=59,microsecond=950000)
        self.now=target
        # Keep this timer test independent of wall time; target can be 09:59:59.950.
        with patch.object(self.client,'_now',return_value=target-timedelta(seconds=.02)):
            result=self.client.rush('3453',1,target_at=target,burst={'burst_count':1,'burst_workers':1,'burst_interval':.1},start_monotonic=time.monotonic()+.02,before_submit=lambda *a:True,cancelled=lambda:False)
        writes=[c for c in self.calls if c[1]=='/normal/initNormalNewQy']
        self.assertEqual(len(writes),1)
        self.assertEqual(writes[0][2]['data'],{'productId':'3453','receivePhone':'','channelCode':'75628','isRandom':'0','sourece':'2'})
        self.assertEqual(writes[0][2]['headers']['Content-Type'],'application/x-www-form-urlencoded; charset=UTF-8')
        self.assertEqual(result['state'],'success')
        self.assertFalse(any('/queryVipLevel' in c[1] or '/queryIndexReceive' in c[1] for c in self.calls))
        self.assertTrue(all(s.closed for s in self.sessions))

    def test_five_independent_rounds_all_send_even_when_first_returns_success(self):
        target=datetime.now(SHANGHAI)
        markers=[]
        result=self.client.rush('3453',1,target_at=target,
            burst={'burst_count':5,'burst_workers':5,'burst_interval':0,'offsets_ms':[-50,0,50,100,150]},
            start_monotonic=time.monotonic()+.07,before_submit=lambda *a:markers.append(a) or True,cancelled=lambda:False)
        self.assertEqual(result['attempt_count'],5)
        self.assertEqual(len([c for c in self.calls if c[1]=='/normal/initNormalNewQy']),5)
        workers=[s for s in self.sessions if any('/initNormalNewQy' in u for u in s.posts)]
        self.assertEqual(len(workers),5)
        self.assertTrue(all(len(s.posts)==1 for s in workers))
        sent=[datetime.fromisoformat(a['sent_at']) for a in result['attempts']]
        self.assertGreaterEqual((sent[-1]-sent[0]).total_seconds(),.17)
        self.assertEqual(result['state'],'success')
        self.assertEqual(len(markers),1)  # Persist once during preparation, not five writes at dispatch.
        self.assertEqual([a['planned_offset_ms'] for a in result['attempts']],[-50,0,50,100,150])

    def test_time_calibration_reads_only_and_reports_resolution_bound(self):
        result=self.client.calibrate()
        self.assertEqual(result['state'],'ready')
        self.assertGreaterEqual(result['samples'],3)
        self.assertGreaterEqual(result['uncertainty_ms'],0)
        self.assertLessEqual(result['uncertainty_ms'],500)
        self.assertTrue(all(c[0]=='GET' and '/normalNewProdList?' in c[1] for c in self.calls))

    def test_rush_without_shared_clock_never_sends(self):
        with self.assertRaises(StarDayError) as caught:
            self.client.rush('3453',1,target_at=self.now,burst={'burst_count':5,'burst_workers':5,'burst_interval':.05},before_submit=lambda *a:True,cancelled=lambda:False)
        self.assertEqual(caught.exception.code,'CLOCK_NOT_READY')
        self.assertFalse(any(c[1]=='/normal/initNormalNewQy' for c in self.calls))

    def test_immediate_claim_checks_monthly_state_and_submits_exact_form_once_without_stock_or_clock(self):
        actions=[]
        result=self.client.claim('3453',1,before_submit=lambda *a:actions.append(a) or True,product_name='会员月卡')
        writes=[c for c in self.calls if c[1]=='/normal/initNormalNewQy']
        self.assertEqual(len(writes),1)
        self.assertEqual(writes[0][2]['data'],{'productId':'3453','receivePhone':'','channelCode':'75628','isRandom':'0','sourece':'2'})
        self.assertTrue(result['month_claimed']);self.assertEqual(result['state'],'success')
        self.assertEqual(result['benefit_name'],'会员月卡');self.assertEqual(result['attempt_count'],1)
        self.assertEqual(actions,[('benefit',self.now.strftime('%Y%m'))])
        self.assertFalse(any('/normalNewProdList' in c[1] for c in self.calls))
        self.assertLess(next(i for i,c in enumerate(self.calls) if c[1].endswith('/queryIndexReceive.do')),
                        next(i for i,c in enumerate(self.calls) if c[1]=='/normal/initNormalNewQy'))
        again=self.client.claim('3453',1,before_submit=lambda *a:self.fail('duplicate'))
        self.assertEqual(again['state'],'already_claimed')
        self.assertEqual(sum(c[1]=='/normal/initNormalNewQy' for c in self.calls),1)

    def test_immediate_claim_prior_unknown_submission_is_not_replayed(self):
        result=self.client.claim('3453',1,before_submit=lambda *a:False)
        self.assertEqual(result['state'],'unknown');self.assertTrue(result['uncertain'])
        self.assertFalse(any(c[1]=='/normal/initNormalNewQy' for c in self.calls))

    def test_immediate_claim_definite_rejection_allows_a_later_manual_attempt(self):
        original=self.handle
        def reject(method,url,kwargs):
            if url.endswith('/normal/initNormalNewQy'):
                self.calls.append((method,'/normal/initNormalNewQy',kwargs))
                return Response({'status':'0','msg':'库存不足'},self.now)
            return original(method,url,kwargs)
        with patch.object(self,'handle',reject):
            with self.assertRaises(StarDayError) as caught:
                self.client.claim('3453',1,before_submit=lambda *a:True)
        self.assertTrue(caught.exception.rejected);self.assertFalse(caught.exception.uncertain)
        result=self.client.claim('3453',1,before_submit=lambda *a:True)
        self.assertEqual(result['state'],'success')

    def test_gift_options_only_exposes_masked_family_and_opaque_identifiers(self):
        options=self.client.gift_options()
        self.assertFalse(options['can_send_other'])
        self.assertEqual(options['family'][0]['phone_masked'],'138****0002')
        self.assertNotIn('opaque-official-member',str(options))
        self.assertFalse(any(c[1]=='/normal/initNormalNewQy' for c in self.calls))

    def test_family_gift_uses_official_encrypted_number_and_source_one(self):
        recipient={'kind':'family','id':hashlib.sha256(b'opaque-official-member').hexdigest()}
        result=self.client.claim('3453',1,product_name='会员月卡',recipient=recipient,before_submit=lambda *a:True)
        body=next(c for c in self.calls if c[1]=='/normal/initNormalNewQy')[2]['data']
        self.assertEqual(body,{'productId':'3453','receivePhone':'opaque-official-member','channelCode':'75628','isRandom':'0','sourece':'1'})
        self.assertEqual(result['benefit_recipient'],'父亲 · 138****0002')
        self.assertTrue(result['month_claimed']);self.assertEqual(result['state'],'success')

    def test_gift_cannot_bypass_family_or_non_transferable_product_rules(self):
        for recipient,transferable in (({'kind':'phone','phone':'13800000002'},True),
                                      ({'kind':'family','id':'0'*64},True),
                                      ({'kind':'family','id':hashlib.sha256(b'opaque-official-member').hexdigest()},False)):
            self.transferable=transferable
            with self.assertRaises(StarDayError):self.client.claim('3453',1,recipient=recipient,before_submit=lambda *a:self.fail('must not write'))
        self.assertFalse(any(c[1]=='/normal/initNormalNewQy' for c in self.calls))

    def test_extraordinary_card_can_gift_to_other_mobile_number(self):
        self.tier='8'
        result=self.client.claim('3453',1,recipient={'kind':'phone','phone':'13800000002'},before_submit=lambda *a:True)
        body=next(c for c in self.calls if c[1]=='/normal/initNormalNewQy')[2]['data']
        self.assertEqual(body['receivePhone'],'13800000002');self.assertEqual(body['sourece'],'1')
        self.assertEqual(result['benefit_recipient'],'138****0002')

    def test_timed_gift_prepares_recipient_before_burst_without_querying_inventory(self):
        recipient={'kind':'family','id':hashlib.sha256(b'opaque-official-member').hexdigest()}
        result=self.client.rush('3453',1,target_at=self.now,recipient=recipient,product_name='会员月卡',
            burst={'burst_count':2,'burst_workers':2,'burst_interval':0,'offsets_ms':[0,30]},
            start_monotonic=time.monotonic()+.03,before_submit=lambda *a:True,cancelled=lambda:False)
        writes=[c for c in self.calls if c[1]=='/normal/initNormalNewQy']
        self.assertEqual(len(writes),2)
        self.assertTrue(all(c[2]['data']['receivePhone']=='opaque-official-member' and c[2]['data']['sourece']=='1' for c in writes))
        self.assertFalse(any('/normalNewProdList' in c[1] for c in self.calls))
        self.assertEqual(result['benefit_recipient'],'父亲 · 138****0002')
        self.assertTrue(result['month_claimed'])


class Queue:
    def __init__(self):self.jobs=[]
    def submit(self,func,*args):self.jobs.append((func,args))
    def drain(self):
        while self.jobs:
            func,args=self.jobs.pop(0);func(*args)
    def shutdown(self,**kwargs):pass


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.storage=DatabaseLoginStorage(Path(self.temp.name)/'accounts.sqlite3',protect=lambda b:b'encrypted:'+b,unprotect=lambda b:b.removeprefix(b'encrypted:'))
        self.now=datetime(2026,10,31,7,tzinfo=SHANGHAI)
        self.pool=[{'id':'a','status':'active','name':'0314','phone_masked':'182****0314','credential_revision':1}]
        self.queue=Queue();self.signs=[];self.rushes=[];self.claims=[];self.gifts=[];self.official=set();self.official_benefits=set();self.fail_write=False
        self.scheduler=self.create()
        self.calibration_calls=0
    def create(self):
        parent=self
        class Client:
            def __init__(self,credentials):self.id=credentials()['id']
            def inspect(self):return {'date':parent.now.strftime('%Y%m%d'),'today_state':'signed' if self.id in parent.official else 'pending','consecutive_days':int(self.id in parent.official),'message':'官方记录','state':'ready'}
            def signin(self,*,before_submit):
                if self.id in parent.official:return self.inspect()
                if not before_submit('signin',parent.now.strftime('%Y%m%d')):return {**self.inspect(),'state':'unknown','uncertain':True}
                parent.signs.append(self.id)
                saved=parent.storage.load_star_day_data()
                parent.assertTrue(saved['actions'][f'{self.id}:signin:{parent.now:%Y%m%d}']['submitted'])
                if parent.fail_write:raise StarDayError('网络中断',uncertain=True)
                parent.official.add(self.id);return {**self.inspect(),'state':'signed'}
            def calibrate(self):
                parent.calibration_calls+=1
                return {'state':'ready','offset_ms':120,'uncertainty_ms':30,'samples':6,'at':parent.now.isoformat()}
            def products(self):return [{'id':'123','phase':1,'name':'会员月卡','stock':100,'transferable':True}]
            def prepare(self):return {'state':'ready','message':'抢购凭据已准备'}
            def gift_options(self):return {'can_send_other':False,'family':[{'id':'f'*64,'name':'亲友','phone_masked':'138****0002'}]}
            def claim(self,product_id,phase,*,before_submit,product_name,recipient=None):
                if recipient:parent.gifts.append(recipient)
                if self.id in parent.official_benefits:
                    return {'date':parent.now.strftime('%Y%m%d'),'state':'already_claimed','month_claimed':True,'message':'本月已领取'}
                if not before_submit('benefit',parent.now.strftime('%Y%m')):
                    return {'state':'unknown','message':'未确认','uncertain':True}
                parent.claims.append((self.id,product_id))
                if parent.fail_write:raise StarDayError('官方拒绝领取',rejected=True)
                parent.official_benefits.add(self.id)
                return {'date':parent.now.strftime('%Y%m%d'),'state':'success','month_claimed':True,'benefit_name':product_name,'message':'领取成功','attempt_count':1}
            def rush(self,product_id,phase,*,target_at,burst,before_submit,cancelled,start_monotonic,product_name):
                parent.rushes.append((self.id,target_at,burst,cancelled(),start_monotonic))
                if cancelled():return {'state':'stopped','message':'已停止'}
                if before_submit('benefit',target_at.strftime('%Y%m')):return {'state':'success','message':'领取成功','attempt_count':1}
                return {'state':'unknown','message':'未确认','uncertain':True}
        return StarDayScheduler(lambda key:{'id':key,'phone':'13800000001'},lambda:deepcopy(self.pool),storage=self.storage,client_factory=Client,executor=self.queue,now=lambda:self.now)
    def test_default_all_valid_and_restored_or_new_after_start_catchup(self):
        self.pool.append({'id':'b','status':'login_required'})
        self.scheduler.settings({'signin_enabled':True,'signin_time':'08:00'})
        self.scheduler.tick();self.queue.drain();self.assertFalse(self.signs)
        self.now=self.now.replace(hour=9);self.scheduler.tick();self.queue.drain();self.assertEqual(self.signs,['a'])
        self.pool[1]['status']='active';self.pool.append({'id':'c','status':'active'})
        self.scheduler.tick();self.queue.drain();self.assertEqual(set(self.signs),{'a','b','c'})
        self.scheduler.tick();self.queue.drain();self.assertEqual(len(self.signs),3)
    def test_read_only_status_never_signs_before_configured_time(self):
        self.scheduler.settings({'signin_enabled':True,'signin_time':'08:00'})
        self.scheduler.status();self.queue.drain();self.assertFalse(self.signs)
        self.assertFalse(self.queue.jobs)
        self.assertEqual(self.scheduler.status()['accounts'][0]['today_state'],'unknown')

    def test_preparing_credentials_does_not_sign_before_the_configured_start(self):
        self.now=self.now.replace(hour=9,minute=30)
        self.scheduler.settings({'signin_enabled':True,'signin_time':'16:00','session_prepare_time':'09:30'})
        self.scheduler.tick();self.queue.drain()
        self.assertFalse(self.signs)
    def test_unknown_submit_persists_across_restart_and_only_reconciles(self):
        self.fail_write=True;self.scheduler.tick();self.queue.drain();self.assertEqual(self.signs,['a'])
        self.scheduler=self.create();self.now+=timedelta(minutes=10);self.scheduler.tick();self.queue.drain();self.assertEqual(self.signs,['a'])
        self.official.add('a');self.now+=timedelta(minutes=6);self.scheduler.run(['a'],inspect=True);self.queue.drain()
        self.assertEqual(self.scheduler.status()['accounts'][0]['today_state'],'signed')
    def test_job_sold_out_product_allowed_one_request_per_account_and_offset(self):
        self.now=self.now.replace(hour=9,minute=59,second=40)
        self.scheduler.settings({'signin_enabled':False,'signin_time':'08:00'})
        at=self.now.replace(hour=10,minute=0,second=0)
        job=self.scheduler.schedule({'product_id':'123','phase':1,'at':at.isoformat()})
        self.scheduler.tick();self.queue.drain();self.scheduler.tick();self.queue.drain()
        self.assertEqual(len(self.rushes),1)
        self.assertEqual(self.rushes[0][1],at)
        self.assertEqual(self.rushes[0][2]['offsets_ms'],[-50,0,50,100,150])
        self.assertEqual(self.rushes[0][2]['burst_count'],5)
        self.scheduler.tick();self.queue.drain();self.assertEqual(len(self.rushes),1)
        self.assertTrue(self.scheduler.data['jobs'][job['id']]['enabled']);self.assertEqual(self.scheduler.data['jobs'][job['id']]['state'],'monthly_completed')
    def test_stopped_job_never_submits_and_forget_removes_backend_data(self):
        self.scheduler.settings({'signin_enabled':False,'signin_time':'08:00'})
        job=self.scheduler.schedule({'product_id':'123','phase':1,'at':(self.now+timedelta(seconds=20)).isoformat()})
        self.scheduler.stop(job['id']);self.scheduler.tick();self.queue.drain();self.assertFalse(self.rushes)
        self.scheduler.run();self.queue.drain();self.scheduler.forget('a')
        saved=self.storage.load_star_day_data();self.assertFalse(saved['accounts']);self.assertFalse(saved['actions'])
    def test_future_time_and_offset_validation(self):
        for payload in [{'product_id':'123','phase':1,'at':self.now.isoformat()}, {'product_id':'123','phase':1,'at':(self.now+timedelta(hours=1)).isoformat(),'offsets_ms':[3000]}]:
            with self.assertRaises(StarDayError):self.scheduler.schedule(payload)
    def test_restart_interrupts_prepared_claim_instead_of_replaying(self):
        self.now=self.now.replace(hour=9,minute=59,second=40)
        job=self.scheduler.schedule({'product_id':'123','phase':1,'at':(self.now+timedelta(seconds=20)).isoformat()})
        self.scheduler.settings({'signin_enabled':False,'signin_time':'08:00'})
        self.scheduler.tick();self.queue.drain()  # Finish the 09:30 credential preparation.
        self.scheduler.calibrate();self.scheduler.tick()
        loaded=self.create().data['jobs'][job['id']]
        self.assertTrue(loaded['enabled']);self.assertEqual(loaded['results']['a']['state'],'unknown')

    def test_selected_accounts_use_one_calibration_and_the_exact_same_deadline(self):
        self.now=self.now.replace(hour=9,minute=59,second=40)
        self.pool += [{'id':'b','status':'active'},{'id':'c','status':'active'}]
        self.scheduler.settings({'signin_enabled':False,'signin_time':'08:00'})
        job=self.scheduler.schedule({'product_id':'123','phase':1,'at':(self.now+timedelta(seconds=25)).isoformat(),'login_ids':['a','b']})
        self.scheduler.tick();self.queue.drain();self.scheduler.tick();self.queue.drain()
        self.assertEqual(self.calibration_calls,1)
        self.assertEqual({r[0] for r in self.rushes},{'a','b'})
        self.assertEqual(self.rushes[0][4],self.rushes[1][4])
        self.assertEqual(self.scheduler.data['jobs'][job['id']]['login_ids'],['a','b'])
        self.assertEqual(self.scheduler.data['jobs'][job['id']]['calibration']['offset_ms'],120)

    def test_empty_or_expired_account_selection_is_rejected_before_scheduling(self):
        self.pool.append({'id':'b','status':'login_required'})
        base={'product_id':'123','phase':1,'at':(self.now+timedelta(minutes=2)).isoformat()}
        for ids in ([],['missing'],['a','b']):
            with self.assertRaises(StarDayError):self.scheduler.schedule({**base,'login_ids':ids})
        self.assertFalse(self.scheduler.data['jobs'])

    def test_each_custom_offset_is_persisted_and_used_without_uniform_spacing(self):
        self.now=self.now.replace(hour=9,minute=59,second=40)
        self.scheduler.settings({'signin_enabled':False,'signin_time':'08:00'})
        custom=[-80,-10,45,120,230]
        job=self.scheduler.schedule({'product_id':'123','phase':1,'at':(self.now+timedelta(seconds=25)).isoformat(),
                                    'login_ids':['a'],'offsets_ms':custom})
        self.assertEqual(self.storage.load_star_day_data()['jobs'][job['id']]['offsets_ms'],custom)
        self.scheduler.tick();self.queue.drain();self.scheduler.tick();self.queue.drain()
        self.assertEqual(self.rushes[0][2]['offsets_ms'],custom)
        self.assertEqual(self.rushes[0][2]['burst_workers'],5)

    def test_rounds_may_be_added_removed_or_entered_out_of_order(self):
        job=self.scheduler.schedule({'product_id':'123','phase':1,'at':(self.now+timedelta(minutes=2)).isoformat(),
                                    'offsets_ms':[140,-60,0]})
        saved=self.scheduler.data['jobs'][job['id']]
        self.assertEqual(saved['offsets_ms'],[-60,0,140])
        self.assertEqual(saved['burst_count'],3)

    def test_missed_reservation_does_not_start_calibration_or_send(self):
        self.now=self.now.replace(hour=9,minute=59,second=40)
        self.scheduler.settings({'signin_enabled':False,'signin_time':'08:00'})
        job=self.scheduler.schedule({'product_id':'123','phase':1,'at':(self.now+timedelta(minutes=1)).isoformat()})
        self.now+=timedelta(minutes=5);self.scheduler.tick();self.queue.drain()
        self.assertEqual(self.calibration_calls,0);self.assertFalse(self.rushes)
        self.assertEqual(self.scheduler.data['jobs'][job['id']]['state'],'waiting');self.assertEqual(self.scheduler.data['jobs'][job['id']]['at'],'2026-11-01T10:00:00+08:00')

    def test_immediate_selected_accounts_run_now_without_calibration_and_keep_existing_reservation(self):
        self.pool += [{'id':'b','status':'active','phone_masked':'138****0002'},
                      {'id':'c','status':'active','phone_masked':'138****0003'}]
        self.scheduler.settings({'signin_enabled':False,'signin_time':'08:00'})
        later=self.scheduler.schedule({'product_id':'123','phase':1,'at':(self.now+timedelta(days=1)).isoformat(),'login_ids':['c']})
        original=deepcopy(self.scheduler.data['jobs'][later['id']])
        job=self.scheduler.schedule({'product_id':'123','phase':1,'login_ids':['a','b']},immediate=True)
        self.scheduler.tick();self.queue.drain();self.scheduler.tick()
        self.assertEqual(self.calibration_calls,0);self.assertFalse(self.rushes)
        self.assertEqual(set(self.claims),{('a','123'),('b','123')})
        self.assertEqual(self.scheduler.data['jobs'][later['id']],original)
        manual=self.scheduler.data['jobs'][job['id']]
        self.assertEqual(manual['state'],'completed');self.assertFalse(manual['enabled'])
        self.assertTrue(manual['immediate']);self.assertEqual(manual['burst_count'],1)
        self.assertTrue(self.scheduler.status()['accounts'][0]['month_claimed'])

    def test_immediate_failed_or_already_claimed_accounts_can_be_checked_again_without_duplicate_success(self):
        self.scheduler.settings({'signin_enabled':False,'signin_time':'08:00'})
        payload={'product_id':'123','phase':1,'login_ids':['a']}
        self.fail_write=True
        self.scheduler.schedule(payload,immediate=True);self.scheduler.tick();self.queue.drain();self.scheduler.tick()
        self.assertNotIn('a:benefit:202610',self.scheduler.data['actions'])
        self.fail_write=False
        for _ in range(2):
            self.scheduler.schedule(payload,immediate=True);self.scheduler.tick();self.queue.drain();self.scheduler.tick()
        self.assertEqual(self.claims,[('a','123'),('a','123')])

    def test_rate_limited_claim_stops_background_retries_but_allows_one_explicit_manual_attempt(self):
        self.scheduler.settings({'signin_enabled':False,'signin_time':'08:00'})
        payload={'product_id':'123','phase':1,'login_ids':['a']}
        def limited(client,product_id,phase,*,before_submit,**kwargs):
            self.assertTrue(before_submit('benefit',self.now.strftime('%Y%m')))
            raise StarDayError('请求频繁','-800',rejected=True)
        with patch.object(self.scheduler.client_factory,'claim',limited):
            self.scheduler.schedule(payload,immediate=True)
            self.scheduler.tick();self.queue.drain();self.scheduler.tick()
        self.assertEqual(self.scheduler.status()['accounts'][0]['state'],'rate_limited')
        self.assertNotIn('a:benefit:202610',self.scheduler.data['actions'])
        self.assertTrue(self.scheduler._deferred('a'))
        for _ in range(3):
            self.now+=timedelta(minutes=5)
            self.scheduler.tick();self.queue.drain()
        self.assertFalse(self.claims)
        self.scheduler.schedule(payload,immediate=True)
        self.scheduler.tick();self.queue.drain();self.scheduler.tick()
        self.assertEqual(self.claims,[('a','123')])
        self.assertTrue(self.scheduler.status()['accounts'][0]['month_claimed'])

    def test_immediate_claim_preserves_prior_unconfirmed_marker_and_running_account_exclusion(self):
        self.scheduler.settings({'signin_enabled':False,'signin_time':'08:00'})
        self.scheduler.data['actions']['a:benefit:202610']={'submitted':True,'time':self.now.isoformat()}
        payload={'product_id':'123','phase':1,'login_ids':['a']}
        job=self.scheduler.schedule(payload,immediate=True)
        self.scheduler.tick();self.scheduler.tick()
        self.assertEqual(len(self.queue.jobs),1)
        self.queue.drain();self.scheduler.tick();self.assertFalse(self.claims)
        self.assertEqual(self.scheduler.data['jobs'][job['id']]['results']['a']['state'],'unknown')
        self.assertIn('a:benefit:202610',self.scheduler.data['actions'])

    def test_immediate_invalid_product_or_selection_does_not_create_jobs(self):
        self.pool.append({'id':'b','status':'login_required'})
        for payload in ({'product_id':'missing','phase':1,'login_ids':['a']},
                        {'product_id':'123','phase':1,'login_ids':[]},
                        {'product_id':'123','phase':1,'login_ids':['b']}):
            with self.assertRaises(StarDayError):self.scheduler.schedule(payload,immediate=True)
        self.assertFalse(self.scheduler.data['jobs'])

    def test_verified_benefit_name_survives_read_only_refresh_failure_and_restart(self):
        self.scheduler.data['accounts']['a']={'observation':{'date':'20261031','month_claimed':True,
            'benefit_id':'123','benefit_name':'已确认权益'},'history':[]}
        client=self.scheduler.client_factory(lambda:{'id':'a'})
        client.inspect=lambda:{'date':'20261031','month_claimed':True,'benefit_id':'123','benefit_name':'',
                               'state':'ready','message':'官方记录'}
        with patch.object(self.scheduler,'client_factory',return_value=client):
            self.scheduler._queue('a','inspect');self.queue.drain()
        self.assertEqual(self.scheduler.data['accounts']['a']['observation']['benefit_name'],'已确认权益')
        restored=self.create()
        self.assertEqual(restored.status()['accounts'][0]['benefit_name'],'已确认权益')
        self.now+=timedelta(days=1)
        account=restored.status()['accounts'][0]
        self.assertIsNone(account['month_claimed']);self.assertEqual(account['benefit_name'],'')
        self.assertEqual(account['benefit_id'],'')

    def test_gift_is_persisted_privately_and_counts_as_the_donor_monthly_success(self):
        job=self.scheduler.schedule({'product_id':'123','phase':1,'login_ids':['a'],'recipient':{'kind':'family','id':'f'*64}},immediate=True)
        self.scheduler.settings({'signin_enabled':False,'signin_time':'08:00'})
        self.scheduler.tick();self.queue.drain();self.scheduler.tick()
        self.assertEqual(self.gifts,[{'kind':'family','id':'f'*64}])
        saved=self.storage.load_star_day_data()['jobs'][job['id']]
        self.assertIn('recipient',saved)
        public=next(j for j in self.scheduler.status()['jobs'] if j['id']==job['id'])
        self.assertNotIn('recipient',public);self.assertTrue(public['is_gift'])
        self.assertTrue(self.scheduler.data['actions']['a:benefit:202610']['confirmed'])

    def test_timed_gift_binds_confirmed_recipient_and_fixed_daily_ten(self):
        job=self.scheduler.schedule({'product_id':'123','phase':1,'login_ids':['a'],'recipient':{'kind':'family','id':'f'*64}})
        saved=self.scheduler.data['jobs'][job['id']]
        self.assertFalse(saved['immediate']);self.assertTrue(saved['daily'])
        self.assertEqual(saved['at'],'2026-10-31T10:00:00+08:00')
        self.assertEqual(saved['recipient_label'],'亲友 · 138****0002')
        self.assertEqual(saved['recipient']['id'],'f'*64)

    def test_gift_confirmation_rejects_unknown_recipient_and_non_extraordinary_phone(self):
        for recipient in ({'kind':'family','id':'0'*64},{'kind':'phone','phone':'13800000002'}):
            with self.assertRaises(StarDayError):
                self.scheduler.schedule({'product_id':'123','phase':1,'login_ids':['a'],'recipient':recipient})
        self.assertFalse(self.scheduler.data['jobs'])


class RouteTests(unittest.TestCase):
    def test_page_status_and_input_errors_with_empty_shared_pool(self):
        from app import create_app
        app=create_app();http=app.test_client()
        response=http.get('/star-day')
        self.assertEqual(response.status_code,200)
        self.assertIn('star-select-all',response.text)
        self.assertIn('star-claim-product',response.text)
        self.assertIn('本月未领权益',response.text)
        self.assertEqual(http.get('/api/star-day/status').json['accounts'],[])
        self.assertEqual(http.post('/api/star-day/signin',json={}).status_code,400)
        self.assertEqual(http.post('/api/star-day/claim',json={}).status_code,400)
        self.assertEqual(http.post('/api/star-day/settings',json={'signin_enabled':True,'signin_time':'99:00'}).status_code,400)
        self.assertEqual(http.get('/api/star-day/products').status_code,401)
        self.assertEqual(http.post('/api/star-day/settings',json={'signin_enabled':True,'signin_time':'08:20'}).status_code,200)
        self.assertEqual(http.get('/api/star-day/status').json['settings']['signin_time'],'08:20')


if __name__=='__main__':unittest.main()
