"""APP SSO access to the official 'mycoupons' wallet.

Protocol observed in touch.10086.cn couponNew, ownAjax and appsso scripts.
Activity tokens and redeemable coupon numbers never leave this module.
"""
import base64
from contextlib import contextmanager, nullcontext
from datetime import datetime
import hashlib
import json
import re
import time
from urllib.parse import urljoin, urlsplit

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from http_transport import requests

from app_login import AppLoginError, AppLoginSession
from recharge.cmcc import APP_UA, SHANGHAI

ORIGIN = 'https://touch.10086.cn'
ENTRY = ORIGIN + '/i/mobile/mycoupons.html'
APP_TOKEN_PATH = '/leadeon-abilityopen-biz/BN/obtainToken/getBigNetToken'
SORTS = {'default': '2', 'recent': '0', 'oldest': '5', 'expiring': '1'}
CBC_KEY = b'043AOQGK6ykklyZA'
GCM_KEY = base64.b64decode('AXtET69BvHtbi8vItAcmPQ==')
DEFAULT_NONCE = b'fxvNMYHTkgoB'


class CouponError(Exception):
    def __init__(self, message, code='COUPON_ERROR', status=502):
        super().__init__(message)
        self.code, self.status = code, status


def _cbc_encrypt(text):
    padder = padding.PKCS7(128).padder()
    padded = padder.update(text.encode()) + padder.finalize()
    cipher = Cipher(algorithms.AES(CBC_KEY), modes.CBC(CBC_KEY)).encryptor()
    return base64.b64encode(cipher.update(padded) + cipher.finalize()).decode()


def _cbc_decrypt(text):
    cipher = Cipher(algorithms.AES(CBC_KEY), modes.CBC(CBC_KEY)).decryptor()
    padded = cipher.update(base64.b64decode(text)) + cipher.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return (unpadder.update(padded) + unpadder.finalize()).decode()


def _payload(body, msg_id='', *, millis=None, gcm=True):
    nonce, result = DEFAULT_NONCE, {}
    if msg_id:
        stamp = str(int(time.time()*1000) if millis is None else millis)
        marker = base64.b64encode(_cbc_encrypt(stamp + _cbc_decrypt(base64.b64decode(msg_id))).encode()).decode()
        result['msgId'], nonce = marker, marker.encode()[:12]
    plain = json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode()
    if not gcm:
        result['inParam'] = base64.b64encode(_cbc_encrypt(plain.decode()).encode()).decode()
        return result
    encrypted = nonce + AESGCM(GCM_KEY).encrypt(nonce, plain, None)
    result['inParam'] = base64.b64encode(base64.b64encode(encrypted)).decode()
    return result


def _unpack(response, *, gcm=False, allow_non_object=False):
    if str(response.get('retCode')) not in ('000000', '0'):
        code = str(response.get('retCode', 'UNKNOWN'))[:40]
        if code == '500003':
            raise CouponError('卡券会话未建立，请校验共享账号后重试。', 'COUPON_LOGIN_REQUIRED', 401)
        raise CouponError('官方未完成卡券查询（'+code+'），请稍后重试。', 'UPSTREAM_REJECTED')
    data = response.get('data')
    if isinstance(data, dict) and isinstance(data.get('outParam'), str):
        encoded = base64.b64decode(data['outParam'])
        if gcm:
            blob = base64.b64decode(encoded)
            plain = AESGCM(GCM_KEY).decrypt(blob[:12], blob[12:], None).decode()
        elif encoded.lstrip().startswith((b'{', b'[', b'"')):
            # ownAjax also accepts outParam containing Base64(JSON).
            plain = encoded.decode()
        else:
            plain = _cbc_decrypt(encoded)
        data = json.loads(plain)
    if not isinstance(data, dict):
        if allow_non_object:
            return {}
        raise CouponError('卡券服务未返回有效数据，请稍后重试。', 'UPSTREAM_FORMAT')
    return data


def _date(value):
    if not value:
        return ''
    text = str(value)
    if re.fullmatch(r'\d{14}', text):
        return datetime.strptime(text, '%Y%m%d%H%M%S').replace(tzinfo=SHANGHAI).isoformat()
    if re.fullmatch(r'\d{8}', text):
        return datetime.strptime(text, '%Y%m%d').replace(tzinfo=SHANGHAI).isoformat()
    return text


RECHARGE_CARD_TYPES = frozenset(('001', '002', '003', '005', '007', '008', '009'))
COUPON_PROTOCOLS = {
    'exchange': {'business':'hf01', 'card_types':frozenset(('004',)), 'category':None,
                 'submit_path':'/i/v1/pay/saveorder/', 'sms_template':None, 'pending_codes':()},
    'recharge': {'business':'hf01', 'card_types':RECHARGE_CARD_TYPES, 'category':None,
                 'submit_path':'/i/v1/pay/saveorderh5/', 'sms_template':None, 'pending_codes':()},
    'rights': {'business':'qy01', 'card_types':frozenset(('004',)), 'category':'ll01',
               'submit_path':'/i/v1/cust/rightsCouponsUse/', 'sms_template':'05', 'pending_codes':('550023',)},
    'flow': {'business':'ll01', 'card_types':frozenset(('001',)), 'category':None,
             'submit_path':'/i/v1/cust/flow/coupongive/', 'sms_template':'03', 'pending_codes':('571001',)},
}


