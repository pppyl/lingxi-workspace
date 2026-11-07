"""新青年查询、活动验证码和兑换接口的 6daEXCun 封装。

参考“浏览器”项目的 request_record.py、payload_codec.py；只重建当前
接口的业务 query，不导入参考项目的 Cookie、账号或请求头。
"""

import base64
import json
from pathlib import Path
import re
import struct
from urllib.parse import quote, urlencode
import zlib

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from app_login import TripleDES
from runtime_paths import DATA_DIR


API_PATH = '/waps/apRight/initVerticalClassVip'
API_PATHS = {
    API_PATH,
    '/waps/rightSell/getSmsBusiCode',
    '/waps/rightSell/coupunRightOrder',
    '/waps/pointClaim/initQueryPointClaim',
    '/waps/pointClaim/conmitPointClaim',
}
PARAMETER_NAME = '6daEXCun'
CONFIG_PATH = DATA_DIR/'mobile-rights/gateway.json'
BUNDLED_CONFIG_PATH = Path(__file__).with_name('mobile_rights_gateway.json')
ALPHABET = 'qrcklmDoExthWJiHAp1sVYKU3RFMQw8IGfPO92bvLNj.7zXBaSnu0TC6gy_4Ze5d'
STANDARD_ALPHABET = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'


class GatewayError(ValueError):
    """诊断不包含密钥、加密参数或 Cookie 值。"""


def _blob(data):
    size = len(data)
    if size <= 127:
        prefix = bytes([size])
    elif size <= 16383:
        prefix = bytes([(size >> 8) | 128, size & 255])
    elif size <= 2097151:
        prefix = bytes([(size >> 16) | 192, (size >> 8) & 255, size & 255])
    elif size <= 268435455:
        prefix = bytes([(size >> 24) | 224]) + (size & 0xFFFFFF).to_bytes(3, 'big')
    elif size <= 0xFFFFFFFF:
        prefix = b'\xf0' + size.to_bytes(4, 'big')
    else:
        raise GatewayError('新青年业务参数长度超出支持范围')
    return prefix + data


def _checksum(query, api_path=API_PATH):
    # 对应原站 canonical(path + '?' + query)，保留非 ASCII 的百分号转义。
    canonical = quote(api_path + '?' + query, safe="~()*!.'-_")
    while True:
        previous = canonical
        canonical = re.sub(r'%([0-9a-fA-F]{2})',
                           lambda match: chr(int(match[1], 16)) if 32 <= int(match[1], 16) <= 126 else match[0],
                           canonical)
        if canonical == previous:
            break
    return zlib.crc32(canonical.upper().encode('utf-8')) & 0xFFFFFFFF


def _hex(data, name, size=None):
    value = data.get(name)
    if not isinstance(value, str) or not re.fullmatch(r'(?:[0-9a-fA-F]{2})*', value):
        raise GatewayError('新青年加密配置格式错误，请更新本地配置')
    result = bytes.fromhex(value)
    if size is not None and len(result) != size:
        raise GatewayError('新青年加密配置格式错误，请更新本地配置')
    return result


def _load_config(path, api_path):
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, UnicodeError, ValueError):
        raise GatewayError(f'移动权益加密配置无法读取，请检查 {Path(path).name} 或更新客户端') from None
    fields = {'parameter_name', 'compression_flag', 'inner_key_hex',
              'config32_hex', 'outer_header_hex', 'record'}
    if not isinstance(data, dict) or type(data.get('schema_version')) is not int:
        raise GatewayError('新青年加密配置格式错误，请更新本地配置')
    if data['schema_version'] == 1:
        if set(data) != fields | {'schema_version', 'api_path'} or data.get('api_path') != API_PATH:
            raise GatewayError('新青年加密配置与接口不匹配，请更新本地配置')
        if api_path != API_PATH:
            raise GatewayError('新青年验证码及兑换加密配置缺失，请更新 mobile-rights/gateway.json 或客户端')
        data = {name: data[name] for name in fields}
    elif data['schema_version'] == 2:
        profiles = data.get('profiles')
        if (set(data) != {'schema_version', 'profiles'} or not isinstance(profiles, dict)
                or not profiles or any(name not in API_PATHS for name in profiles)):
            raise GatewayError('新青年加密配置与接口不匹配，请更新本地配置')
        data = profiles.get(api_path)
        if not isinstance(data, dict):
            raise GatewayError('新青年当前接口的加密配置缺失，请更新 mobile-rights/gateway.json 或客户端')
    else:
        raise GatewayError('新青年加密配置版本无法识别，请更新本地配置')
    if (set(data) != fields or data.get('parameter_name') != PARAMETER_NAME
            or type(data.get('compression_flag')) is not int or data['compression_flag'] != 1):
        raise GatewayError('新青年加密配置与接口不匹配，请更新本地配置')
    key = _hex(data, 'inner_key_hex')
    if len(key) not in (16, 24, 32):
        raise GatewayError('新青年加密密钥配置无效，请更新本地配置')
    config32 = _hex(data, 'config32_hex', 32)
    header = _hex(data, 'outer_header_hex')
    record = data.get('record')
    if not isinstance(record, dict) or set(record) != {'flags', 'request_type', 'build_id', 'config_number', 'sequence', 'charset_hex'}:
        raise GatewayError('新青年请求记录配置无效，请更新本地配置')
    for name, maximum in [('flags', 0xFFFFFFFF), ('request_type', 0xFF), ('build_id', 0xFFFF),
                          ('config_number', 0xFFFFFFFF), ('sequence', 0xFFFFFFFFFFFFFFFF)]:
        if type(record.get(name)) is not int or not 0 <= record[name] <= maximum:
            raise GatewayError('新青年请求记录配置无效，请更新本地配置')
    if record['flags'] & 3 != 3:
        raise GatewayError('新青年请求记录缺少业务参数或校验配置')
    charset = _hex(record, 'charset_hex')
    if charset and not record['flags'] & 4:
        raise GatewayError('新青年请求记录的编码配置不一致')
    return key, config32, header, record, charset


