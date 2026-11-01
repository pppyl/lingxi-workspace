"""User-initiated campaign SMS login; never renews the shared APP identity."""
import re
import time

from http_transport import requests
from star_day_auth import AJAX_HEADERS, WEB_UA, PLATFORM, StarDayAuthError, RATE_LIMIT_MESSAGE
from star_day_crypto import encrypt_sms_phone, encrypt_login_value


class StarDaySmsLogin:
    def __init__(self, cache, *, session_factory=None, now=time.time):
        self.cache, self.now = cache, now
        self.session_factory = session_factory or (lambda:requests.Session(impersonate='chrome', default_headers=False, use_thread_local_curl=False))
        self.pending = {}

    def _request(self, client, path, data):
        try:
            response = client.post(PLATFORM+path, data=data, timeout=(10,25), allow_redirects=False)
        except requests.RequestsError as error:
            raise StarDayAuthError('连接星动日短信认证服务失败；请勿连续获取验证码。', 'SMS_NETWORK') from error
        if response.status_code != 200:
            raise StarDayAuthError('星动日短信认证 HTTP '+str(response.status_code), 'SMS_HTTP')
        try:
            value = response.json()
        except ValueError as error:
            raise StarDayAuthError('短信认证响应格式异常，请稍后重试。', 'SMS_FORMAT') from error
        if not isinstance(value,dict):
            raise StarDayAuthError('短信认证响应格式异常，请稍后重试。','SMS_FORMAT')
        code = str(value.get('status', 'unknown'))
        if code != '1':
            message = {'0':'验证码错误，请检查后重新输入。','-41':'验证码错误，请检查后重新输入。',
                '-42':'获取验证码过于频繁，请稍后再试。','-43':'获取验证码过于频繁，请稍后再试。',
                '-44':'验证码已失效，请重新获取。','-800':RATE_LIMIT_MESSAGE}.get(code,'星动日短信认证未通过（'+code[:20]+'）。')
            raise StarDayAuthError(message, 'SMS_REJECTED', 400)
        return value

    def send(self, snapshot):
        login_id, phone = snapshot['id'], snapshot['phone']
        if not re.fullmatch(r'1[3-9]\d{9}', phone):
            raise StarDayAuthError('该账号缺少有效的手机号。','INVALID_PHONE',400)
        with self.cache.account_lock(login_id):
            previous = self.pending.get(login_id)
            if previous and self.now()-previous['sent_at'] < 60:
                return {'sent':previous['sent'],'retry_after':max(1,int(60-self.now()+previous['sent_at'])),
                        'message':'验证码已发送，请勿重复获取。' if previous['sent'] else '上次发送结果未确认，请先检查短信，稍后再获取。'}
            if previous:
                previous['client'].close()
            client = self.session_factory()
            client.headers.update({**AJAX_HEADERS, 'User-Agent':WEB_UA})
            # Reserve before HTTP: a timeout can occur after the SMS was sent.
            record = {'client':client,'phone':phone,'sent_at':self.now(),'sent':False}
            self.pending[login_id] = record
            self._request(client,'/new/cmpSmsSend.action',{'phone':encrypt_sms_phone(phone,int(self.now()*1000)),'sspId':'75628'})
            record['sent'] = True
            return {'sent':True,'retry_after':60,'message':'验证码已发送到 '+phone[:3]+'****'+phone[-4:]+'。'}

    def login(self, snapshot, code):
        if not isinstance(code,str) or not re.fullmatch(r'\d{6}',code):
            raise StarDayAuthError('请输入 6 位短信验证码。','INVALID_SMS_CODE',400)
        login_id, phone = snapshot['id'], snapshot['phone']
        with self.cache.account_lock(login_id):
            record = self.pending.get(login_id)
            if not record or record['phone'] != phone or self.now()-record['sent_at'] > 600:
                raise StarDayAuthError('请先向该账号获取新的验证码。','SMS_REQUIRED',400)
            client = record['client']
            self._request(client,'/login.action',{'phone':encrypt_login_value(phone),'smsCode':encrypt_login_value(code)})
            if not any(c.name == 'access_token' and c.value for c in client.cookies.jar):
                raise StarDayAuthError('认证响应缺少星动日凭据，请稍后重试。','AUTH_FORMAT')
            saved = self.cache.save(login_id,phone,client,source='sms')
            self.pending.pop(login_id)
            if not self.cache.retain(login_id,saved,client):
                client.close()
            return {'message':'星动日短信登录成功，活动凭据已保存。'}

    def forget(self, login_id):
        with self.cache.account_lock(login_id):
            record = self.pending.pop(login_id,None)
            if record:record['client'].close()
