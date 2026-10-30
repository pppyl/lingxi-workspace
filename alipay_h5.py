"""Exchange an official signed order for Alipay's HTTPS cashier, without paying.

Packet format and fresh terminal defaults follow the bundled Alipay SDK.
This does not invoke an installed Alipay client or reuse a phone's device IDs.
"""
import base64
import gzip
import hashlib
import json
import locale
import platform
import re
import secrets
import string
import time
import uuid
import zlib
from urllib.parse import parse_qsl, urlsplit

from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
from cryptography.hazmat.primitives import padding, serialization
from cryptography.hazmat.primitives.asymmetric import padding as rsa_padding
from cryptography.hazmat.primitives.ciphers import Cipher, modes

from http_transport import requests

ENDPOINT = 'https://mobilegw.alipay.com/mgw.htm'
PUBLIC_KEY = ('MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDENksAVqDoz5SMCZq0bsZwE+I3NjrANyTT'
              'wUVSf1+ec1PfPB4tiocEpYJFCYju9MIbawR8ivECbUWjpffZq5QllJg+19CB7V5rYGcEnb/M7C'
              'S3lFF2sNcRFJUtXUUAqyR3/l7PmpxTwObZ4DLG258dhE2vFlVGXjnuLs+FI2hg4QIDAQAB')


class AlipayH5Error(Exception):
    def __init__(self, message, code='ALIPAY_H5_UNAVAILABLE'):
        super().__init__(message)
        self.code = code


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()


def _frame(parts):
    return b''.join(f'{len(part):05d}'.encode()+part for part in parts)


def _unframe(data):
    parts, offset = [], 0
    for _ in range(2):
        prefix = data[offset:offset+5]
        if len(prefix) != 5 or not prefix.isdigit():
            raise ValueError('Invalid packet length')
        length = int(prefix)
        offset += 5
        if offset+length > len(data):
            raise ValueError('Truncated packet')
        parts.append(data[offset:offset+length])
        offset += length
    if offset != len(data):
        raise ValueError('Unexpected trailing packet data')
    return parts


def _encrypt(data, key):
    padder = padding.PKCS7(64).padder()
    encryptor = Cipher(TripleDES(key), modes.CBC(bytes(8))).encryptor()
    return encryptor.update(padder.update(data)+padder.finalize())+encryptor.finalize()


def _decrypt(data, key):
    decryptor = Cipher(TripleDES(key), modes.CBC(bytes(8))).decryptor()
    unpadder = padding.PKCS7(64).unpadder()
    return unpadder.update(decryptor.update(data)+decryptor.finalize())+unpadder.finalize()


def _sdk_order(order):
    fields = dict(parse_qsl(order, keep_blank_values=True))
    if fields.get('method') != 'alipay.trade.app.pay' or not fields.get('sign'):
        raise AlipayH5Error('官方支付宝订单参数不完整，请重新获取支付链接。', 'ALIPAY_H5_ORDER_INVALID')
    if 'bizcontext' in fields:
        return order
    stamp = int(time.time()*1000)
    token = 'EP1'+hashlib.sha256((order+str(stamp)+str(uuid.uuid4())).encode()).digest()[:16].hex()+'_'+str(stamp)
    context = {'appkey': '2014052600006128', 'ty': 'and_lite', 'sv': 'h.a.3.8.14',
               'an': '', 'av': '', 'sdk_start_time': stamp,
               'extInfo': {'ap_link_token': token}, 'act_info': 'null'}
    # SDK appends unsigned bizcontext; never decode/re-encode the signed fields.
    return order+'&bizcontext='+_json(context).decode()


