import base64
from copy import deepcopy
from datetime import datetime
import unittest
from unittest.mock import Mock, patch

from http_transport import requests
from recharge.cmcc import SHANGHAI
from star_day_auth import star_day_session, StarDayAuthError, encrypt_uid, UID_PUBLIC_MODULUS, CHECK_UID_PATH, ORIGIN, ENTRY_URL


class StarDayAuthTests(unittest.TestCase):
    def setUp(self):
        self.snapshot={'phone':'18200000314','cookie':'JSESSIONID=session; UID=original-uid','credential_revision':1}
        self.client=Mock(headers={},cookies=requests.Cookies())
        self.client.post.return_value=Mock(status_code=200)
        self.client.post.return_value.json.return_value={'status':'1'}
        self.client.cookies.set('access_token','campaign-token',domain='qqt.cmicrwx.cn')
        self.flow=Mock();self.flow.call.return_value=(200,{'retCode':'000000'},{})
    def test_bridge_uses_original_uid_and_campaign_uses_separate_encryption(self):
        with patch('star_day_auth.AppLoginSession.from_credentials',return_value=self.flow),patch('star_day_auth.encrypt_uid',return_value='encrypted-uid'):
            with star_day_session(lambda:deepcopy(self.snapshot),session_factory=lambda:self.client) as (client,snapshot):
                self.assertIs(client,self.client)
                self.assertEqual(snapshot['phone'],self.snapshot['phone'])
        self.flow.call.assert_called_once_with(CHECK_UID_PATH,{'channelId':'12063','domain':ORIGIN,'url':ENTRY_URL,'tel':self.snapshot['phone'],'uId':'original-uid'})
        self.assertEqual(self.client.post.call_args.kwargs['data'],{'token':'encrypted-uid','sspId':'75628'})
        self.client.close.assert_called_once()
    def test_uid_encryption_uses_native_rsa_s1_and_time_plaintext(self):
        key=Mock();key.encrypt.return_value=b'cipher'
        with patch('star_day_auth.rsa.RSAPublicNumbers') as numbers:
            numbers.return_value.public_key.return_value=key
            self.assertEqual(encrypt_uid('uid',datetime(2026,10,31,10,0,0,tzinfo=SHANGHAI)),base64.b64encode(b'cipher').decode())
            numbers.assert_called_once_with(65537,UID_PUBLIC_MODULUS)
            self.assertEqual(key.encrypt.call_args.args[0],b'leadeonuid20261031100000')
    def test_missing_uid_requires_login_before_upstream(self):
        self.snapshot['cookie']='JSESSIONID=only'
        with self.assertRaises(StarDayAuthError) as caught:
            with star_day_session(lambda:self.snapshot,session_factory=lambda:self.client):pass
        self.assertEqual(caught.exception.status,401);self.client.post.assert_not_called()
    def test_activity_error_or_missing_cookie_never_yields_authenticated_session(self):
        with patch('star_day_auth.AppLoginSession.from_credentials',return_value=self.flow),patch('star_day_auth.encrypt_uid',return_value='encrypted-uid'):
            for mode in ('denied','no-cookie'):
                self.client.post.return_value.json.return_value={'status':'0'} if mode=='denied' else {'status':'1'}
                self.client.cookies=requests.Cookies()
                with self.assertRaises(StarDayAuthError):
                    with star_day_session(lambda:deepcopy(self.snapshot),session_factory=lambda:self.client):self.fail('must not yield')
    def test_metadata_refresh_keeps_session_but_real_cookie_rotation_stops_it(self):
        def campaign(*args,**kwargs):
            self.snapshot['credential_revision']+=1
            return Mock(status_code=200,json=lambda:{'status':'1'})
        self.client.post.side_effect=campaign
        with patch('star_day_auth.AppLoginSession.from_credentials',return_value=self.flow),patch('star_day_auth.encrypt_uid',return_value='encrypted-uid'):
            with star_day_session(lambda:deepcopy(self.snapshot),session_factory=lambda:self.client):pass
            def changed(*args,**kwargs):
                self.snapshot['cookie']='JSESSIONID=new; UID=original-uid'
                return Mock(status_code=200,json=lambda:{'status':'1'})
            self.client.post.side_effect=changed
            with self.assertRaises(StarDayAuthError) as caught:
                with star_day_session(lambda:deepcopy(self.snapshot),session_factory=lambda:self.client):pass
            self.assertEqual(caught.exception.code,'APP_CREDENTIALS_CHANGED')


if __name__=='__main__':unittest.main()
