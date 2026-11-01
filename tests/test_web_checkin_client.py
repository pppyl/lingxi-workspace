"""Official non-APP protocol, binding and no-replay regression tests."""
from copy import deepcopy
from datetime import datetime
import json
from types import SimpleNamespace
import unittest

from http_transport import requests
from credential_cache import CredentialCache
from activities.web_checkin.client import (
    WebCheckinClient, WebCheckinError, WebLoginManager, SCOPE, ENTRY, BASE,
    _encrypt, _decrypt,
)


PHONE = '13800000001'
SNAPSHOT = {'id':'account-a', 'phone':PHONE, 'cookie':'JSESSIONID=app-only-secret'}


class Response:
    def __init__(self, value=None, *, text='', status=200, location=None):
        self.status_code, self.value, self.text = status, value, text
        self.headers = {'location':location} if location else {}

    def json(self):
        if self.value is None:
            raise ValueError('not json')
        return self.value


class Session:
    def __init__(self, fixture):
        self.fixture, self.headers, self.cookies = fixture, {}, requests.Cookies()
        self.closed = False

    def close(self):
        self.closed = True

    def get(self, url, **kwargs):
        self.fixture.calls.append(('GET',url,kwargs,dict(self.cookies.items())))
        if '/qwhdsso/login?' in url:
            return Response(status=302, location='https://wx.10086.cn/website/bind/bindAccount/new?'
                'redirectSource=SSO_YQS&activityId=fixture&redirectUrl=https%3A%2F%2Fwx.10086.cn%2Fqwhdsso%2Fredirect%3Fsid%3Dfixture')
        if '/website/bind/bindAccount/new?' in url:
            self.cookies.set('csrf-session','bound', domain='wx.10086.cn')
            return Response(text="<script>var csrf='csrf-fixture',reqTmp='2025080702';</script>")
        if '/qwhdsso/redirect?' in url:
            self.cookies.set('QWHDSSOJWT','independent-web-session', domain='wx.10086.cn')
            return Response(status=302, location=ENTRY)
        if url == ENTRY:
            return Response(text='<html>official activity</html>')
        raise AssertionError('Unexpected GET '+url)

    def post(self, url, **kwargs):
        self.fixture.calls.append(('POST',url,kwargs,dict(self.cookies.items())))
        if url.endswith('/website/bind/sendMsg'):
            value = {'status':0}
            return Response(_encrypt(json.dumps(value)) if self.fixture.encrypted_sms else value)
        if url.endswith('/website/bind/bindAccount/new'):
            return Response({'status':self.fixture.login_code})
        if url.endswith('/website/api/singleAuth'):
            return Response(text='official-ticket')
        if url == BASE+'user/info':
            value = {'mobile':PHONE,'nickName':'138****0001','channel':self.fixture.channel}
            if self.fixture.wrong_phone:
                value.update(mobile='13800000002',nickName='138****0002')
        elif url == BASE+'info/commonInfo':
            value = {'sysTime':'20261101090000'}
        elif url == BASE+'info/prizeInfo':
            if self.fixture.fail_after_submit and self.fixture.submits:
                raise requests.RequestsError('interrupted')
            value = {'todayMarked':self.fixture.signed,'markedTimes':3 if self.fixture.completed else 1 if self.fixture.signed else 0,
                     'totalMarkTimes':3,'prizes':[]}
            if self.fixture.invalid_progress:
                value.pop('todayMarked')
        elif url == BASE+'do/mark':
            self.fixture.submits += 1
            self.fixture.signed = True
            value = {'name':'签到奖品'}
        else:
            raise AssertionError('Unexpected POST '+url)
        if self.fixture.auth_expired:
            return Response({'code':'GOTO_LOGIN'})
        return Response({'code':'SUCCESS','data':value})


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.calls, self.sessions = [], []
        self.channel, self.login_code = 'dlwmh', 0
        self.signed = self.wrong_phone = self.invalid_progress = self.auth_expired = False
        self.completed = False
        self.fail_after_submit = self.encrypted_sms = False
        self.submits, self.clock = 0, 100
        self.snapshot, self.cache = deepcopy(SNAPSHOT), CredentialCache()
        self.manager = WebLoginManager(lambda _id:self.snapshot, self.cache,
            session_factory=self.session, now=lambda:self.clock)
        self.client = WebCheckinClient(lambda:self.snapshot, credential_cache=self.cache, session_factory=self.session)
        self.addCleanup(self.manager.close)

    def session(self):
        session = Session(self)
        self.sessions.append(session)
        return session

    def login(self):
        self.manager.sms('account-a')
        return self.manager.login('account-a','123456')

    def test_aes_matches_official_cryptojs_fixture(self):
        self.assertEqual(_encrypt(PHONE),'EF81C6E29DFF7B43BF01F4ED811CF442')
        self.assertEqual(_decrypt(_encrypt('123456')),'123456')

    def test_missing_channel_login_does_not_convert_app_login_or_send_sms(self):
        with self.assertRaises(WebCheckinError) as error:
            self.client.status()
        self.assertEqual(error.exception.code,'ACTIVITY_LOGIN_REQUIRED')
        self.assertFalse(self.calls)
        self.assertTrue(self.sessions[0].closed)

    def test_login_preserves_csrf_jar_and_exact_official_encryption_without_app_cookie(self):
        self.login()
        sms = next(call for call in self.calls if call[1].endswith('/sendMsg'))
        login = next(call for call in self.calls if call[1].endswith('/bindAccount/new') and call[0]=='POST')
        self.assertEqual(sms[2]['data'],{'telephone':_encrypt(PHONE),'riskToken':''})
        self.assertEqual(sms[2]['headers'],{'csrf-token':'csrf-fixture','reqTmp':'2025080702'})
        self.assertEqual(login[2]['json']['password'],_encrypt('123456'))
        self.assertEqual(login[2]['json']['checkCtrol'],1)
        self.assertEqual(login[2]['json']['bindSource'],'Afixture')
        self.assertEqual(login[3]['csrf-session'],'bound')
        self.assertFalse(any('JSESSIONID' in call[3] for call in self.calls))
        saved = self.cache.load(SCOPE,self.snapshot)
        self.assertIsNotNone(saved)
        self.assertNotIn('123456',json.dumps(saved))
        self.assertNotIn('app-only-secret',json.dumps(saved))

    def test_encrypted_sms_response_from_official_page_is_supported(self):
        self.encrypted_sms = True
        self.assertEqual(self.manager.sms('account-a')['retry_after'],60)

    def test_sms_cooldown_uses_existing_flow_without_sending_again(self):
        self.manager.sms('account-a')
        count = len(self.calls)
        with self.assertRaises(WebCheckinError) as error:
            self.manager.sms('account-a')
        self.assertEqual(error.exception.code,'SMS_COOLDOWN')
        self.assertEqual(len(self.calls),count)
        self.clock += 60
        self.manager.sms('account-a')
        self.assertEqual(sum('/qwhdsso/login?' in call[1] for call in self.calls),1)

    def test_changed_shared_app_cookie_does_not_discard_web_session(self):
        self.login()
        self.calls.clear()
        self.snapshot['cookie']='JSESSIONID=rotated-app-only'
        state = self.client.status()
        self.assertEqual(state['channel'],'dlwmh')
        self.assertEqual(state['marked_times'],0)
        self.assertEqual([call[1].removeprefix(BASE) for call in self.calls],
                         ['user/info','info/commonInfo','info/prizeInfo'])

    def test_an_app_identity_cannot_be_saved_as_web_channel(self):
        self.channel='app'
        with self.assertRaises(WebCheckinError) as error:
            self.login()
        self.assertEqual(error.exception.code,'CHANNEL_MISMATCH')
        self.assertIsNone(self.cache.load(SCOPE,self.snapshot))

    def test_wrong_account_cannot_be_saved(self):
        self.wrong_phone=True
        with self.assertRaises(WebCheckinError) as error:
            self.login()
        self.assertEqual(error.exception.code,'ACCOUNT_MISMATCH')
        self.assertIsNone(self.cache.load(SCOPE,self.snapshot))

    def test_official_expired_code_is_not_treated_as_login_success(self):
        self.manager.sms('account-a')
        self.login_code=6002
        with self.assertRaises(WebCheckinError) as error:
            self.manager.login('account-a','123456')
        self.assertEqual(error.exception.code,'6002')
        self.assertIsNone(self.cache.load(SCOPE,self.snapshot))
        self.assertFalse(any('/singleAuth' in call[1] for call in self.calls))

    def test_signin_uses_non_app_do_mark_once_and_checks_authoritative_progress(self):
        self.login()
        self.calls.clear()
        recorded=[]
        state=self.client.signin(before_submit=lambda:recorded.append(self.submits))
        self.assertEqual(recorded,[0])
        self.assertEqual(self.submits,1)
        self.assertTrue(state['today_signed'])
        self.assertEqual(state['sign_date'],'2026-11-01')
        self.assertFalse(any('mark31' in call[1] for call in self.calls))
        self.client.signin(before_submit=lambda:self.fail('already signed'))
        self.assertEqual(self.submits,1)

    def test_incomplete_progress_prevents_submission(self):
        self.login()
        self.invalid_progress=True
        with self.assertRaises(WebCheckinError) as error:
            self.client.signin()
        self.assertEqual(error.exception.code,'PROGRESS_MISSING')
        self.assertEqual(self.submits,0)

    def test_completed_cycle_is_not_submitted_again_even_when_not_signed_today(self):
        self.login()
        self.completed = True
        result = self.client.signin(before_submit=lambda:self.fail('completed cycle cannot be submitted'))
        self.assertFalse(result['today_signed'])
        self.assertTrue(result['cycle_complete'])
        self.assertEqual(self.submits,0)

    def test_lost_status_after_submission_remains_uncertain_without_retry(self):
        self.login()
        self.fail_after_submit=True
        with self.assertRaises(WebCheckinError) as error:
            self.client.signin()
        self.assertTrue(error.exception.uncertain)
        self.assertEqual(self.submits,1)

    def test_auth_expiry_invalidates_only_this_scope_and_never_sends_sms(self):
        self.login()
        self.calls.clear()
        self.auth_expired=True
        with self.assertRaises(WebCheckinError) as error:
            self.client.status()
        self.assertEqual(error.exception.code,'ACTIVITY_LOGIN_REQUIRED')
        self.assertIsNone(self.cache.load(SCOPE,self.snapshot))
        self.assertEqual(len(self.calls),1)

    def test_forget_removes_only_the_selected_channel_account(self):
        self.login()
        self.manager.forget('account-a')
        self.assertIsNone(self.cache.load(SCOPE,self.snapshot))
        self.assertFalse(self.manager.flows)


if __name__ == '__main__':
    unittest.main()
