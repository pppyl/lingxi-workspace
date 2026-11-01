"""Independent non-APP qwhdmark channel, reconstructed from official H5 assets.

The development inspector uses Scrapling to recover the current entry assets.
Runtime requests, AES, SMS login and signing run in Python on both platforms.
Never attach the shared APP Cookie to this channel's authenticated session.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import logging
import math
import re
from threading import RLock
import time
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from credential_cache import CredentialCache
from http_transport import requests
from recharge.cmcc import parse_server_time


ENTRY = 'https://wx.10086.cn/qwhdhub/qwhdmark/1021122301'
ORIGIN = 'https://wx.10086.cn'
BASE = ORIGIN + '/qwhdhub/api/mark/'
SCOPE = 'web-checkin:1021122301'
WEB_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
          '(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36')
KEY, IV = b'1234123412ABCDEF', b'ABCDEF1234123412'
READ_PATHS = {'user/info', 'info/commonInfo', 'info/prizeInfo'}


class WebCheckinError(Exception):
    def __init__(self, message, code='WEB_CHECKIN_ERROR', status=502, *, uncertain=False):
        super().__init__(message)
        self.code, self.status, self.uncertain = code, status, uncertain


def _encrypt(value):
    padder = padding.PKCS7(128).padder()
    raw = padder.update(value.encode()) + padder.finalize()
    encryptor = Cipher(algorithms.AES(KEY), modes.CBC(IV)).encryptor()
    return (encryptor.update(raw) + encryptor.finalize()).hex().upper()


def _decrypt(value):
    decryptor = Cipher(algorithms.AES(KEY), modes.CBC(IV)).decryptor()
    raw = decryptor.update(bytes.fromhex(value)) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return (unpadder.update(raw) + unpadder.finalize()).decode()


def _url(value, base=ENTRY):
    value = urljoin(base, value)
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or parsed.netloc != 'wx.10086.cn'
            or parsed.username or parsed.password):
        logging.getLogger(__name__).warning('Official login redirect not followed: scheme=%s host=%s path=%s',
                                           parsed.scheme,parsed.hostname,parsed.path)
        if parsed.hostname == 'open.weixin.qq.com':
            raise WebCheckinError('官方仍要求额外微信授权，尚未取得活动会话。',
                                  'WECHAT_AUTH_REQUIRED',409)
        raise WebCheckinError('该渠道需要官方授权，未取得可用的网页登录入口。', 'LOGIN_ENTRY', 409)
    return value


def _follow(client, url, *, sms_fallback=False):
    for _ in range(8):
        url = _url(url)
        response = client.get(url, timeout=(10, 25), allow_redirects=False)
        if response.status_code in (301, 302, 303, 307, 308):
            location = urljoin(url, response.headers.get('location', ''))
            parsed = urlsplit(location)
            if sms_fallback and parsed.scheme == 'https' and parsed.netloc == 'open.weixin.qq.com':
                location = _url(parse_qs(parsed.query).get('redirect_uri',[''])[0])
                if urlsplit(location).path != '/website/bind/bindAccount/new':
                    raise WebCheckinError('官方未返回短信登录入口，未发送验证码。','LOGIN_ENTRY',409)
                # Preserve the official activity callback when selecting its SMS page.
                # The channel is verified against the server after authentication.
                client.headers['User-Agent'] = WEB_UA
                sms_fallback = False
            url = _url(location, url)
            continue
        if response.status_code != 200:
            raise WebCheckinError('官方登录入口暂不可用，请稍后重试。', 'UPSTREAM_HTTP')
        return response, url
    raise WebCheckinError('官方登录跳转未完成，请重新登录此渠道。', 'LOGIN_ENTRY', 409)


def _snapshot(provider):
    value = provider()
    if not isinstance(value, dict) or not re.fullmatch(r'1[3-9]\d{9}', str(value.get('phone', ''))):
        raise WebCheckinError('请先在共享账号池添加有效号码。', 'APP_LOGIN_REQUIRED', 401)
    return value


def _json(response):
    if response.status_code == 401:
        raise WebCheckinError('该渠道会话已失效，请恢复本渠道授权。', 'ACTIVITY_LOGIN_REQUIRED', 401)
    if response.status_code != 200:
        raise WebCheckinError('官方服务暂不可用，请稍后重试。', 'UPSTREAM_HTTP')
    try:
        try:
            value = response.json()
        except ValueError:
            raw = response.text.strip()
            if not re.fullmatch(r'(?:[0-9a-fA-F]{32})+',raw):
                raise
            value = json.loads(_decrypt(raw))
        if isinstance(value, str):
            value = json.loads(_decrypt(value))
    except (ValueError, TypeError) as error:
        raise WebCheckinError('官方响应格式变化，请稍后重试。', 'UPSTREAM_FORMAT') from error
    if not isinstance(value, dict):
        raise WebCheckinError('官方未返回完整业务结果。', 'UPSTREAM_FORMAT')
    return value


def _identity(data, snapshot, expected_channel='dlwmh'):
    if not isinstance(data, dict):
        raise WebCheckinError('未取得该渠道身份，请重新登录。', 'UPSTREAM_FORMAT')
    mobile, nick = str(data.get('mobile') or ''), data.get('nickName')
    same = mobile == snapshot['phone'] if re.fullmatch(r'1[3-9]\d{9}', mobile) else (
        bool(mobile) and nick == snapshot['phone'][:3]+'****'+snapshot['phone'][-4:])
    if not same:
        raise WebCheckinError('官方返回的号码与当前账号不一致，未保存登录。', 'ACCOUNT_MISMATCH', 409)
    if data.get('channel') != expected_channel:
        raise WebCheckinError('官方返回登录渠道 '+str(data.get('channel') or '未知')[:40]
                              +'，与当前渠道不一致，未保存或使用此会话。', 'CHANNEL_MISMATCH', 409)
    return data


class WebCheckinClient:
    def __init__(self, credentials_provider, *, credential_cache=None, session_factory=None,
                 scope=SCOPE, channel='dlwmh', user_agent=WEB_UA):
        self.credentials_provider = credentials_provider
        self.credential_cache = credential_cache or CredentialCache()
        self.session_factory = session_factory or (lambda: requests.Session(impersonate='chrome'))
        self.scope, self.channel, self.user_agent = scope, channel, user_agent
        self.login_message = '该渠道尚未登录或会话已失效，请使用本页短信登录。'

    def _post(self, client, path, *, writing=False, before_submit=None):
        if path not in READ_PATHS | {'do/mark'} or writing != (path == 'do/mark'):
            raise ValueError('Unsupported web check-in request')
        if writing and before_submit is not None:
            before_submit()
        try:
            response = client.post(BASE+path, json={}, headers={
                'Content-Type':'application/json;charset=UTF-8',
                'x-requested-with':'XMLHttpRequest', 'login-check':'1'},
                timeout=(10, 8 if writing else 25), allow_redirects=False)
            value = _json(response)
        except requests.RequestsError as error:
            raise WebCheckinError('提交结果待核实，不会自动重发。' if writing else '连接签到服务失败，请稍后重试。',
                'RESULT_UNCERTAIN' if writing else 'UPSTREAM_NETWORK', uncertain=writing) from error
        except WebCheckinError as error:
            if writing:
                error.uncertain = True
            raise
        if value.get('code') == 'GOTO_LOGIN':
            if hasattr(client, '_credential_binding'):
                self.credential_cache.invalidate(client)
            raise WebCheckinError(self.login_message, 'ACTIVITY_LOGIN_REQUIRED', 401)
        if value.get('code') != 'SUCCESS':
            code = str(value.get('status') or value.get('code') or 'UPSTREAM_FAILED')
            message = {
                'PRIZE_NO_CONFIG':'签到成功，官方未配置今日奖品。',
                'USER_LIMITED':'官方限制该号码参与，今日不再自动重试。',
                'NOT_TIME_IN':'当前不在该渠道的活动时间内。',
                'PRIZE_LIMIT_DAY':'今日领取次数已用完，请核实今日签到记录。',
            }.get(code, '官方暂未允许此操作（'+code[:40]+'）。')
            if re.search('FREQUEN|LIMIT|RISK|CAPTCHA|VERIFY', code, re.I):
                message += '请在官方页面核实；请求频繁可尝试更换服务ip再试。'
            raise WebCheckinError(message, code, 409)
        return value.get('data')

    @contextmanager
    def _session(self):
        snapshot = _snapshot(self.credentials_provider)
        client = self.session_factory()
        client.headers.update({'User-Agent':self.user_agent, 'Origin':ORIGIN, 'Referer':ENTRY})
        try:
            def missing():
                raise WebCheckinError(self.login_message, 'ACTIVITY_LOGIN_REQUIRED', 401)
            self.credential_cache.connect(self.scope, snapshot, client, missing)
            yield client, snapshot
        finally:
            client.close()

    def _inspect(self, client, snapshot):
        identity = _identity(self._post(client, 'user/info'), snapshot, self.channel)
        common = self._post(client, 'info/commonInfo')
        now = parse_server_time(common.get('sysTime')) if isinstance(common, dict) else None
        if now is None:
            raise WebCheckinError('官方未返回服务器时间，本次未提交。', 'SERVER_TIME_MISSING')
        progress = self._post(client, 'info/prizeInfo')
        if (not isinstance(progress, dict) or progress.get('todayMarked') not in (True, False, 0, 1)
                or 'todayMarked' not in progress or isinstance(progress.get('markedTimes'), bool)
                or not isinstance(progress.get('markedTimes'), int)
                or isinstance(progress.get('totalMarkTimes'), bool)
                or not isinstance(progress.get('totalMarkTimes'), int)
                or progress['markedTimes'] < 0 or progress['totalMarkTimes'] <= 0
                or not isinstance(progress.get('prizes', []), list)):
            raise WebCheckinError('官方未返回完整的渠道签到进度，本次未提交。', 'PROGRESS_MISSING')
        signed = bool(progress['todayMarked'])
        return {'today_signed':signed, 'sign_date':now.strftime('%Y-%m-%d') if signed else '',
            'marked_times':progress['markedTimes'], 'total_times':progress['totalMarkTimes'],
            'cycle_complete':progress['markedTimes'] >= progress['totalMarkTimes'],
            'channel':identity['channel'], 'server_time':now.isoformat(),
            'prizes':[{'name':str(item.get('name') or item.get('prizeName') or '')[:160],
                       'status':str(item.get('status') or '')[:40]}
                      for item in progress.get('prizes', []) if isinstance(item, dict)],
            'message':'今日已签到' if signed else '本周期签到已完成' if progress['markedTimes'] >= progress['totalMarkTimes'] else '今日尚未签到'}

    def status(self):
        with self._session() as (client, snapshot):
            return self._inspect(client, snapshot)

    def signin(self, before_submit=None):
        with self._session() as (client, snapshot):
            state = self._inspect(client, snapshot)
            if state['today_signed'] or state['cycle_complete']:
                return state
            latest = _snapshot(self.credentials_provider)
            if latest['phone'] != snapshot['phone']:
                raise WebCheckinError('当前号码已变化，本次没有提交签到。', 'ACCOUNT_MISMATCH', 409)
            self._post(client, 'do/mark', writing=True, before_submit=before_submit)
            try:
                state = self._inspect(client, snapshot)
            except WebCheckinError as error:
                raise WebCheckinError('签到已提交，等待核实官方进度，不会重复提交。',
                                      'RESULT_UNCERTAIN', uncertain=True) from error
            if not state['today_signed']:
                raise WebCheckinError('签到已提交，官方进度尚未更新，不会重复提交。',
                                      'RESULT_UNCERTAIN', uncertain=True)
            state['message'] = '签到成功，已核实该渠道今日记录'
            return state


class WebLoginManager:
    def __init__(self, credentials, cache, *, session_factory=None, now=time.monotonic,
                 scope=SCOPE, channel='dlwmh', user_agent=WEB_UA):
        self.credentials, self.cache = credentials, cache
        self.session_factory = session_factory or (lambda: requests.Session(impersonate='chrome'))
        self.now, self.lock, self.locks, self.flows = now, RLock(), {}, {}
        self.scope, self.channel, self.user_agent = scope, channel, user_agent

    def _lock(self, login_id):
        with self.lock:
            return self.locks.setdefault(login_id, RLock())

    def _begin(self, snapshot):
        client = self.session_factory()
        client.headers.update({'User-Agent':self.user_agent, 'Referer':ENTRY, 'Origin':ORIGIN})
        try:
            response, url = _follow(client, ORIGIN+'/qwhdsso/login?'+urlencode({'dlwmh':'true','actUrl':ENTRY}),
                                    sms_fallback=self.channel == 'wechat')
            csrf = re.search(r"\bcsrf\s*=\s*['\"]([^'\"]+)['\"]", response.text)
            template = re.search(r"\breqTmp\s*=\s*['\"]([^'\"]+)['\"]", response.text)
            params = parse_qs(urlsplit(url).query)
            redirect = params.get('redirectUrl', [''])[0]
            if not csrf or not template or params.get('redirectSource') != ['SSO_YQS'] or not redirect:
                raise WebCheckinError('官方网页登录规则已变化，未发送验证码。', 'LOGIN_ENTRY', 409)
            redirect = _url(redirect)
            if urlsplit(redirect).path != '/qwhdsso/redirect':
                raise WebCheckinError('官方登录返回了其他活动入口，未发送验证码。', 'LOGIN_ENTRY', 409)
            bind_source = params.get('bindSource', [''])[0]
            if not bind_source and params.get('activityId'):
                bind_source = 'A'+params['activityId'][0]
            return {'client':client, 'snapshot':snapshot, 'headers':{'csrf-token':csrf[1], 'reqTmp':template[1]},
                    'redirect':redirect, 'bind_source':bind_source,
                    'created_at':self.now(), 'sent_at':None, 'sms_sent':False}
        except BaseException as error:
            client.close()
            if isinstance(error, requests.RequestsError):
                raise WebCheckinError('连接官方网页登录入口失败，请稍后重试。', 'UPSTREAM_NETWORK') from error
            raise

    def sms(self, login_id):
        with self._lock(login_id):
            snapshot = _snapshot(lambda:self.credentials(login_id))
            flow = self.flows.get(login_id)
            if flow and flow['sent_at'] is not None and self.now()-flow['sent_at'] < 60:
                error = WebCheckinError('验证码已发送，请稍后再获取。', 'SMS_COOLDOWN', 429)
                error.retry_after = math.ceil(60-(self.now()-flow['sent_at']))
                raise error
            if flow is None or self.now()-flow['created_at'] >= 600 or flow['snapshot']['phone'] != snapshot['phone']:
                if flow:
                    flow['client'].close()
                flow = self._begin(snapshot)
                self.flows[login_id] = flow
            flow['sent_at'] = self.now()
            try:
                response = flow['client'].post(ORIGIN+'/website/bind/sendMsg',
                    data={'telephone':_encrypt(snapshot['phone']), 'riskToken':''},
                    headers=flow['headers'], timeout=(10, 25), allow_redirects=False)
                value = _json(response)
            except requests.RequestsError as error:
                raise WebCheckinError('验证码请求未确认，请稍后再试。', 'UPSTREAM_NETWORK') from error
            if str(value.get('status')) != '0':
                code = str(value.get('status') or 'SMS_FAILED')
                raise WebCheckinError('官方未允许发送验证码（'+code[:40]+'），请稍后再试；请求频繁可尝试更换服务ip再试。', code, 409)
            flow['sms_sent'] = True
            return {'retry_after':60, 'message':'验证码已发送到当前号码，请输入本次验证码。'}

    def login(self, login_id, code):
        if not isinstance(code, str) or not re.fullmatch(r'\d{6}', code):
            raise WebCheckinError('请输入本次收到的六位验证码。', 'SMS_INVALID', 400)
        with self._lock(login_id):
            snapshot = _snapshot(lambda:self.credentials(login_id))
            flow = self.flows.get(login_id)
            if (not flow or not flow['sms_sent'] or self.now()-flow['created_at'] >= 600
                    or flow['snapshot']['phone'] != snapshot['phone']):
                raise WebCheckinError('请重新获取该渠道验证码。', 'SMS_EXPIRED', 409)
            client = flow['client']
            try:
                response = client.post(ORIGIN+'/website/bind/bindAccount/new', json={
                    'telephone':_encrypt(snapshot['phone']), 'password':_encrypt(code), 'pwdType':'02',
                    'checkCtrol':1, 'bindSource':flow['bind_source'], 'sourcePage':'WMH_登录',
                    'ys':'', 'ysTitle':'', 'constId':''}, headers=flow['headers'], timeout=(10, 25), allow_redirects=False)
                value = _json(response)
                if str(value.get('status')) != '0':
                    code = str(value.get('status') or 'LOGIN_FAILED')
                    message = {'6002':'验证码已过期，请重新获取。', '6001':'验证码有误，请核对后输入。',
                        '6005':'验证码有误，请核对后输入。', '6006':'验证码错误次数过多，请重新获取。',
                        '2046':'官方已锁定该号码，请勿反复尝试。', '8009':'官方已锁定该号码，请勿反复尝试。'}
                    raise WebCheckinError(message.get(code, '该渠道登录未完成（'+code[:40]+'），请在官方核实。'), code, 409)
                response = client.post(ORIGIN+'/website/api/singleAuth',
                    params={'redirectSource':'SSO_YQS', 'redirectUrl':flow['redirect']},
                    headers=flow['headers'], timeout=(10, 25), allow_redirects=False)
                if response.status_code != 200:
                    raise WebCheckinError('网页登录完成，但活动换票未完成，请重新登录此渠道。', 'SSO_FAILED')
                token = response.text.strip()
                if token.startswith('"'):
                    token = json.loads(token)
                if not isinstance(token, str) or not token or token.startswith(('<','{','[')):
                    raise WebCheckinError('官方未返回活动换票令牌。', 'SSO_FAILED')
                client.headers['User-Agent'] = self.user_agent
                try:
                    _follow(client, flow['redirect']+'&'+urlencode({'wmhToken':token}))
                except WebCheckinError as error:
                    if error.code != 'WECHAT_AUTH_REQUIRED':
                        raise
                    # A landing-page OAuth redirect is not itself proof that the
                    # activity cookie was not issued. Check its identity once.
                identity = _json(client.post(BASE+'user/info', json={},
                    headers={'Content-Type':'application/json;charset=UTF-8','login-check':'1'},
                    timeout=(10, 25), allow_redirects=False))
                if identity.get('code') != 'SUCCESS':
                    raise WebCheckinError('该渠道认证未完成，请重新登录。', 'ACTIVITY_LOGIN_REQUIRED', 401)
                identity = _identity(identity.get('data'), snapshot, self.channel)
                self.cache.forget(login_id, scope=self.scope)
                self.cache.connect(self.scope, snapshot, client, lambda:identity, headers=('User-Agent',))
            except requests.RequestsError as error:
                raise WebCheckinError('连接网页登录服务失败，本次未自动重复登录。', 'UPSTREAM_NETWORK') from error
            else:
                client.close()
                self.flows.pop(login_id, None)
                return {'message':'该渠道已登录，已保留独立活动会话。'}

    def forget(self, login_id):
        with self._lock(login_id):
            flow = self.flows.pop(login_id, None)
            if flow:
                flow['client'].close()
            self.cache.forget(login_id, scope=self.scope)

    def close(self):
        for login_id in list(self.flows):
            with self._lock(login_id):
                flow = self.flows.pop(login_id, None)
                if flow:
                    flow['client'].close()