def request_params(api_path, params, *, config_path=None, query_prefix=''):
    """每次以当前业务参数重算 CRC 和 AES，只返回一个加密查询参数。"""
    if not isinstance(api_path, str) or api_path not in API_PATHS:
        raise GatewayError('新青年加密接口无法识别')
    if query_prefix not in ('', 'undefined&'):
        raise GatewayError('网关查询前缀无效')
    if (not isinstance(params, dict) or PARAMETER_NAME in params
            or any(not isinstance(name, str) or not isinstance(value, str) for name, value in params.items())
            or not re.fullmatch(r'0\.\d{1,24}', params.get('ajax_randomcode', ''))):
        raise GatewayError('新青年加密参数无效，请检查业务参数与随机 Cookie')
    if config_path is None:
        config_path = CONFIG_PATH if CONFIG_PATH.exists() else BUNDLED_CONFIG_PATH
    key, config32, header, record, charset = _load_config(config_path, api_path)
    query = query_prefix + urlencode(params)
    request_record = struct.pack('>IBHIQ', record['flags'], record['request_type'],
                                 record['build_id'], record['config_number'], record['sequence'])
    request_record += _checksum(query, api_path).to_bytes(4, 'big') + _blob(query.encode('utf-8'))
    if record['flags'] & 4:
        request_record += _blob(charset)
    # 单层 ID11；不把已有 6daEXCun 再当作业务参数加密。
    plaintext = b'\x0b' + _blob(request_record)
    if len(plaintext) >= 16:
        plaintext = bytes(a ^ b for a, b in zip(plaintext[:16], config32[:16])) + plaintext[16:]
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plaintext)+padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(bytes(16))).encryptor()
    ciphertext = encryptor.update(padded)+encryptor.finalize()
    frame = b'\x01' + _blob(header) + _blob(config32) + _blob(ciphertext)
    envelope = (zlib.crc32(frame) & 0xFFFFFFFF).to_bytes(4, 'big') + frame
    payload = base64.b64encode(envelope).decode('ascii').rstrip('=').translate(str.maketrans(STANDARD_ALPHABET, ALPHABET))
    return {PARAMETER_NAME: payload}


def initialization_params(params, *, config_path=None):
    """兼容原初始化调用；其配置不能代替验证码或兑换接口的配置。"""
    return request_params(API_PATH, params, config_path=config_path)


def page_signature(page_url):
    """对应原页面 strToken(href, href[0:8], href[1:9], href[3:11])。"""
    if not isinstance(page_url, str) or not page_url.startswith('https://wap.gz.10086.cn/waps/'):
        raise GatewayError('新青年页面地址无法识别')
    # 原站 generateKeys 的 PC1 排列与标准 DES 不同；先转换密钥位序，
    # 再使用标准 DES，避免固定 ajax_code 与当前 rightId/vId 不匹配。
    standard_pc1 = (56, 48, 40, 32, 24, 16, 8, 0, 57, 49, 41, 33, 25, 17, 9, 1,
                    58, 50, 42, 34, 26, 18, 10, 2, 59, 51, 43, 35, 62, 54, 46, 38,
                    30, 22, 14, 6, 61, 53, 45, 37, 29, 21, 13, 5, 60, 52, 44, 36,
                    28, 20, 12, 4, 27, 19, 11, 3)
    site_pc1 = [8 * row + column for column in range(7) for row in range(7, -1, -1)]
    keys = []
    for text in (page_url[:8], page_url[1:9], page_url[3:11]):
        encoded = text.encode('utf-16-be', errors='surrogatepass')
        for cursor in range(0, len(encoded), 8):
            original = int.from_bytes(encoded[cursor:cursor + 8].ljust(8, b'\0'), 'big')
            transformed = 0
            for target, source in zip(standard_pc1, site_pc1):
                transformed |= ((original >> (63 - source)) & 1) << (63 - target)
            keys.append(transformed.to_bytes(8, 'big'))
    encoded = page_url.encode('utf-16-be', errors='surrogatepass')
    encoded += bytes((-len(encoded)) % 8)
    for key in keys:
        encryptor = Cipher(TripleDES(key*3), modes.ECB()).encryptor()
        encoded = encryptor.update(encoded)+encryptor.finalize()
    return encoded.hex().upper()
