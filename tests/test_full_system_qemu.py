import hashlib
import importlib.util
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import full_system_qemu as full  # noqa: E402
import inspect_macos26_full_image as inspector  # noqa: E402

spec = importlib.util.spec_from_file_location('image_fixture', ROOT / 'tests/test_inspect_macos26_full_image.py')
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


def variant(*, root_to_xid=4, data_flags=1):
    payload = bytearray(fixture.container(data_flags=data_flags))
    at = 6 * fixture.BLOCK
    struct.pack_into('<Q', payload, at + 968, root_to_xid)
    payload[at:at + 8] = inspector.checksum(payload[at:at + fixture.BLOCK])
    return fixture.gpt_disk(bytes(payload))


class FullSystemQemuTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.dir = Path(folder.name)

    def image(self, data, name='disk.img'):
        image = self.dir / name
        image.write_bytes(data)
        report = self.dir / (name + '.json')
        report.write_text(json.dumps(inspector.inspect(image)))
        return image, report

    def test_accepts_new_unsealed_root_snapshot(self):
        image, report = self.image(variant())
        identity = full.qualify_inspection(image, report)
        self.assertEqual(identity['system_volume'], fixture.SYSTEM)
        self.assertEqual(identity['volume_group'], fixture.GROUP)
        self.assertEqual(identity['root_snapshot'], 'com.apple.root.MODIFIED')
        self.assertEqual(identity['sealed_snapshots'], ['com.apple.os.update-ORIGINAL'])
        described = full.describe(identity)
        self.assertEqual(described['system_volume'], str(fixture.SYSTEM).upper())
        json.dumps(described)

    def test_refuses_original_sealed_or_missing_root_snapshot(self):
        for root, name in ((2, 'sealed.img'), (0, 'none.img')):
            image, report = self.image(variant(root_to_xid=root), name)
            with self.assertRaisesRegex(ValueError, 'new unsealed snapshot'):
                full.qualify_inspection(image, report)

    def test_refuses_encrypted_data(self):
        image, report = self.image(variant(data_flags=0))
        with self.assertRaisesRegex(ValueError, 'encrypted'):
            full.qualify_inspection(image, report)

    def test_refuses_changed_image_or_foreign_report(self):
        image, report = self.image(variant())
        status = image.stat()
        os.utime(image, ns=(status.st_atime_ns, status.st_mtime_ns + 1_000_000_000))
        with self.assertRaisesRegex(ValueError, 'unchanged image'):
            full.qualify_inspection(image, report)
        other, _ = self.image(variant(), 'other.img')
        with self.assertRaisesRegex(ValueError, 'unchanged image'):
            full.qualify_inspection(other, report)

    def test_blank_first_disk_is_new_and_empty(self):
        path = self.dir / 'first.raw'
        record = full.create_blank_disk(path)
        self.assertEqual(path.read_bytes(), bytes(full.BLANK_BYTES))
        self.assertEqual(record['sha256'], hashlib.sha256(path.read_bytes()).hexdigest())
        with self.assertRaises(FileExistsError):
            full.create_blank_disk(path)

    def test_disk_arguments_keep_the_image_read_only_and_root_by_volume(self):
        image, report = self.image(variant())
        identity = full.qualify_inspection(image, report)
        blank = self.dir / 'first.raw'
        disks = full.qemu_disk_arguments(identity, blank, self.dir / 'qemu.dtb', 4096, 8)
        blockdevs = [json.loads(disks['fdt_command'][i + 1])
                     for i, value in enumerate(disks['fdt_command']) if value == '-blockdev']
        self.assertEqual([b['file']['filename'] for b in blockdevs], [str(blank), str(image.resolve())])
        self.assertTrue(all(b['read-only'] and b['file']['read-only'] for b in blockdevs))
        self.assertIn('virtio-blk-device,drive=isc-disk,bus=virtio-mmio-bus.0', disks['fdt_command'])
        self.assertEqual(disks['dt_arguments'][2:], ['--qemu-scratch-mmio', '0xa000000',
                                                     '--root-match-volume-uuid', str(fixture.SYSTEM).upper()])
        self.assertEqual(disks['gdb_stop_arguments'], ['--virtio-block-image', str(blank),
                                                       '--virtio-extra-block-image', str(image.resolve()),
                                                       '--virtio-extra-block-overlay'])

    def provenance(self, sample, offset, size):
        data = sample.read_bytes()
        return dict(schema=full.LAUNCHD_SCHEMA, build='25F71', source_image=full.OS_IMAGE_NAME,
                    source_image_sha384=full.OS_IMAGE_SHA384, member='sbin/launchd', bytes=len(data),
                    sha256=hashlib.sha256(data).hexdigest(),
                    arm64e_slice=dict(offset=offset, bytes=size,
                                      sha256=hashlib.sha256(data[offset:offset + size]).hexdigest()))

    def test_launchd_sample_requires_verified_provenance(self):
        sample = self.dir / 'launchd'
        sample.write_bytes(b'\xcf\xfa\xed\xfe' + bytes(60))
        good = self.provenance(sample, 0, 64)
        record = Path(str(sample) + '.json')
        record.write_text(json.dumps(good))
        data, _ = full.load_launchd_sample(sample)
        self.assertEqual(len(data), 64)
        for key, value in (('source_image_sha384', '0' * 96), ('sha256', '0' * 64), ('member', 'usr/lib/dyld'),
                           ('arm64e_slice', dict(offset=0, bytes=64, sha256='0' * 64))):
            record.write_text(json.dumps(good | {key: value}))
            with self.assertRaisesRegex(ValueError, 'verified 25F71'):
                full.load_launchd_sample(sample)

    def test_universal_launchd_returns_the_arm64e_image(self):
        x86 = b'\xcf\xfa\xed\xfe' + b'x' * 60
        arm = b'\xcf\xfa\xed\xfe' + b'a' * 124
        header = struct.pack('>II', 0xcafebabe, 2)
        header += struct.pack('>iIIII', 0x01000007, 3, 4096, len(x86), 12)
        header += struct.pack('>iIIII', 0x0100000c, 0x80000002, 8192, len(arm), 14)
        blob = header.ljust(4096, b'\0') + x86.ljust(4096, b'\0') + arm
        sample = self.dir / 'launchd'
        sample.write_bytes(blob)
        Path(str(sample) + '.json').write_text(json.dumps(self.provenance(sample, 8192, len(arm))))
        data, _ = full.load_launchd_sample(sample)
        self.assertEqual(data, arm)
        with self.assertRaisesRegex(ValueError, 'exactly one arm64e'):
            full.arm64e_slice(struct.pack('>II', 0xcafebabe, 1) + struct.pack('>iIIII', 0x01000007, 3, 28, 4, 2) + bytes(4))


if __name__ == '__main__':
    unittest.main()
