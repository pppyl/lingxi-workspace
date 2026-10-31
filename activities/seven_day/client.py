"""Official 7-day golden-autumn campaign; independent COC session per account.

Protocol: dailyLoginEvent index-BsvlzBlx / daily-login.service-CCdoEszk,
and official rules version 20261028112703. Request construction and crypto
belong to this Python module; no browser is part of business execution.
"""
from __future__ import annotations

import base64
from datetime import datetime
import json
from pathlib import Path
import re
import secrets
import time

from cryptography.hazmat.primitives import padding, serialization
from cryptography.hazmat.primitives.asymmetric import padding as rsa_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from app_login import AppLoginError, AppLoginSession
from http_transport import requests
from recharge.cmcc import APP_UA, SHANGHAI
from star_day_auth import encrypt_uid
from .environment import collect

ORIGIN = 'https://dev.coc.10086.cn'
API = ORIGIN + '/coc/'
PAGE_ID = '2090341662452445184'
CHANNEL = 'P00000129418'
ENTRY = ORIGIN + '/coc/web6/dailyLoginEvent/?pageId=' + PAGE_ID + '&channelId=' + CHANNEL
CONFIG_URL = 'https://res.coc.10086.cn/res/cdn/coc1/fixedPath/' + PAGE_ID + '.js'
SIGN_ID = '2110926553960751104'
DRAW_ID = '2110928691993350144'
REWARD_ID = '2110923957812281344'
REWARD_SKU = '2110933605165395968'
REWARD_CODE = '130000010667'
REWARD_RIGHT = '67425'
REWARDS = (1, .6, .5, .5, .6, .7, 1.1)
DRAW_PRIZES = dict(zip([str(i) for i in range(130000010668, 130000010676)],
                      ['88元话费兑换券', '66元话费兑换券', '0.5元话费兑换券', '1元话费兑换券',
                       '2元话费兑换券', '1GB流量日包券', '2GB流量日包券', '5GB7日流量券']))
DRAW_PRIZES['999999999999'] = '未中奖'
PUBLIC_KEY = 'MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA0a66eNBd/ioaGnEM2AV+ctQX8VYt6dszzI8zOv26Kc1A7ex/cu/txmbB6WrU5ARoTeS0gk3KS21e/xP1BR1FUqL/+YJ4v8gpFY21/2gWuEPVBoHEd34FMpDRuiQkpmrvuuRF+QJQIg5ASEBpwz4n15ltl7Fd4E+NLp/JRJxzNR55ZeVLLFaucWYe8FIvX5BKx8i+sJWZYDoxTbjrBTy2CZDZObT+PxhOBXsCkolsG8Ux+tIAoU+UAVSQ0qT62z/sHPVPngHAqmLP/sF3IM6Mrub7BqhwJe3rvddqrw+Q2Jg8upZ/oUlZvXtQZCxJXtg0eJVdKTmnE6hD+/M99ukEoQIDAQAB'

DEFAULT_CAMPAIGN = {
    'title': '7日打卡分百万', 'start_at': '2026-10-31T09:00:00+08:00',
    'end_at': '2026-11-30T23:59:59+08:00',
    'rules_text': '连续打卡7天合计5元，每号活动期内仅领取一次，跨月累计；漏打清零。每天夺宝一次，次日0点刷新，不保证中奖。第7天领奖需短信验证；卡券24小时内到账，7天内使用。红包领完则整个活动提前结束。',
    'notice': '规则正文至12月31日，当前官方页面配置11月30日下线；本页跟随官方页面时间。',
}
DEFAULT_CAMPAIGN['rules_text'] = (Path(__file__).resolve().parent/'static/seven-day-rules.txt').read_text('utf-8')


class SevenDayError(Exception):
    def __init__(self, message, code='SEVEN_DAY_ERROR', status=502, *, uncertain=False):
        super().__init__(message)
        self.code, self.status, self.uncertain = code, status, uncertain


def _aes_encrypt(value, key):
    pad = padding.PKCS7(128).padder()
    plain = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()
    cipher = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return base64.b64encode(cipher.update(pad.update(plain) + pad.finalize()) + cipher.finalize()).decode()


