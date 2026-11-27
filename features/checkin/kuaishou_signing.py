"""The 13.3.10 SDK's four request signatures, initialized entirely in Python."""
import hashlib
import secrets
import struct
from threading import Lock
import time
import zlib

from .kuaishou_material import load
from .kuaishou_xgs import sign as sign_xgs


class KuaishouSigner:
    def __init__(self, *, nonce=None, clock=time.time):
        material = load()
        self.boxes = material[:40960]
        self.mixing = struct.unpack('<36864I', material[40960:188416])
        self.inner_pad, self.outer_pad = material[188416:188480], material[188480:188544]
        self.permutation = struct.unpack('<16I', material[188544:])
        self.nonce = secrets.randbelow(2**31 - 1) + 1 if nonce is None else nonce
        self.counter, self.clock, self.lock = 0, clock, Lock()

    def _block(self, block):
        state = list(block)
        for round_index in range(9):
            state = [state[index] for index in self.permutation]
            for column in range(4):
                word = 0
                for row in range(4):
                    position = column * 4 + row
                    word ^= self.mixing[round_index * 4096 + position * 256 + state[position]]
                state[column * 4:column * 4 + 4] = word.to_bytes(4, 'big')
        state = [state[index] for index in self.permutation]
        return bytes(self.boxes[0x9000 + position * 256 + value]
                     for position, value in enumerate(state))

    def core(self, message):
        value = hashlib.sha256(self.outer_pad + hashlib.sha256(self.inner_pad + message).digest()).digest()
        value += bytes([16]) * 16  # The 32-byte digest always needs a full PKCS#7 block.
        return b''.join(self._block(value[index:index + 16]) for index in range(0, 48, 16))

    def sign(self, path, query, *, client_salt, feature_bits, form=None):
        # OkHttp decodes query/form values, unions key=value entries, then uses
        # Java's UTF-16 lexicographic ordering before signing the UTF-8 bytes.
        entries = {key + '=' + value for key, value in [*query.items(), *(form or {}).items()]
                   if '__NS' not in key}
        canonical = ''.join(sorted(entries, key=lambda value: value.encode('utf-16-be'))).encode('utf-8')
        sig = hashlib.md5(canonical + b'772867c19925').hexdigest()
        core = self.core((path + sig).encode('utf-8'))
        with self.lock:
            self.counter += 1
            counter = self.counter
        flags = feature_bits
        attributes = ((flags >> 29) & 1) | ((flags >> 25) & 2) | ((flags >> 26) & 4)
        attributes |= ((flags >> 21) & 16) | ((flags >> 22) & 32) | ((flags >> 12) & 64) | 0xd00
        packet = bytearray(struct.pack('<2sHIIIII', b'AQ', 39, self.nonce, counter,
                                       zlib.crc32(core), int(self.clock()), attributes))
        total = sum(packet[:23])
        checksum = (total if total < 256 else -total) & 255
        for index in range(23):
            packet[index] ^= checksum ^ index
        packet[23] = checksum
        return {**query, 'sig': sig,
                '__NStokensig': hashlib.sha256((sig + client_salt).encode()).hexdigest(),
                '__NS_sig3': packet.hex(),
                '__NS_xfalcon': sign_xgs(path.encode('utf-8') + canonical)}


# SDK public tables, process nonce and counter survive ordinary page queries.
SIGNER = KuaishouSigner()