def _item(row):
    business = str(row.get('busiType', ''))
    category = str(row.get('provPcardType') or business)
    code = str(row.get('couponStatus', ''))
    state = {'10': 'used', '7': 'invalid', '4': 'expired', '1': '未激活',
             '5': '已失效', '11': '已失效', '22': '已失效'}.get(code)
    if code == '8':
        state = '待生效' if str(row.get('timeFlag')) == '2' else 'available'
    state = state or ('官方状态 '+code if code else '状态未提供')
    use_kind = ''
    if state == 'available' and str(row.get('cardVersion')) == '2':
        card_type = str(row.get('pCardType'))
        use_kind = next((name for name, protocol in COUPON_PROTOCOLS.items()
            if business == protocol['business'] and card_type in protocol['card_types']
            and (protocol['category'] is None or category == protocol['category'])), '')
    identity = json.dumps([row.get(k) for k in ('pCardNo', 'pcardId', 'stallName', 'expireTime')], ensure_ascii=False)
    return {'id': hashlib.sha256(identity.encode()).hexdigest()[:24],
            'name': str(row.get('stallName') or '未命名卡券'),
            'category': 'fee' if category == 'hf01' else 'flow' if category == 'll01' else 'other',
            'amount': str(row.get('valueFormat') or row.get('couponValue') or ''),
            'unit': str(row.get('unitFormat') or row.get('unitName') or ''),
            'expires_at': _date(row.get('expireTime')),
            'received_at': '',
            'source': str(row.get('marketName') or ''), 'description': str(row.get('stallInfo') or ''),
            'status': state, 'use_kind': use_kind}