def _encrypted(value, kind):
    key = secrets.token_hex(8).encode()
    public = serialization.load_der_public_key(base64.b64decode(PUBLIC_KEY))
    content = _aes_encrypt(value, key)
    if kind == 'meteor':
        header = {'coc-meteor': base64.b64encode(public.encrypt(base64.b64encode(key), rsa_padding.PKCS1v15())).decode(), 'coc-meteor-v': '2'}
        content = base64.b64encode(content.encode()).decode()
    else:
        header = {'Coc-En-Version': '1486234121417629696', 'coc-aurora-v': '2',
                  'coc-aurora': base64.b64encode(public.encrypt(key, rsa_padding.PKCS1v15())).decode()}
    return {'cocEnContent': content}, header, key


def _decode(response, key=None):
    if not isinstance(response, dict):
        raise SevenDayError('官方响应格式异常', 'UPSTREAM_FORMAT')
    if key is not None and response.get('returnData'):
        cipher = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
        unpad = padding.PKCS7(128).unpadder()
        raw = cipher.update(base64.b64decode(response['returnData'])) + cipher.finalize()
        response = json.loads((unpad.update(raw) + unpad.finalize()).decode())
    if str(response.get('code')) != '0':
        code = str(response.get('code', 'UNKNOWN'))[:60]
        message = str(response.get('message') or '官方暂未完成此操作')[:300]
        raise SevenDayError(message + '（' + code + '）', code, 401 if code == '40' else 502)
    return response.get('data')


