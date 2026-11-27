import json
from pathlib import Path
import unittest

from features.checkin.kuaishou_xgs import sign


class KuaishouXgsTests(unittest.TestCase):
    def test_matches_original_module_vectors(self):
        fixture = Path(__file__).parent / 'fixtures/kuaishou_xgs_vectors.json'
        vectors = json.loads(fixture.read_text('utf-8'))['vectors']
        for row in vectors:
            data = bytes.fromhex(row['input_hex'])
            with self.subTest(length=len(data), prefix=data[:8].hex()):
                self.assertEqual(sign(data), row['expected'])

    def test_requires_encoded_input(self):
        with self.assertRaises(TypeError):
            sign('query=data')

    def test_repeated_initialization_has_no_external_state(self):
        first = sign(b'first-owned-fixture')
        second = sign(b'second-owned-fixture')
        self.assertNotEqual(first, second)
        self.assertEqual(first, sign(b'first-owned-fixture'))


if __name__ == '__main__':
    unittest.main()
