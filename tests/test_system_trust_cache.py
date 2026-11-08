# SPDX-License-Identifier: GPL-3.0-or-later
"""Trust cache bundle rules (synthetic data, no Apple content) and the ADT TrustCache slot."""
from pathlib import Path
import struct
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import system_trust_cache as trust  # noqa: E402
from native_devtree import configure_native_text_state  # noqa: E402


def module(uuid_byte, hashes, hash_type=2):
    entries = b''.join(h + bytes([hash_type, 0, 0, 0]) for h in sorted(hashes))
    return struct.pack('<I', 2) + bytes([uuid_byte]) * 16 + struct.pack('<I', len(hashes)) + entries


def node(name, properties=(), children=()):
    props = (('name', name.encode() + b'\0'), *properties)
    return (struct.pack('<II', len(props), len(children)) +
            b''.join(struct.pack('<32sI', k.encode(), len(v)) + v + bytes(-len(v) % 4) for k, v in props) +
            b''.join(children))


class SystemTrustCacheTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.dir = Path(folder.name)

    def test_bundle_check_matches_darwinboot_rules(self):
        good = trust.bundle(module(1, [b'\x10' * 20, b'\x20' * 20]))
        trust.check_bundle(good)
        bad = bytearray(good)
        bad[8 + 24:8 + 44], bad[8 + 48:8 + 68] = bad[8 + 48:8 + 68], bad[8 + 24:8 + 44]
        with self.assertRaisesRegex(ValueError, 'ascending'):
            trust.check_bundle(bytes(bad))

    def test_devtree_trust_cache_slot(self):
        path = self.dir / 'kernel.adt'
        path.write_bytes(node('device-tree', children=(node('chosen', children=(node('memory-map'),)),)))
        configure_native_text_state(path, development_root=False, trust_cache_slot=True)
        data = path.read_bytes()
        at = data.index(b'TrustCache')
        self.assertEqual(struct.unpack_from('<I', data, at + 32)[0], 16)
        self.assertEqual(data[at + 36:at + 52], bytes(16))
        missing = self.dir / 'none.adt'
        missing.write_bytes(node('device-tree', children=(node('chosen'),)))
        with self.assertRaisesRegex(ValueError, 'complete generated'):
            configure_native_text_state(missing, trust_cache_slot=True)


if __name__ == '__main__':
    unittest.main()