def campaign_config(*, session=None):
    """Read the same CDN time gate as the official page, without signing in."""
    own = session is None
    client = session or requests.Session(impersonate='chrome')
    try:
        response = client.get(CONFIG_URL, params={'v': int(time.time() // 300)}, timeout=(5, 15))
        response.raise_for_status()
        match = re.search(r'var configData\s*=\s*(\{.*\})\s*\n', response.text)
        if not match:
            raise SevenDayError('官方活动时间配置格式异常', 'CONFIG_FORMAT')
        items = json.loads(match[1])['configItemList']
        fields = {key: value for item in items for key, value in item.items() if key.startswith('configContent_')}
        start, end = (datetime.strptime(fields['configContent_'+str(i)], '%Y/%m/%d %H:%M:%S').replace(tzinfo=SHANGHAI) for i in (0, 1))
        return {**DEFAULT_CAMPAIGN, 'start_at': start.isoformat(), 'end_at': end.isoformat()}
    except (requests.RequestsError, ValueError, KeyError) as error:
        raise SevenDayError('暂时无法更新官方活动时间，沿用已确认的期限', 'CONFIG_FAILED') from error
    finally:
        if own:
            client.close()


class SevenDayClient:
    def __init__(self, credentials_provider, *, session_factory=None, sdk_encrypt=None, credential_cache=None):
        self.credentials_provider = credentials_provider
        self.session_factory = session_factory or (lambda: requests.Session(impersonate='chrome'))
        self.sdk_encrypt = sdk_encrypt
        self.credential_cache = credential_cache
        self.client = None
        self.snapshot = None
        self.user = {}

    def current(self):
        latest = self.credentials_provider()
        if (latest is None or latest.get('phone') != self.snapshot.get('phone')
                or self.credential_cache is None and latest.get('cookie') != self.snapshot['cookie']):
            raise SevenDayError('共享账号登录已更新，请重新获取本次验证', 'APP_CREDENTIALS_CHANGED', 409)

    def connect(self):
        snapshot = self.credentials_provider()
        cookie = snapshot.get('cookie', '') if isinstance(snapshot, dict) else ''
        uid = re.search(r'(?:^|;)\s*UID=([^;]+)', cookie, re.I)
        if not uid or not re.search(r'(?:^|;)\s*JSESSIONID=', cookie, re.I) or not re.fullmatch(r'1[3-9]\d{9}', str(snapshot.get('phone', ''))):
            raise SevenDayError('共享账号需要重新登录，请在账号管理恢复登录', 'APP_LOGIN_REQUIRED', 401)
        self.snapshot = snapshot
        self.client = self.session_factory()
        self.client.headers.update({'User-Agent': APP_UA, 'Origin': ORIGIN, 'Referer': ENTRY,
                                    'Accept-Language': 'zh-CN,zh;q=0.9'})
        if self.credential_cache is None:
            self._authenticate(snapshot,uid[1])
        else:
            data = self.credential_cache.connect('seven-day',snapshot,self.client,
                lambda:self._authenticate(snapshot,uid[1]))
            self.user = data['user']
        return self

    def _authenticate(self,snapshot,uid):
        try:
            flow = AppLoginSession.from_credentials(snapshot)
            _, result, _ = flow.call('/biz-orange/LN/checkUIDAction/checkUID', {
                'channelId': '12093', 'domain': ORIGIN, 'url': ENTRY,
                'tel': snapshot['phone'], 'uId': uid,
            })
        except AppLoginError as error:
            raise SevenDayError('APP 活动认证暂未完成，请校验并重连', 'APP_UID_FAILED') from error
        if str(result.get('retCode')) != '000000':
            raise SevenDayError('APP 活动认证暂未完成（'+str(result.get('retCode'))[:40]+'）', 'APP_UID_FAILED')
        self.current()
        self.user = self.post('user/loginCheck', {'uid': encrypt_uid(uid), 'verifyType': 1}, encryption='aurora')
        # The shipped Jl() returns a growthInfo object even for level="";
        # Ba() checks that object, not whether its level is in V1–V5.
        # post() already checks the official response code. An empty level
        # must not become a local rejection before querying campaign progress.
        self.post('activities/growth/query', {'touchCode': 'P00000001090', 'provinceCode': self.user.get('provinceCode', '')})
        return {'user':self.user}

    def close(self):
        if self.client is not None:
            self.client.close()
            self.client = None

    def environment(self):
        # These are authenticated activity context values, not invented browser
        # hardware or automation-detection values. The host remains Python.
        return {**collect(), 'wasm_ver': '0.0.3',
                'page_url': ENTRY, 'hostname': 'dev.coc.10086.cn',
                'protocol': 'https:', 'port': '', 'referer': ENTRY, 'events': '',
                'user_agent': self.client.headers['User-Agent'], 'language': 'zh-CN',
                'languages': ['zh-CN'], 'on_line': True, 'cookie_enabled': True,
                'main_script': '', 'collection_time': int(time.time()*1000),
                'local_storage': {'benefit_phone': self.snapshot['phone'], 'pageId': PAGE_ID,
                                  'channelId': CHANNEL, 'userId': self.user.get('userId', '')},
                'cookie': {cookie.name: cookie.value for cookie in self.client.cookies.jar},
                'tls_fingerprint': {'has_subtle_crypto': False, 'is_secure_context': True,
                                    'protocol': 'https:', 'user_agent_contains_ssl': 'ssl' in APP_UA.lower()}}

    def post(self, path, payload, *, encryption=None, mutation=False, before_submit=None):
        key, headers, body = None, {}, payload
        if encryption in ('aurora', 'meteor'):
            body, headers, key = _encrypted(payload, encryption)
        elif encryption == 'wheel':
            from .crypto import encrypt, SevenDayCryptoError
            def transport(body, headers):
                self.current()
                try:
                    result = self.client.post(API+'wasmrc/gDT', json=body, headers=headers,
                                              timeout=(8, 20), allow_redirects=False)
                except requests.RequestsError as error:
                    raise SevenDayCryptoError('官方设备令牌服务连接失败，本次未提交业务请求。', 'DEVICE_TOKEN_NETWORK') from error
                if result.status_code != 200:
                    raise SevenDayCryptoError('官方设备令牌服务 HTTP '+str(result.status_code), 'DEVICE_TOKEN_HTTP')
                try:
                    return result.json()
                except ValueError as error:
                    raise SevenDayCryptoError('官方设备令牌响应格式异常', 'DEVICE_TOKEN_FORMAT') from error
            try:
                encrypted = (self.sdk_encrypt or encrypt)(payload, [], APP_UA, transport=transport, environment=self.environment)
            except SevenDayCryptoError as error:
                raise SevenDayError(str(error), error.code, error.status) from error
            body, headers = encrypted['data'], encrypted['headers']
        if mutation:
            self.current()
            if before_submit is not None:
                before_submit()
        try:
            response = self.client.post(API + path, json=body, headers=headers, timeout=(8, 20), allow_redirects=False)
        except requests.RequestsError as error:
            raise SevenDayError('官方连接中断，将先核实结果后再决定后续操作', 'UPSTREAM_NETWORK', uncertain=mutation) from error
        if response.status_code not in (200,401):
            raise SevenDayError('官方接口 HTTP ' + str(response.status_code), 'UPSTREAM_HTTP', uncertain=mutation)
        try:
            value = {'code':'40'} if response.status_code == 401 else response.json()
            return _decode(value, key if encryption == 'meteor' else None)
        except SevenDayError as error:
            if error.code == '40' and self.credential_cache is not None and getattr(self.client,'_credential_binding',None):
                if not mutation and path != 'user/loginCheck' and self.credential_cache.recover(self.client):
                    self.user = self.client._credential_data['user']
                    return self.post(path,payload,encryption=encryption)
                self.credential_cache.invalidate(self.client)
            raise
        except (ValueError, UnicodeError) as error:
            raise SevenDayError('官方返回了非预期内容，将先查询结果', 'UPSTREAM_FORMAT', uncertain=mutation) from error

    def inspect(self, now=None):
        now = now or datetime.now(SHANGHAI)
        marks = self.post('activities/prize/consecutiveClockInQuery', {'activityId': SIGN_ID})
        if not isinstance(marks, dict) or not isinstance(marks.get('clockInDateList'), list):
            raise SevenDayError('官方未返回完整打卡进度', 'PROGRESS_FORMAT')
        days = [str(d) for d in marks['clockInDateList']]
        orders = self.post('activities/prize/activityOrderList', {'activityId': REWARD_ID})
        chances = self.post('activities/prize/drawDayNum', {'activityId': DRAW_ID})
        remaining = int(chances['dayRemainNum'])
        draws = self.post('activities/prize/activityOrderList', {'activityId': DRAW_ID})
        today = now.strftime('%Y%m%d')
        recent = [d for d in (draws.get('activityOrderDTO') or []) if re.sub(r'\D', '', str(d.get('createTime', '')))[:8] == today]
        return {'date': now.date().isoformat(), 'days': len(days), 'dates': days,
                'total_amount': round(sum(REWARDS[:len(days)]), 1), 'checked_today': today in days,
                'completed': str(marks.get('taskCompleted')) == '0' or len(days) >= 7,
                'claimed': bool(orders.get('activityOrderDTO')), 'chances': max(0, remaining),
                'draw_prize': str(recent[-1].get('skuName') or '') if recent else '', 'eligible': True,
                'message': '已查询官方最新进度'}

    def signin(self, before_submit=None):
        # Retained by the official daily-login service, and verified on the
        # production campaign: this route advances consecutiveClockInQuery.
        # The clockIn/Wheel branch returns code10 despite a valid device token.
        return self.post('activities/prize/templatePrize', {'activityId': SIGN_ID},
                         mutation=True, before_submit=before_submit)

    def stock(self):
        data = self.post('activities/prize/stock', {'activityId': REWARD_ID, 'rightSkuId': REWARD_RIGHT})
        return int(data['stock'])

    def draw(self, before_submit=None):
        return self.post('activities/prize/templatePrize', {'activityId': DRAW_ID, 'touchCode': CHANNEL},
                         mutation=True, before_submit=before_submit)

    def send_reward_sms(self):
        return self.post('user/smsCode', {'mobile': self.snapshot['phone'], 'userId': self.user.get('userId', ''),
                         'smsCodeTypeEnum': '505', 'productCode': REWARD_CODE, 'pageId': PAGE_ID, 'touchCode': CHANNEL},
                         encryption='aurora', mutation=True)

    def claim(self, code, before_submit=None):
        extra = json.dumps({'activitySkuId': REWARD_SKU, 'smsCode': code, 'productCode': REWARD_CODE,
                            'touchCode': CHANNEL, 'rightSkuId': REWARD_RIGHT}, separators=(',', ':'))
        return self.post('activities/prize/templatePrize/new', {'lotteryId': REWARD_ID, 'jsonExtParam': extra,
                         'provinceCode': self.user.get('provinceCode') or '100'},
                         encryption='wheel', mutation=True, before_submit=before_submit)
