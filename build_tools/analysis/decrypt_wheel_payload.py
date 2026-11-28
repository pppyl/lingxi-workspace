"""离线检查 Wheel 请求体，用已有的 AES 密钥验证并解密密文。"""

import argparse
import base64
import json
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('payload', type=Path, help='请求体 JSON 文件')
    parser.add_argument('--aes-key-file', type=Path, help='保存本次 AES-256 密钥的文件（64 个十六进制字符）')
    args = parser.parse_args()

    try:
        payload = json.loads(args.payload.read_bytes())
        if not isinstance(payload, dict) or not isinstance(payload.get('encryptedText'), str):
            raise ValueError('请求体应为包含字符串 encryptedText 的 JSON 对象。')
        envelope = base64.b64decode(payload['encryptedText'], validate=True)
        nonce_size = 12
        tag_size = 16
        if len(envelope) < nonce_size + tag_size:
            raise ValueError('encryptedText 的解码长度不足，无法容纳 nonce 和认证标签。')

        if args.aes_key_file is None:
            benefit_phone = payload.get('benefit_phone')
            result = {
                'benefit_phone_length': len(benefit_phone) if isinstance(benefit_phone, str) else None,
                'encryptedText_decoded_bytes': len(envelope),
                'decrypted': False,
            }
        else:
            key = bytes.fromhex(args.aes_key_file.read_text(encoding='utf-8-sig'))
            if len(key) != 32:
                raise ValueError('AES-256 密钥必须为 32 字节。')
            nonce = envelope[:nonce_size]
            ciphertext_and_tag = envelope[nonce_size:]
            plaintext = AESGCM(key).decrypt(nonce, ciphertext_and_tag, None)
            result = json.loads(plaintext)
    except InvalidTag:
        parser.exit(1, 'AES-GCM 认证失败，请核对密钥与密文是否来自同一次请求。\n')
    except (OSError, ValueError) as error:
        parser.exit(1, f'无法处理样本：{error}\n')

    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == '__main__':
    main()