class CouponClient:
    def __init__(self, credentials_provider, *, session_factory=None, app_factory=AppLoginSession.from_credentials,
                 entry=ENTRY, credential_cache=None):
        self.credentials_provider = credentials_provider
        self.session_factory = session_factory or (lambda: requests.Session(impersonate='chrome'))
        self.app_factory = app_factory
        self.entry = entry
        self.credential_cache = credential_cache

    @staticmethod
    def _json(response):
        if response.status_code == 401:
            raise CouponError('卡券会话已失效，请重新校验活动登录。','COUPON_LOGIN_REQUIRED',401)
        if response.status_code != 200:
            raise CouponError('卡券服务 HTTP '+str(response.status_code), 'UPSTREAM_HTTP')
        value = response.json()
        if not isinstance(value, dict):
            raise CouponError('卡券服务响应格式异常。', 'UPSTREAM_FORMAT')
        return value

    def _authenticate(self,client,snapshot,current):
        _, value, _ = self.app_factory(snapshot).call(APP_TOKEN_PATH,
            {'sourceId': '019020', 'url': self.entry, 'cellNum': snapshot['phone']})
        code = str(value.get('retCode'))
        if code in ('400007', '420000'):
            raise CouponError('APP 登录已过期，请在账号管理重新登录。', 'APP_LOGIN_REQUIRED', 401)
        if code != '000000':
            raise CouponError('APP 未返回卡券认证令牌，请校验共享账号后重试。', 'APP_TOKEN_FAILED')
        body = value.get('rspBody')
        if isinstance(body, str):
            body = json.loads(body)
        if not isinstance(body, dict) or not body.get('token'):
            raise CouponError('APP 未返回卡券认证令牌，请校验共享账号后重试。', 'APP_TOKEN_FAILED')
        current()
        response = client.get(ORIGIN+'/i/v1/auth/getArtifact2', params={
            'channelID': '12014', 'backUrl': self.entry, 'token': body['token'],
        }, timeout=(10, 25), allow_redirects=False)
        for _ in range(8):
            if response.status_code not in (301, 302, 303, 307, 308):
                break
            url = urljoin(str(response.url), response.headers.get('location', ''))
            parts = urlsplit(url)
            if parts.scheme != 'https' or parts.netloc not in ('touch.10086.cn', 'login.10086.cn'):
                raise CouponError('卡券认证返回了不支持的跳转地址。', 'SSO_REDIRECT')
            response = client.get(url, timeout=(10, 25), allow_redirects=False)
        if response.status_code != 200:
            raise CouponError('卡券认证暂未完成，请稍后重试。', 'SSO_FAILED')
        return {}

    def _identity(self,client,phone):
        data = _unpack(self._json(client.get(ORIGIN+'/i/v1/auth/loginfo',timeout=(10,20),allow_redirects=False)))
        if str(data.get('userType')) == '2':
            raise CouponError('卡券账号尚未登录，请校验共享账号后重试。','COUPON_LOGIN_REQUIRED',401)
        login = str(data.get('loginValue') or '')
        number = login.replace('******',str(data.get('safeCode') or ''))
        if number != phone:
            raise CouponError('卡券认证返回的账号不一致，本次已停止。','ACCOUNT_MISMATCH',409)
        return {'number':number,'login_type':'0' if str(data.get('userType')) == '4' else '1',
                'province':str(data.get('provice') or ''),'msg_id':data.get('msgId') or ''}

    @contextmanager
    def _session(self, *, recover_login=True):
        snapshot = self.credentials_provider()
        if (not isinstance(snapshot, dict) or not re.fullmatch(r'1[3-9]\d{9}', str(snapshot.get('phone', '')))
                or not isinstance(snapshot.get('cookie'), str) or 'JSESSIONID=' not in snapshot['cookie']):
            raise CouponError('共享账号需要重新登录，请从左侧账号管理处理。', 'APP_LOGIN_REQUIRED', 401)
        def current():
            latest = self.credentials_provider()
            if (latest is None or latest.get('phone') != snapshot['phone']
                    or snapshot.get('id') and latest.get('id') != snapshot['id']):
                raise CouponError('账号已退出或已切换，本次卡券查询已停止。', 'APP_CREDENTIALS_CHANGED', 409)
        client = self.session_factory()
        client.headers.update({'User-Agent':APP_UA,'Referer':self.entry,'Origin':ORIGIN})
        def authenticate():
            current()
            return self._authenticate(client,self.credentials_provider(),current)
        try:
            if self.credential_cache is None:
                authenticate()
            else:
                self.credential_cache.connect('coupon-wallet:12014',snapshot,client,authenticate)
            # loginfo supplies a fresh flow-specific msgId. It must not be
            # cached along with the reusable wallet login cookies.
            for attempt in range(2):
                try:
                    identity = self._identity(client,snapshot['phone'])
                    break
                except CouponError as error:
                    if error.code != 'COUPON_LOGIN_REQUIRED' or not recover_login or self.credential_cache is None or attempt or not self.credential_cache.recover(client):raise
            current()
            yield client,identity,current
        except CouponError as error:
            if error.code == 'COUPON_LOGIN_REQUIRED' and self.credential_cache is not None and getattr(client,'_credential_binding',None):
                self.credential_cache.invalidate(client)
            raise
        except AppLoginError as error:
            raise CouponError('APP 卡券认证失败，请校验共享账号后重试。','APP_TOKEN_FAILED') from error
        except requests.RequestsError as error:
            raise CouponError('连接卡券服务失败，请稍后重试。','UPSTREAM_NETWORK') from error
        except (ValueError,TypeError,KeyError,InvalidTag) as error:
            raise CouponError('卡券服务响应无法解析，请稍后重试。','UPSTREAM_FORMAT') from error
        finally:
            client.close()

    def query(self, *, state='available', page=1, sort='default', wallet_session=None, recover_login=True):
        with (self._session(recover_login=recover_login) if wallet_session is None else nullcontext(wallet_session)) as (client, user, current):
            for attempt in range(2):
                try:
                    number = user['number']
                    client.headers.update({'payPhoneNo': base64.b64encode(_cbc_encrypt(number).encode()).decode()+number[5:7],
                                           'provcode': user['province'], 'Content-Type': 'application/json; charset=utf-8'})
                    queries = [dict(status='1,8,9,21', sortType=SORTS[sort])] if state == 'available' else [
                        dict(status=status, sortType=order, qryType=source, page=page-1)
                        for status, order in [('10', '3'), ('4,5,11,22', '4')] for source in ('0', '1')]
                    items, total, more = [], 0, False
                    for query in queries:
                        current()
                        body = {'loginNo': number, 'loginType': user['login_type'], 'channelId': '11',
                                'provCode': user['province'], 'busiType': '', 'pcardPasswd': '', **query}
                        response = client.post(ORIGIN+'/i/v1/cust/pcardAndGiftQry/'+number,
                            json=_payload(body, user['msg_id']), timeout=(10, 25), allow_redirects=False)
                        data = _unpack(self._json(response), gcm=True)
                        groups = data.get('busiData')
                        if not isinstance(groups, list):
                            raise CouponError('官方未返回卡券列表，本次查询未完成。', 'LIST_MISSING')
                        rows = [row for group in groups for row in group.get('batchInfo', [])]
                        items.extend(_item(row) for row in rows)
                        count = data.get('totalNum')
                        total = total+int(count) if total is not None and count is not None else None
                        if state == 'history':
                            pages = data.get('totlePage')
                            more |= page < int(pages) if pages is not None else bool(rows)
                    current()
                    return {'items': list({item['id']: item for item in items}.values()), 'page': page,
                            'total': total, 'has_more': more}
                except CouponError as error:
                    if error.code != 'COUPON_LOGIN_REQUIRED' or self.credential_cache is None or not getattr(client,'_credential_binding',None):
                        raise
                    if wallet_session is None and recover_login and not attempt and self.credential_cache.recover(client):
                        # Re-authentication also replaces the flow-specific msgId.
                        # Never replay a query encrypted with the old wallet's msgId.
                        current()
                        user = self._identity(client,number)
                        continue
                    self.credential_cache.invalidate(client)
                    raise
