"""Pure Python XGS business-module initialization and request signing.

Version-bound to SHA-256 d385c5119bbcaae6bfb895b6cb237851547d72bb1933428dec2ac070dc23d3b0.
No SDK library, VM, browser, phone or external signer is called.
"""
import base64
import struct


MASK = 0xffffffff
KEY = (4183807412, 394484062, 1106561997, 2378328696,
       630790222, 2546784104, 2891127470, 1922531795)
NONCE = (2215853858, 1643070585, 1849059804)
IV = (2837534710, 2845986804, 2436420605, 706843635,
      719254516, 2557931286, 2596197199, 2432949778)
SIGMA = ((0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15),
         (14,10,4,8,9,15,13,6,1,12,0,2,11,7,5,3),
         (11,8,12,0,5,2,15,13,10,14,3,6,7,1,9,4),
         (7,9,3,1,13,12,11,14,2,6,5,10,4,0,15,8),
         (9,0,5,7,2,4,10,15,14,1,11,12,6,8,3,13),
         (2,12,6,10,0,11,8,3,4,13,7,5,15,14,1,9),
         (12,5,1,15,14,13,4,10,0,7,6,3,9,2,8,11),
         (13,11,7,14,12,1,3,9,5,0,15,4,8,6,2,10),
         (6,15,14,9,11,3,0,8,12,2,13,7,1,4,10,5),
         (10,2,8,4,7,6,1,5,15,11,9,14,3,12,13,0))


def _transform(encoded):
    # The key and exact transitions come from the public library's decoder.
    seed = int.from_bytes(b'Vuz4fCHxn1CO'[4:8], 'big')
    state_a = state_b = state_c = seed
    output = bytearray()
    for value in encoded:
        bit_b, bit_c = state_b & 1, state_c & 1
        result = 0
        for _ in range(8):
            if state_a & 1:
                state_a = ((state_a ^ 0x40000031) | 0x80000000) & 0xffffffff
                bit_b = state_b & 1
                state_b = ((state_b ^ 0x20000010) | 0xc0000000) & 0xffffffff if bit_b else (state_b >> 1) & 0x3fffffff
            else:
                state_a = (state_a >> 1) & 0x7fffffff
                bit_c = state_c & 1
                state_c = ((state_c ^ 0x08000001) | 0xf0000000) & 0xffffffff if bit_c else (state_c >> 1) & 0x0fffffff
            result = ((result & 127) << 1) | bit_b ^ bit_c
        output.append(((result + 3) ^ value) & 255)
    return bytes(output)


def rol(value, count):
    return ((value << count) | (value >> (32 - count))) & MASK


def ror(value, count):
    return ((value >> count) | (value << (32 - count))) & MASK


def chacha_block(state):
    values = list(state)
    def quarter(a, b, c, d):
        values[a] = (values[a] + values[b]) & MASK
        values[d] = rol(values[d] ^ values[a], 16)
        values[c] = (values[c] + values[d]) & MASK
        values[b] = rol(values[b] ^ values[c], 12)
        values[a] = (values[a] + values[b]) & MASK
        values[d] = rol(values[d] ^ values[a], 8)
        values[c] = (values[c] + values[d]) & MASK
        values[b] = rol(values[b] ^ values[c], 7)
    for _ in range(10):
        for indices in [(0,4,8,12),(1,5,9,13),(2,6,10,14),(3,7,11,15),
                        (0,5,10,15),(1,6,11,12),(2,7,8,13),(3,4,9,14)]:
            quarter(*indices)
    return [value + original for value, original in zip(values, state)]


def chacha_encrypt(value):
    state = [394484062, 2378328696, 630790222, 1922531795, *KEY, 1, *NONCE]
    output = bytearray()
    for offset in range(0, len(value), 64):
        block = chacha_block(state)
        stream = struct.pack('<16I', *(word & MASK for word in block))
        output.extend(a ^ b for a, b in zip(value[offset:offset + 64], stream))
        state[12] += 1
    return bytes(output)


