# SPDX-License-Identifier: GPL-3.0-or-later
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import build_macos26_storage as storage  # noqa: E402
import inspect_macos26_full_image as inspector  # noqa: E402
import install_macos26_full_system as installer  # noqa: E402
import prepare_macos26_full_root as prepare  # noqa: E402

spec = importlib.util.spec_from_file_location('image_fixture', ROOT / 'tests/test_full_system_qemu.py')
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
MIB = 1 << 20


class FakeTransport:
    """In-memory partition; discard optionally leaves stale bytes behind."""

    def __init__(self, size, *, discard_zeroes=True, corrupt_at=None):
        self.partition = bytearray(b'\xee' * size)
        self.discard_zeroes, self.corrupt_at = discard_zeroes, corrupt_at
        self.staged, self.writes, self.removed = {}, [], []

    def discard(self):
        if self.discard_zeroes:
            self.partition[:] = bytes(len(self.partition))
        return 'fake-blkdiscard'

    def stage(self, data, name):
        remote = '/tmp/sheng-macossystem-test/' + name
        self.staged[remote] = bytes(data)
        return remote

    def write(self, remote, offset, length):
        data = bytearray(self.staged[remote][:length])
        if self.corrupt_at == offset:
            data[0] ^= 1
        self.writes.append((offset, length))
        self.partition[offset:offset + length] = data

    def digest(self, offset, length):
        return hashlib.sha256(self.partition[offset:offset + length]).hexdigest()

    def remove(self, remote):
        self.removed.append(remote)


class FullRootPackageTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.dir = Path(folder.name)
        # Shrink the chunk so the small synthetic disk spans zero and non-zero chunks.
        for name, value in (('CHUNK', 8192), ('ZERO', bytes(8192)),
                            ('ZERO_CHUNK_SHA256', hashlib.sha256(bytes(8192)).hexdigest())):
            patcher = mock.patch.object(prepare, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.image = self.dir / 'disk.img'
        self.image.write_bytes(fixture.variant())
        self.report = self.dir / 'disk.img.json'
        self.report.write_text(json.dumps(inspector.inspect(self.image)))

    def test_package_describes_the_image_without_copying_it(self):
        record = prepare.prepare(self.image, self.report, self.dir / 'package')
        data = self.image.read_bytes()
        header = (self.dir / 'package/manifest.bin').read_bytes()
        self.assertEqual(header[:8], b'SHNGROOT')
        self.assertEqual(hashlib.sha256(header[:4064]).digest(), header[4064:])
        self.assertEqual(record['window'], dict(name='OS', offset=MIB, bytes=len(data),
                                                sha256=hashlib.sha256(data).hexdigest()))
        chunks = json.loads((self.dir / 'package/chunks.json').read_text())['chunks']
        self.assertEqual(b''.join(data[c['offset']:c['offset'] + c['bytes']] for c in chunks), data)
        self.assertTrue(any(c['zero'] for c in chunks) and not all(c['zero'] for c in chunks))
        contract = json.loads((self.dir / 'package/macos26-full-root.json').read_text())
        self.assertEqual(contract['volume_uuid'], str(fixture.fixture.SYSTEM).upper())
        self.assertEqual(contract['partition'], 'macossystem')
        self.assertEqual(sorted(p.name for p in (self.dir / 'package').iterdir()),
                         ['build.json', 'chunks.json', 'macos26-full-root.json', 'manifest.bin'])

    def test_contract_is_accepted_by_the_storage_build(self):
        prepare.prepare(self.image, self.report, self.dir / 'package')
        (self.dir / 'config').mkdir()
        shutil.copy(self.dir / 'package/macos26-full-root.json', self.dir / 'config')
        contract = storage.full_root_contract(self.dir)
        self.assertEqual(contract['overlay']['boot_argument'], 'native-ufs-root-overlay-mib')
        broken = dict(contract, partition='macosroot')
        (self.dir / storage.FULL_ROOT_CONFIG).write_text(json.dumps(broken))
        with self.assertRaisesRegex(ValueError, 'inconsistent'):
            storage.full_root_contract(self.dir)
        (self.dir / storage.FULL_ROOT_CONFIG).unlink()
        with self.assertRaisesRegex(ValueError, 'missing'):
            storage.full_root_contract(self.dir)

    def install(self, transport):
        prepare.prepare(self.image, self.report, self.dir / 'package')
        build = json.loads((self.dir / 'package/build.json').read_text())
        chunk_map = json.loads((self.dir / 'package/chunks.json').read_text())
        header = (self.dir / 'package/manifest.bin').read_bytes()
        installer.verify_image(self.image, chunk_map)
        journal = io.StringIO()
        counts = installer.write_partition(transport, self.image, chunk_map, header, build['window']['sha256'],
                                           journal)
        return counts, header, [json.loads(line) for line in journal.getvalue().splitlines()]

    def test_install_writes_data_chunks_and_header_last(self):
        size = MIB + len(self.image.read_bytes())
        transport = FakeTransport(size)
        counts, header, journal = self.install(transport)
        self.assertEqual(bytes(transport.partition[:4096]), header)
        self.assertEqual(bytes(transport.partition[MIB:]), self.image.read_bytes())
        self.assertEqual(transport.writes[-1], (0, 4096))
        self.assertEqual(counts['zero_written'], 0)
        self.assertGreater(counts['zero_verified'], 0)
        self.assertEqual(journal[-1], dict(operation='verified', kind='window', bytes=size - MIB))

    def test_install_zeroes_stale_chunks_when_discard_keeps_data(self):
        transport = FakeTransport(MIB + len(self.image.read_bytes()), discard_zeroes=False)
        counts, _, _ = self.install(transport)
        self.assertEqual(counts['zero_verified'], 0)
        self.assertGreater(counts['zero_written'], 0)
        self.assertEqual(bytes(transport.partition[MIB:]), self.image.read_bytes())

    def test_readback_mismatch_stops_before_the_header(self):
        transport = FakeTransport(MIB + len(self.image.read_bytes()), corrupt_at=MIB)
        with self.assertRaisesRegex(ValueError, 'stopped without retry'):
            self.install(transport)
        self.assertEqual(transport.writes, [(MIB, 8192)])
        self.assertNotEqual(bytes(transport.partition[:8]), b'SHNGROOT')

    def prepared(self):
        prepare.prepare(self.image, self.report, self.dir / 'package')
        build = json.loads((self.dir / 'package/build.json').read_text())
        chunk_map = json.loads((self.dir / 'package/chunks.json').read_text())
        header = (self.dir / 'package/manifest.bin').read_bytes()
        return build, header, chunk_map, chunk_map['window_offset'] + sum(c['bytes'] for c in chunk_map['chunks'])

    def test_resume_continues_after_the_verified_prefix(self):
        build, header, chunk_map, window = self.prepared()
        transport = FakeTransport(window)
        with io.StringIO() as journal:
            installer.write_partition(transport, self.image, chunk_map, header, build['window']['sha256'], journal)
        complete = bytes(transport.partition)
        resumed = FakeTransport(window, discard_zeroes=False)
        first = len(chunk_map['chunks']) // 2
        stop = chunk_map['window_offset'] + chunk_map['chunks'][first]['offset']
        resumed.partition[:stop] = complete[:stop]
        resumed.partition[:4096] = bytes(4096)
        with io.StringIO() as journal:
            installer.write_partition(resumed, self.image, chunk_map, header, build['window']['sha256'], journal, resume_from=first)
            text = journal.getvalue()
        self.assertEqual(bytes(resumed.partition), complete)
        self.assertIn('"resume"', text)
        self.assertNotIn('"discard"', text)
        self.assertTrue(all(offset >= stop or offset == 0 for offset, _ in resumed.writes))

    def test_resume_refuses_a_bad_prefix_and_a_corrupt_earlier_chunk(self):
        path = self.dir / 'journal.jsonl'
        path.write_text('{"operation": "verified", "chunk": 0}\n{"operation": "verified", "chunk": 2}\n')
        with self.assertRaisesRegex(ValueError, 'contiguous'):
            installer.verified_prefix(path)
        path.write_text('{"operation": "verified", "chunk": 0}\n{"operation": "mismatch", "chunk": 1}\n')
        with self.assertRaisesRegex(ValueError, 'mismatch'):
            installer.verified_prefix(path)
        path.write_text('{"operation": "discard"}\n{"operation": "zero-verified", "chunk": 0}\n{"operation": "starting", "chunk": 1}\n{"operation": "verified", "chunk": 1}\n')
        self.assertEqual(installer.verified_prefix(path), 2)
        build, header, chunk_map, window = self.prepared()
        transport = FakeTransport(window)
        with io.StringIO() as journal, self.assertRaisesRegex(ValueError, 'whole-window'):
            installer.write_partition(transport, self.image, chunk_map, header, build['window']['sha256'], journal, resume_from=1)

    def transport_with_mock_device(self):
        device = mock.Mock(adb=['adb'])
        device.shell.side_effect = ['tmpfs /tmp tmpfs rw 0 0', '']
        return installer.AdbTransport(device, '/tmp/sheng-macossystem-test')

    def test_only_a_local_daemon_failure_is_repeated(self):
        transport = self.transport_with_mock_device()
        answers = [mock.Mock(returncode=1, stdout='', stderr='adb.exe: cannot connect to daemon at tcp:5037: timed out'),
                   mock.Mock(returncode=0, stdout='abc  -', stderr='')]
        with mock.patch.object(installer.subprocess, 'run', side_effect=answers) as run, mock.patch.object(installer.time, 'sleep'):
            self.assertEqual(transport.digest(0, 4096), 'abc')
        self.assertEqual((run.call_count, transport.daemon_retries), (2, 1))
        transport = self.transport_with_mock_device()
        failed = mock.Mock(returncode=1, stdout='dd: write error', stderr='')
        with mock.patch.object(installer.subprocess, 'run', return_value=failed) as run, self.assertRaisesRegex(ValueError, 'write error'):
            transport.write('/tmp/x', 4096, 4096)
        self.assertEqual(run.call_count, 1)

    def test_a_resumed_journal_counts_from_its_first_chunk(self):
        path = self.dir / 'journal.jsonl'
        path.write_text('{"operation": "resume", "first_chunk": 5}\n{"operation": "verified", "chunk": 5}\n{"operation": "verified", "chunk": 6}\n')
        self.assertEqual(installer.verified_prefix(path, 5), 7)

    def test_changed_image_is_refused_before_discard(self):
        prepare.prepare(self.image, self.report, self.dir / 'package')
        chunk_map = json.loads((self.dir / 'package/chunks.json').read_text())
        data = bytearray(self.image.read_bytes())
        data[-1] ^= 1
        self.image.write_bytes(bytes(data))
        with self.assertRaisesRegex(ValueError, 'differs from the chunk map'):
            installer.verify_image(self.image, chunk_map)

    def test_staging_path_is_bounded(self):
        for bad in ('/tmp/other', '/tmp/sheng-macossystem-../x', '/data/sheng-macossystem-a'):
            with self.assertRaisesRegex(ValueError, 'staging'):
                installer.AdbTransport(mock.Mock(), bad)


if __name__ == '__main__':
    unittest.main()
