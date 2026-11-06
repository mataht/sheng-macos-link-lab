# SPDX-License-Identifier: GPL-3.0-or-later
"""Exercise production FAT ownership checks and interrupted snapshot commits."""
import ctypes
import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
from native_log_file import (BLOCK, CONTRACT, FILE_BYTES, LAYOUT, LAYOUTS, SLOT_BYTES, SNAPSHOT_BYTES, decode,
                             geometry_defines, guard, layout_of, locate)
from prepare_native_log_file import prepare


def fixture_backup_entries(layout):
    # Upstream keeps its historical array; the LUN0 fixture uses the ptool convention (disk last - 4).
    return 521211 if layout['name'] == 'sheng-lun4-upstream' else layout['disk_blocks'] - 5


def compile_defines(layout):
    return ['-D'+name+'='+value for name, value in geometry_defines(layout).items()] + [
        f'-DTEST_BACKUP_ENTRIES={fixture_backup_entries(layout)}ULL']


class NativeLogFile(unittest.TestCase):
    layout = LAYOUT

    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.TemporaryDirectory()
        cls.path = Path(cls.work.name)
        compiler = shutil.which('g++')
        if not compiler or not all(shutil.which(tool) for tool in ('mkfs.fat', 'mcopy', 'mdir')):
            raise unittest.SkipTest('requires a host C++ compiler, dosfstools and mtools')
        library = cls.path/'bridge.so'
        subprocess.run([compiler, '-std=c++23', '-Wall', '-Wextra', '-Werror', '-shared', '-fPIC', *compile_defines(cls.layout),
                        str(ROOT/'tests/macoslog_bridge.cpp'), '-o', str(library)], check=True)
        cls.lib = ctypes.CDLL(str(library))
        cls.lib.check_fat.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint64)]
        cls.lib.check_gpt.argtypes = [ctypes.c_void_p]*4 + [ctypes.c_bool]
        cls.lib.check_guard.argtypes = [ctypes.c_void_p, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_void_p]
        cls.lib.commit.argtypes = [ctypes.c_void_p, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_void_p,
                                   ctypes.c_uint, ctypes.c_void_p]
        for name in ('check_fat', 'check_gpt', 'check_guard', 'commit'):
            getattr(cls.lib, name).restype = ctypes.c_bool
        cls.fid = uuid.UUID(CONTRACT['file_uuid']).bytes
        cls.pid = uuid.UUID(CONTRACT['partition_uuid']).bytes_le
        cls.tid = uuid.UUID(CONTRACT['partition_type']).bytes_le
        worker = cls.path/'worker.so'
        defines = ['-DNATIVE_MACOSLOG_'+key+'_ID={'+','.join(map(str, value))+'}'
                   for key, value in (('FILE', cls.fid), ('PARTITION', cls.pid), ('TYPE', cls.tid))]
        subprocess.run([compiler, '-std=c++23', '-Wall', '-Wextra', '-Werror', '-Wno-attributes',
                        '-Wno-address', '-shared', '-fPIC', *defines, *compile_defines(cls.layout),
                        str(ROOT/'tests/macoslog_worker_bridge.cpp'), '-o', str(worker)], check=True)
        cls.worker = ctypes.CDLL(str(worker))
        cls.worker.run_worker.argtypes = [ctypes.c_void_p]*5 + [ctypes.c_uint]*2 + [ctypes.c_void_p]
        image = cls.path/'original.img'
        with image.open('wb') as stream:
            stream.truncate(64 << 20)
        subprocess.run(['mkfs.fat', '-F', '16', '-S', '4096', '-s', '1', '-n', 'MACOSLOG', str(image)],
                       check=True, capture_output=True)
        previous = cls.path/'OBSERVED.JSON'
        previous.write_text('{"preserve":true}\n')
        filler = cls.path/'hole.bin'
        filler.write_bytes(bytes(BLOCK))
        subprocess.run(['mcopy', '-i', str(image), str(filler), '::/HOLE.BIN'], check=True)
        subprocess.run(['mcopy', '-i', str(image), str(previous), '::/OBSERVED.JSON'], check=True)
        subprocess.run(['mdel', '-i', str(image), '::/HOLE.BIN'], check=True)
        cls.record = prepare(image, cls.path/'prepared')
        cls.image = (cls.path/'prepared/macoslog.img').read_bytes()
        cls.offset = locate(cls.image)
        cls.first = cls.layout['partition_first'] + cls.offset//BLOCK
        # The guard block names the file's absolute LBA; rebuild it for the layout under test.
        image = bytearray(cls.image)
        image[cls.offset+FILE_BYTES-BLOCK:cls.offset+FILE_BYTES] = guard(cls.first)
        cls.image = bytes(image)

    @classmethod
    def tearDownClass(cls):
        cls.work.cleanup()

    def test_real_fat_file_and_existing_files_preserved(self):
        first = ctypes.c_uint64()
        self.assertTrue(self.lib.check_fat(self.image, ctypes.byref(first)))
        self.assertEqual(first.value, self.first)
        marker = self.image[self.offset+FILE_BYTES-BLOCK:self.offset+FILE_BYTES]
        self.assertTrue(self.lib.check_guard(marker, first.value, self.fid, self.pid))
        self.assertFalse(self.lib.check_guard(marker, first.value+1, self.fid, self.pid))
        text = subprocess.check_output(['mtype', '-i', str(self.path/'prepared/macoslog.img'), '::/OBSERVED.JSON'])
        self.assertEqual(text, b'{"preserve":true}\n')
        with self.assertRaisesRegex(ValueError, 'already exists'):
            prepare(self.path/'prepared/macoslog.img', self.path/'refused')
        self.assertFalse((self.path/'refused').exists())

    def test_real_command_write_window(self):
        executable = self.path/'commands'
        subprocess.run([shutil.which('g++'), '-std=c++23', '-Wall', '-Wextra', '-Werror',
            '-Wno-unused-function', '-fsanitize=address,undefined', *compile_defines(self.layout),
            str(ROOT/'tests/macoslog_commands.cpp'),
            '-o', str(executable)], check=True)
        subprocess.run([str(executable)], check=True, timeout=15)

    def test_fat_mirror_fragmentation_crosslink_and_extent_refusal(self):
        reserved, = struct.unpack_from('<H', self.image, 14)
        fats, = struct.unpack_from('<H', self.image, 22)
        entries, = struct.unpack_from('<H', self.image, 17)
        root = (reserved+2*fats)*BLOCK
        target = next(at for at in range(root, root+entries*32, 32) if self.image[at:at+11] == b'SHENGLOGBIN')
        cluster, = struct.unpack_from('<H', self.image, target+26)
        for mode in ('mirror', 'fragment', 'crosslink', 'attribute', 'size', 'geometry'):
            raw = bytearray(self.image)
            if mode == 'mirror':
                raw[reserved*BLOCK+cluster*2] ^= 1
            elif mode in ('fragment', 'crosslink'):
                at, value = (cluster, cluster+3) if mode == 'fragment' else (cluster+512, cluster)
                for copy in range(2):
                    struct.pack_into('<H', raw, (reserved+copy*fats)*BLOCK+at*2, value)
            elif mode == 'attribute':
                raw[target+11] |= 1
            elif mode == 'size':
                struct.pack_into('<I', raw, target+28, FILE_BYTES+1)
            else:
                struct.pack_into('<H', raw, 11, 512)
            with self.subTest(mode=mode):
                self.assertFalse(self.lib.check_fat(bytes(raw), ctypes.byref(ctypes.c_uint64())))
                with self.assertRaises(ValueError):
                    locate(bytes(raw))

    def gpt_fixture(self, array=None, backup=False, backup_entries=None):
        layout = self.layout
        disk_last, count = layout['disk_blocks'] - 1, layout['entry_count']
        if array is None:
            array = bytearray(count*128)
            at = (layout['partition_index'] - 1)*128
            array[at:at+16], array[at+16:at+32] = self.tid, self.pid
            struct.pack_into('<QQ', array, at+32, layout['partition_first'], layout['partition_last'])
            array[at+56:at+72] = 'macoslog'.encode('utf-16-le')
        entries_lba = (fixture_backup_entries(layout) if backup_entries is None else backup_entries) if backup else 2
        raw = bytearray(4096)
        struct.pack_into('<8s4I4Q16sQ3I', raw, 0, b'EFI PART', 0x10000, 92, 0, 0,
            disk_last if backup else 1, 1 if backup else disk_last, 6, disk_last - 5, bytes(16),
            entries_lba, count, 128, zlib.crc32(array))
        struct.pack_into('<I', raw, 16, zlib.crc32(raw[:92]))
        return bytes(raw), array

    def test_gpt_identity_crc_and_overlap(self):
        _, entries = self.gpt_fixture()
        at = (self.layout['partition_index'] - 1)*128

        def header(array, backup=False):
            return self.gpt_fixture(array, backup)[0]

        for backup in (False, True):
            self.assertTrue(self.lib.check_gpt(header(entries, backup), bytes(entries), self.pid, self.tid, backup))
        for mode in ('crc', 'guid', 'overlap', 'index'):
            bad = bytearray(entries)
            if mode == 'crc':
                h = header(bad)
                bad[at+32] ^= 1
            elif mode == 'guid':
                bad[at+16] ^= 1
                h = header(bad)
            elif mode == 'index':
                bad[:128], bad[at:at+128] = bad[at:at+128], bytes(128)
                h = header(bad)
            else:
                bad[:16] = self.tid
                first = self.layout['partition_first']
                struct.pack_into('<QQ', bad, 32, first - 1, first)
                h = header(bad)
            self.assertFalse(self.lib.check_gpt(h, bytes(bad), self.pid, self.tid, False), mode)
        # The backup array must sit after the last usable block and before the backup header.
        disk_last = self.layout['disk_blocks'] - 1
        blocks = self.layout['entry_count']*128//BLOCK
        for where in (disk_last - 5, disk_last - blocks + 1, 2):
            h = self.gpt_fixture(entries, True, where)[0]
            self.assertFalse(self.lib.check_gpt(h, bytes(entries), self.pid, self.tid, True), where)
        accepted = self.lib.check_gpt(self.gpt_fixture(entries, True, disk_last - blocks)[0], bytes(entries),
                                      self.pid, self.tid, True)
        # Upstream keeps the exact historical array (disk last - 4); the reviewed LUN0 layout accepts any in-bound one.
        self.assertEqual(accepted, self.layout['name'] != 'sheng-lun4-upstream' or blocks == 4)

    def test_commit_order_and_recovery_from_each_interruption(self):
        identity = '12345678-1234-5678-9012-345678901234'
        payload = bytearray(21*BLOCK)
        struct.pack_into('<8s4I2Q2I8Q16s', payload, 0, b'SHNLOG01', 1, 128, 16384, 65536,
                         6, 0, 0, 0, *([0]*8), uuid.UUID(identity).bytes)
        payload[128:134] = b'first\n'
        initial = bytearray(FILE_BYTES)
        initial[-BLOCK:] = guard(self.record['file_first_lba'])

        def commit(data, generation, fail=0):
            events = (ctypes.c_uint*4)()
            buf = (ctypes.c_ubyte*FILE_BYTES).from_buffer(data)
            contents = bytes(payload)
            ok = self.lib.commit(buf, generation, contents, self.fid, fail, events)
            return ok, list(events)

        ok, events = commit(initial, 1)
        self.assertTrue(ok)
        self.assertEqual(events, [0x10000+257*32+21, 0x20000, 0x10000+256*32+1, 0x20000])
        self.assertEqual(decode(bytes(initial), identity)[1]['generation'], 1)
        payload[128:134] = b'next!\n'
        for fail in range(1, 5):
            interrupted = bytearray(initial)
            self.assertFalse(commit(interrupted, 2, fail)[0])
            raw, report = decode(bytes(interrupted), identity)
            self.assertEqual(report['generation'], 2 if fail == 4 else 1)
            self.assertEqual(interrupted[-BLOCK:], initial[-BLOCK:])
            self.assertFalse(report['latest_panic_guaranteed'])
        complete = bytearray(initial)
        self.assertTrue(commit(complete, 2)[0])
        self.assertEqual(decode(bytes(complete), identity)[1]['console_text'], 'next!\n')
        complete[BLOCK+128] ^= 1
        self.assertEqual(decode(bytes(complete), identity)[1]['generation'], 1)
        with self.assertRaises(ValueError):
            decode(bytes(initial), str(uuid.uuid4()))
        initial[-1] ^= 1
        with self.assertRaisesRegex(ValueError, 'guard'):
            decode(bytes(initial), identity)

    def test_worker_default_off_readonly_commit_restart_and_error_stop(self):
        _, array = self.gpt_fixture()
        headers = [self.gpt_fixture(array, backup)[0] for backup in (False, True)]
        identity = '12345678-1234-5678-9012-345678901234'
        payload = struct.pack('<8s4I2Q2I8Q16s', b'SHNLOG01', 1, 128, 16384, 65536,
                              5, 0, 0, 0, *([0]*8), uuid.UUID(identity).bytes) + b'boot\n' + bytes(SNAPSHOT_BYTES-133)

        def run(volume, mode, fail=0):
            result = (ctypes.c_uint64*7)()
            self.worker.run_worker((ctypes.c_ubyte*len(volume)).from_buffer(volume), *headers,
                                   bytes(array), payload, mode, fail, result)
            return list(result)

        volume = bytearray(self.image)
        self.assertEqual(run(volume, 0), [0]*7)
        self.assertEqual(run(volume, 1), [2, 0, 3, 0, 0, 0, 0])
        self.assertEqual(volume, self.image)
        self.assertEqual(run(volume, 2), [3, 0, 4, 1, 1, 2, 2])
        capture = bytes(volume[self.offset:self.offset+FILE_BYTES])
        self.assertEqual(decode(capture, identity, self.layout)[1]['console_text'], 'boot\n')
        # All partition bytes outside the owned snapshot file remain unchanged.
        self.assertEqual(volume[:self.offset], self.image[:self.offset])
        self.assertEqual(volume[self.offset+FILE_BYTES:], self.image[self.offset+FILE_BYTES:])
        self.assertEqual(volume[self.offset+FILE_BYTES-BLOCK:self.offset+FILE_BYTES],
                         self.image[self.offset+FILE_BYTES-BLOCK:self.offset+FILE_BYTES])
        self.assertEqual(run(volume, 2)[3], 2)
        self.assertEqual(run(volume, 2, 1), [5, 1, 4, 0, 0, 1, 0])
        self.assertEqual(decode(bytes(volume[self.offset:self.offset+FILE_BYTES]), identity, self.layout)[1]['generation'], 2)
        volume[self.offset+FILE_BYTES-1] ^= 1
        self.assertEqual(run(volume, 2), [5, 0, 3, 0, 0, 0, 0])

    def test_package_requires_both_modes_current_sources_and_matching_readback(self):
        from package_macos26_ufs import qualify_macoslog
        base = self.path/'qualification'
        build, readonly, write = (base/name for name in ('build', 'readonly', 'write'))
        for path in (build, readonly, write):
            path.mkdir(parents=True)
        identity = dict(kernel_sha256='kernel', uuid='12345678-1234-5678-9012-345678901234')
        first = self.record['file_first_lba']
        metadata = dict(contract=CONTRACT, default_off=True, filesystem_metadata_writes=False,
            writes_confined_to_preallocated_file=True,
            source_sha256={name: hashlib.sha256((ROOT/'platform/sheng/macos26/storage'/name).read_bytes()).hexdigest()
                           for name in ('macoslog_layout.h', 'macoslog_worker.h')})
        (build/'security-relink.json').write_text(json.dumps(dict(storage_driver=dict(build=dict(macoslog=metadata)))))
        deployed = dict(status='verified', partition=LAYOUT['device'], file_first_lba=first,
            gpt_writes=False, gpt_unchanged=True, existing_allocations_preserved=True,
            image_sha256='seed', readback_sha256='seed')
        deployment = base/'deployment.json'
        deployment.write_text(json.dumps(deployed))
        payload = bytearray(21*BLOCK)
        struct.pack_into('<8s4I2Q2I8Q16s', payload, 0, b'SHNLOG01', 1, 128, 16384, 65536,
                         0, 0, 0, 0, *([0]*8), uuid.UUID(identity['uuid']).bytes)
        file = bytearray(FILE_BYTES)
        file[-BLOCK:] = guard(first)
        for generation in (1, 2):
            self.assertTrue(self.lib.commit((ctypes.c_ubyte*FILE_BYTES).from_buffer(file), generation,
                                            bytes(payload), self.fid, 0, (ctypes.c_uint*4)()))
        (write/'macoslog.bin').write_bytes(file)
        for folder, state, count in ((readonly, 2, 0), (write, 3, 2)):
            backend = dict(identity, schema='sheng-macoslog-qemu-v1', transport_mocked=True,
                production_worker_used=True, outside_file_unchanged=True, guard_unchanged=True,
                writes_confined_to_owned_file=True, image_before_sha256='seed', file_first_lba=first,
                write_calls=count*2, sync_calls=count*2)
            values = dict(_native_macoslog_state=hex(state), _native_macoslog_error='0x0',
                _native_macoslog_first_lba=hex(first), _native_macoslog_snapshots=hex(count),
                _native_macoslog_generation=hex(count))
            (folder/'result.json').write_text(json.dumps(dict(identity, observed_globals=values)))
            (folder/'macoslog-backend.json').write_text(json.dumps(backend))
        result = qualify_macoslog(build, write, readonly, deployment, identity)
        self.assertEqual(result['generation'], 2)
        self.assertFalse(result['hardware_write_qualified'])
        stale = dict(identity, kernel_sha256='other')
        with self.assertRaisesRegex(ValueError, 'exact-candidate'):
            qualify_macoslog(build, write, readonly, deployment, stale)
        file[-1] ^= 1
        (write/'macoslog.bin').write_bytes(file)
        with self.assertRaisesRegex(ValueError, 'guard'):
            qualify_macoslog(build, write, readonly, deployment, identity)
        observed = json.loads((readonly/'macoslog-backend.json').read_text())
        observed['write_calls'] = 1
        (readonly/'macoslog-backend.json').write_text(json.dumps(observed))
        with self.assertRaisesRegex(ValueError, 'readonly'):
            qualify_macoslog(build, write, readonly, deployment, identity)

    def test_installer_stale_source_single_transfer_and_uncertain_outcome(self):
        import install_native_log_file as installer

        class Device:
            adb = ['adb', '-s', 'fixture']

        original = (self.path/'original.img').read_bytes()
        for state in ('success', 'stale', 'timeout', 'readback'):
            output = self.path/('install-'+state)
            current, attempts = original, []

            def transfer(command, *, stdin, **kwargs):
                nonlocal current
                attempts.append(command)
                self.assertIn('of='+LAYOUT['device'], command[-1])
                self.assertIn('count=67108864 iflag=count_bytes', command[-1])
                current = stdin.read()
                if state == 'timeout':
                    raise subprocess.TimeoutExpired(command, 180)
                if state == 'readback':
                    current = current[:-1]+bytes([current[-1]^1])
                return subprocess.CompletedProcess(command, 0, b'', b'')

            if state == 'stale':
                current = original[:-1]+bytes([original[-1]^1])
            with patch.object(installer, 'identity', return_value={'primary': b'unchanged'}), \
                    patch.object(installer, 'read_image', side_effect=lambda device: current), \
                    patch.object(installer.subprocess, 'run', side_effect=transfer):
                if state == 'success':
                    result = installer.install(Device(), self.path/'prepared', output)
                    self.assertEqual(result['status'], 'verified')
                    self.assertFalse(result['kernel_log_write_qualified'])
                else:
                    with self.assertRaises((ValueError, subprocess.TimeoutExpired)):
                        installer.install(Device(), self.path/'prepared', output)
            self.assertEqual(len(attempts), 0 if state == 'stale' else 1)
            if state == 'timeout':
                self.assertEqual(json.loads((output/'deployment.json').read_text())['status'], 'write-outcome-unknown')


class NativeLogFileUpstreamLayout(NativeLogFile):
    """The same production kernel checks compiled for the upstream LUN4 layout must behave exactly as before."""
    layout = layout_of(dict(LAYOUTS['sheng-lun4-upstream'], file_uuid=CONTRACT['file_uuid']))

    def test_package_requires_both_modes_current_sources_and_matching_readback(self):
        self.skipTest('packaging qualifies the active layout only')

    def test_installer_stale_source_single_transfer_and_uncertain_outcome(self):
        self.skipTest('the installer targets the active layout only')


if __name__ == '__main__':
    unittest.main()
