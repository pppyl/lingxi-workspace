"""Official Star Day SMS SM2(C1C2C3)/SM3 and AES-ECB, in Python."""
import base64
import secrets
import struct

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

P = 0xFFFFFFFEFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF00000000FFFFFFFFFFFFFFFF
A = P-3
N = 0xFFFFFFFEFFFFFFFFFFFFFFFFFFFFFFFF7203DF6B21C6052B53BBF40939D54123
G = (0x32C4AE2C1F1981195F9904466A39C9948FE30BBFF2660BE1715A4589334C74C7,
     0xBC3736A2F4F6779C59BDCEE36B692153D0A9877CC62A474002DF32E52139F0A0)
PUBLIC = (0xa1ae3a2f576a99b9999d22a707fa2f93c5921ac285094a0a0549a0ceb519ffc3,
          0x26b8a58b0e945b40cd699c92926632997722f3cc3efc5f3cb1b96e399cdd9d)


def _rol(value, count):
    count %= 32
    return ((value << count) | (value >> (32-count))) & 0xffffffff


def sm3(data):
    length = len(data)*8
    data += b'\x80'
    data += b'\0'*((56-len(data)) % 64)+struct.pack('>Q', length)
    state = [0x7380166f,0x4914b2b9,0x172442d7,0xda8a0600,0xa96f30bc,0x163138aa,0xe38dee4d,0xb0fb0e4e]
    for offset in range(0,len(data),64):
        words = list(struct.unpack('>16I',data[offset:offset+64]))
        for j in range(16,68):
            x = words[j-16]^words[j-9]^_rol(words[j-3],15)
            words.append(x^_rol(x,15)^_rol(x,23)^_rol(words[j-13],7)^words[j-6])
        a,b,c,d,e,f,g,h = state
        for j in range(64):
            ss1 = _rol((_rol(a,12)+e+_rol(0x79cc4519 if j < 16 else 0x7a879d8a,j)) & 0xffffffff,7)
            ss2 = ss1^_rol(a,12)
            ff = a^b^c if j < 16 else (a&b)|(a&c)|(b&c)
            gg = e^f^g if j < 16 else (e&f)|((~e)&g)
            t1 = (ff+d+ss2+(words[j]^words[j+4])) & 0xffffffff
            t2 = (gg+h+ss1+words[j]) & 0xffffffff
            d,c,b,a = c,_rol(b,9),a,t1
            h,g,f,e = g,_rol(f,19),e,t2^_rol(t2,9)^_rol(t2,17)
        state = [old^new for old,new in zip(state,(a,b,c,d,e,f,g,h))]
    return struct.pack('>8I',*state)


def _add(left, right):
    if left is None:return right
    if right is None:return left
    x,y = left
    u,v = right
    if x == u and (y+v) % P == 0:return None
    slope = ((3*x*x+A)*pow(2*y,-1,P) if left == right else (v-y)*pow(u-x,-1,P)) % P
    result = (slope*slope-x-u) % P
    return result,(slope*(x-result)-y) % P


def _multiply(scalar, point):
    result = None
    while scalar:
        if scalar & 1:result = _add(result, point)
        point = _add(point, point)
        scalar >>= 1
    return result


def encrypt_sms_phone(phone, timestamp):
    # The shipped sm2Encrypt first Base64-encodes UTF-8, then encrypts that text.
    message = base64.b64encode(f'{phone}:{timestamp}'.encode())
    while True:
        nonce = secrets.randbelow(N-1)+1
        c1 = _multiply(nonce,G)
        x,y = _multiply(nonce,PUBLIC)
        x,y = x.to_bytes(32,'big'),y.to_bytes(32,'big')
        mask = b''.join(sm3(x+y+i.to_bytes(4,'big')) for i in range(1,(len(message)+31)//32+1))[:len(message)]
        if any(mask):break
    c2 = bytes(a^b for a,b in zip(message,mask))
    return '04'+''.join(f'{n:064x}' for n in c1)+c2.hex()+sm3(x+message+y).hex()


def encrypt_login_value(value):
    padder = padding.PKCS7(128).padder()
    padded = padder.update(value.encode())+padder.finalize()
    cipher = Cipher(algorithms.AES(b'WX812FB36C5901D3'),modes.ECB()).encryptor()
    return base64.b64encode(cipher.update(padded)+cipher.finalize()).decode()