_environment = bytearray([14, 30, 0, 0])
for tag in range(1, 10):
    _environment.extend([tag, 2, int(tag == 2)])
_environment[2:] = bytes(byte ^ 35 for byte in _environment[2:])
_HEADER = b'HUDR_' + base64.b64encode(chacha_encrypt(bytes(_environment)))


def compress(h, block, length_words, compressed_words, final):
    # This module folds up to 64 uint32 words into 16 before the BLAKE2s rounds.
    folded = [0] * 8
    for index in range(length_words // 2):
        folded[index & 7] ^= struct.unpack_from('<Q', block, index * 8)[0]
    message = [part for value in folded for part in (value & MASK, value >> 32)]
    if length_words & 1:
        message[(length_words & 15) - 1] ^= struct.unpack_from('<I', block, length_words // 2 * 8)[0]
    values = list(h) + list(IV)
    values[12] ^= compressed_words
    if final:
        values[14] ^= MASK
    def mix(a, b, c, d, x, y):
        values[a] = (values[a] + values[b] + x) & MASK
        values[d] = ror(values[d] ^ values[a], 16)
        values[c] = (values[c] + values[d]) & MASK
        values[b] = ror(values[b] ^ values[c], 12)
        values[a] = (values[a] + values[b] + y) & MASK
        values[d] = ror(values[d] ^ values[a], 8)
        values[c] = (values[c] + values[d]) & MASK
        values[b] = ror(values[b] ^ values[c], 7)
    for sigma in SIGMA:
        for index, indices in enumerate([(0,4,8,12),(1,5,9,13),(2,6,10,14),(3,7,11,15),
                                         (0,5,10,15),(1,6,11,12),(2,7,8,13),(3,4,9,14)]):
            mix(*indices, message[sigma[index * 2]], message[sigma[index * 2 + 1]])
    return [(h[index] ^ values[index] ^ values[index + 8]) & MASK for index in range(8)]


def folded_blake(value):
    padded = value + bytes((-len(value)) % 4)
    words = len(padded) // 4
    h = list(IV)
    h[0] = 2820691926
    offset = 0
    compressed = 0
    while words > 64:
        compressed += 64
        h = compress(h, padded[offset:offset + 256], 64, compressed, False)
        offset += 256
        words -= 64
    h = compress(h, padded[offset:], words, compressed + words, True)
    return h


def sign(value):
    if not isinstance(value, bytes):
        raise TypeError('XGS input must be bytes')
    header = _HEADER
    words = folded_blake(value + header)
    digest_text = ''.join(f'{word:08x}' for word in words).encode('ascii')
    encrypted = _transform(digest_text[:16])
    key = bytes.fromhex('2dd345c0')
    encrypted = bytes(byte ^ key[index & 3] for index, byte in enumerate(encrypted))
    packet = bytearray(45)
    packet[:5] = b'KT\xcd\xab\xab'
    struct.pack_into('<Q', packet, 5, 159892789367)
    struct.pack_into('<Q', packet, 11, 504684793134995575)
    packet[19:22] = bytes([2, 0, 55])
    struct.pack_into('<I', packet, 22, (-1163093793) & MASK)
    packet[26:30] = encrypted[:4]
    struct.pack_into('<Q', packet, 30, 1689417985566)
    env = bytearray(struct.pack('<Q', 33 | (33 << 56)))
    env[0] = 90
    env_key = bytes.fromhex('7b563eda')
    for index in range(1, 8):
        env[index] ^= env_key[index & 3]
    packet[36:44] = env
    checksum = sum(packet[:44])
    checksum = (checksum if checksum <= 255 else -checksum) & 255
    packet[44] = checksum
    packet[:44] = bytes(byte ^ checksum for byte in packet[:44])
    prefix = struct.pack('<I', 1598379044)
    return (header + prefix + packet.hex().encode('ascii')).decode('ascii')