class AlipayH5:
    def __init__(self, *, session_factory=None):
        self.session_factory = session_factory or (lambda: requests.Session())

    def payment_url(self, order):
        order = _sdk_order(order)
        stamp = int(time.time()*1000)
        body = {'action': {'type': 'cashier', 'method': 'main'}, 'external_info': order,
                'tid': '', 'has_alipay': False, 'has_msp_app': False,
                'app_key': '2014052600006128', 'utdid': '',
                'new_client_key': format(stamp, 'x')[-10:], 'pa': ''}
        # The SDK's lite marker selects its web response contract. Unknown
        # device fields stay empty; OS/model describe this host, not a phone.
        fields = [platform.system()+' '+platform.release(), platform.system()+' '+platform.version(),
                  locale.getlocale()[0] or '', 'https', '0*0', '0', 'none', '-1', '-1', '1',
                  '000000000000000', '000000000000000', body['new_client_key'], '', platform.machine(),
                  'false', '00:00:00:00:00:00', '-1', '-1', 'sdk-and-lite',
                  format(stamp, 'x')+str(secrets.randbelow(9000)+1000),
                  format(stamp, 'x')+str(secrets.randbelow(9000)+1000), '-1', '00']
        body['user_agent'] = 'Msp/15.8.14 ('+';'.join(value.replace(';', ' ') for value in fields)+')'
        envelope = {'data': {'device': platform.machine(), 'namespace': 'com.alipay.mobilecashier',
                             'api_name': 'com.alipay.mcpay', 'api_version': '4.9.0', 'params': {}}}
        msp_fields = {}
        for part in order.split('&'):
            for name in ('biz_type', 'biz_no', 'trade_no', 'app_userid'):
                if name in msp_fields or name not in part or (name == 'trade_no' and part.startswith('out_trade_no')):
                    continue
                pieces = part.split('=')
                if len(pieces) > 1:
                    msp_fields[name] = pieces[1].replace('"', '')
        headers = {'msp-gzip': 'true', 'Operation-Type': 'alipay.msp.cashier.dispatch.bytes',
                   'Content-Type': 'application/octet-stream', 'Version': '2.0', 'AppId': 'TAOBAO',
                   'Msp-Param': ';'.join(name+'='+msp_fields[name] for name in
                       ('biz_type', 'biz_no', 'trade_no', 'app_userid') if msp_fields.get(name)),
                   'des-mode': 'CBC', 'User-Agent': 'msp', 'Accept-Charset': 'UTF-8'}
        public_key = PUBLIC_KEY
        with self.session_factory() as client:
            for attempt in range(2):
                key = ''.join(secrets.choice(string.ascii_letters+string.digits) for _ in range(24)).encode()
                try:
                    rsa_key = serialization.load_der_public_key(base64.b64decode(public_key, validate=True))
                    packet = _frame((_json(envelope), rsa_key.encrypt(key, rsa_padding.PKCS1v15()),
                                     _encrypt(gzip.compress(_json(body), compresslevel=6, mtime=0), key)))
                    response = client.post(ENDPOINT, data=packet, headers=headers,
                                           timeout=(10, 20), allow_redirects=False)
                except requests.RequestsError as cause:
                    raise AlipayH5Error('连接支付宝网页收银台失败，订单已保留，请重新获取链接。',
                                        'ALIPAY_H5_NETWORK') from cause
                if response.status_code != 200:
                    raise AlipayH5Error('支付宝网页网关暂未返回有效结果，请稍后获取链接。', 'ALIPAY_H5_HTTP')
                try:
                    raw_envelope, encrypted = _unframe(response.content)
                    params = json.loads(raw_envelope).get('data', {}).get('params', {})
                    replacement = params.get('public_key')
                    if replacement and attempt == 0:
                        public_key = replacement
                        continue
                    plain = _decrypt(encrypted, key) if encrypted else b'{}'
                    if encrypted and response.headers.get('msp-gzip', '').lower() == 'true':
                        plain = gzip.decompress(plain)
                    payload = json.loads(plain)
                    command = payload.get('form', {}).get('onload', {}).get('name', '')
                    for action in command.split(';'):
                        if not action.startswith(('js://wappay', 'loc:openweb')):
                            continue
                        first, last = action.find('('), action.rfind(')')
                        if first < 0 or last <= first:
                            raise ValueError('Invalid web command')
                        args = [v.strip().replace("'", '').replace('"', '') for v in
                                re.split(r"' *, *'", action[first+1:last])]
                        if action.startswith('js://wappay') and len(args) == 2 and args[1]:
                            raise AlipayH5Error('官方收银台还要求浏览器会话，请在 APP 继续此订单。', 'ALIPAY_H5_SESSION_REQUIRED')
                        if action.startswith('loc:openweb'):
                            config = json.loads(base64.b64decode(args[2]))
                            if config.get('method', 'POST').upper() != 'GET':
                                raise AlipayH5Error('官方返回的支付页需要表单提交，暂不能作为直接付款链接。', 'ALIPAY_H5_METHOD_UNSUPPORTED')
                        url = urlsplit(args[0])
                        if (url.scheme != 'https' or url.username or url.password
                                or not url.hostname or not (url.hostname == 'alipay.com' or url.hostname.endswith('.alipay.com'))
                                or url.port not in (None, 443)):
                            raise AlipayH5Error('官方未返回支持的支付宝 HTTPS 付款地址。', 'ALIPAY_URL_UNSUPPORTED')
                        return args[0]
                except (ValueError, IndexError, KeyError, TypeError, AttributeError, OSError, zlib.error) as cause:
                    raise AlipayH5Error('支付宝网页响应无法解析，订单已保留，请重新获取链接。', 'ALIPAY_H5_FORMAT') from cause
                raise AlipayH5Error('支付宝本次未返回网页收银台，请重新获取链接。')
        raise AlipayH5Error('支付宝网页密钥更新未完成，请重新获取链接。')
