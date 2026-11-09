# SPDX-License-Identifier: GPL-3.0-or-later
"""ADSP and battery companion: upstream core tests, the real front ends against a simulated SoC, the contract, firmware pinning, the
device-tree node and the packager arguments. Model tests only: no SDK compile here, no hardware."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import native_adsp  # noqa: E402
from native_devtree import configure_native_text_state  # noqa: E402

ADSP = ROOT / 'platform/sheng/macos26/adsp'
UPSTREAM = ADSP / 'upstream'
FORWARDED = ('IOKit/IOLib.h', 'IOKit/IOService.h', 'IOKit/IOMemoryDescriptor.h', 'IOKit/IOBufferMemoryDescriptor.h', 'IOKit/IODeviceMemory.h',
             'IOKit/IODeviceTreeSupport.h', 'IOKit/IOWorkLoop.h', 'IOKit/IOTimerEventSource.h', 'IOKit/IOInterruptEventSource.h',
             'IOKit/IOPlatformExpert.h', 'IOKit/pwr_mgt/IOPM.h', 'IOKit/pwr_mgt/IOPMPowerSource.h', 'libkern/c++/OSData.h', 'libkern/c++/OSBoolean.h',
             'libkern/c++/OSDictionary.h', 'libkern/c++/OSNumber.h', 'libkern/c++/OSSymbol.h', 'kern/thread.h', 'kern/clock.h', 'pexpert/pexpert.h')


def node(name, properties=(), children=()):
    props = (('name', name.encode() + b'\0'), *properties)
    return (struct.pack('<II', len(props), len(children)) +
            b''.join(struct.pack('<32sI', key.encode(), len(value)) + value + bytes(-len(value) % 4) for key, value in props) +
            b''.join(children))


def read_tree(data):
    result = {}

    def read(at, parents):
        count, children = struct.unpack_from('<II', data, at)
        at += 8
        props = {}
        for _ in range(count):
            key, size = struct.unpack_from('<32sI', data, at)
            at += 36
            props[key.rstrip(b'\0').decode()] = data[at:at + size]
            at += (size + 3) & ~3
        path = (*parents, props['name'].rstrip(b'\0').decode())
        result[path] = props
        for _ in range(children):
            at = read(at, path)
        return at
    assert read(0, ()) == len(data)
    return result


def elf32(headers_only=False):
    """ELF32 with a header segment, a hash segment and one load segment, as (joined image, list of pieces)."""
    phdr = 52 + 3 * 32
    hash_data, load_data = bytes(range(16)) * 4, b'\x5a' * 256
    head = bytearray(phdr)
    head[:4] = b'\x7fELF'
    head[4:6] = bytes((1, 1))
    struct.pack_into('<I', head, 28, 52)
    struct.pack_into('<HHH', head, 40, 52, 32, 3)
    table = ((0, 0, 0, phdr, 0, 7 << 24), (0, 0x1000, 0x8a000000, len(hash_data), 0x1000, 2 << 24),
             (1, 0x2000, 0x8a100000, len(load_data), 0x200, (1 << 27) | 5))
    for i, (kind, off, addr, filesz, memsz, flags) in enumerate(table):
        struct.pack_into('<8I', head, 52 + 32 * i, kind, off, addr, addr, filesz, memsz, flags, 0x1000)
    joined = bytearray(0x2000 + len(load_data))
    joined[:phdr] = head
    joined[0x1000:0x1000 + len(hash_data)] = hash_data
    joined[0x2000:] = load_data
    return bytes(joined), [bytes(head), hash_data, load_data]


class NativeAdspTests(unittest.TestCase):
    def compile_and_run(self, name, sources, *arguments, language='c'):
        compiler = shutil.which('cc') or shutil.which('gcc') or shutil.which('clang')
        if not compiler:
            self.skipTest('requires a C compiler')
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / name
            sanitize = ['-fsanitize=address,undefined']
            for attempt in (sanitize, []):
                # Unmodified upstream files: these two GCC warnings are not in the compiler they were written for.
                build = subprocess.run([compiler, '-std=c11', '-Wall', '-Wextra', '-Werror', '-Wno-implicit-fallthrough', '-Wno-maybe-uninitialized',
                                        '-O1', '-g', *attempt, *map(str, sources), '-o', str(executable)],
                                       capture_output=True, text=True)
                if build.returncode == 0:
                    break
                if 'asan' not in build.stderr and 'cannot find' not in build.stderr:
                    break
            self.assertEqual(build.returncode, 0, build.stderr)
            run = subprocess.run([str(executable), *arguments], capture_output=True, text=True, timeout=60)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertTrue(run.stdout.startswith('PASS'), run.stdout)
            return run.stdout

    def test_upstream_adsp_cores(self):
        names = ('QcomMdt.c', 'QcomSmem.c', 'QcomSmp2p.c', 'QcomGlink.c', 'QcomQmp.c', 'QcomRpmh.c')
        self.compile_and_run('qcom-adsp-host', [UPSTREAM / 'tests/qcom_adsp_host.c', *[UPSTREAM / n for n in names]])

    def test_upstream_battery_manager(self):
        self.compile_and_run('battmgr-host', [UPSTREAM / 'tests/battmgr_host.c', UPSTREAM / 'ShengBattmgr.c'])

    def test_upstream_router(self):
        self.compile_and_run('qrtr-host', [UPSTREAM / 'tests/qrtr_host.c', UPSTREAM / 'QcomQrtr.c'])

    def host_binary(self, directory):
        compiler_cxx = shutil.which('clang++') or shutil.which('g++')
        compiler_c = shutil.which('clang') or shutil.which('gcc')
        if not compiler_cxx or not compiler_c:
            self.skipTest('requires C and C++ compilers')
        forward = directory / 'forward'
        for name in FORWARDED:
            target = forward / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('#include "iokit_stub.h"\n')
        includes = ['-I', str(forward), '-I', str(ROOT / 'tests/native_adsp_stubs'), '-I', str(ADSP), '-I', str(UPSTREAM)]
        objects = []
        for name in native_adsp.UPSTREAM_C:
            obj = directory / (Path(name).stem + '.o')
            subprocess.run([compiler_c, '-std=gnu17', '-g', '-O1', '-Wall', '-Wextra', '-Werror', '-Wno-unused-function', *includes,
                            '-c', str(UPSTREAM / name), '-o', str(obj)], check=True, capture_output=True, text=True)
            objects.append(str(obj))
        cxx = [compiler_cxx, '-std=gnu++20', '-g', '-O1', '-DNATIVE_ADSP_HOST', '-Wall', '-Wextra', '-Werror', '-Wno-unused-parameter', *includes]
        for source, stem in ((ROOT / 'tests/native_adsp_host_entry.cpp', 'entry'), (ADSP / 'native_battery.cpp', 'battery'), (ROOT / 'tests/native_adsp_host.cpp', 'host')):
            obj = directory / (stem + '.o')
            subprocess.run([*cxx, '-c', str(source), '-o', str(obj)], check=True, capture_output=True, text=True)
            objects.append(str(obj))
        executable = directory / 'native-adsp-host'
        subprocess.run([compiler_cxx, *objects, '-pthread', '-o', str(executable)], check=True, capture_output=True, text=True)
        return executable

    def test_real_front_ends_against_simulated_soc(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            try:
                executable = self.host_binary(directory)
            except subprocess.CalledProcessError as error:
                self.fail(error.stderr)
            for stage, battery, scenario in (('1', '0', 'normal'), ('2', '0', 'normal'), ('3', '0', 'normal'), ('3', '1', 'normal'), ('3', '2', 'normal'),
                                             ('3', '0', 'overlap'), ('3', '0', 'nofirmware'), ('3', '0', 'tampered'), ('3', '0', 'stopped')):
                with self.subTest(stage=stage, battery=battery, scenario=scenario):
                    run = subprocess.run([str(executable), stage, battery, scenario], capture_output=True, text=True, timeout=30)
                    self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
                    self.assertTrue(run.stdout.startswith('PASS'), run.stdout + run.stderr)

    def test_contract(self):
        record = native_adsp.evidence()
        native_adsp.validate(record)
        self.assertTrue(record['default_off'] and record['hardware_only'] and not record['firmware_compiled_in'])
        for change in (dict(default_off=False), dict(firmware_compiled_in=True), dict(read_only_stage=2), dict(charger_writes_default=True),
                       dict(critical_level_asserted=True),
                       dict(source_sha256=dict(record['source_sha256'], **{next(iter(record['source_sha256'])): '0' * 64}))):
            with self.assertRaises(ValueError):
                native_adsp.validate(record | change)
        self.assertEqual(native_adsp.POWER_SOURCE_ABI, dict(base_class='IOPMPowerSource', object_bytes=0x138, vtable_slots=273))

    def test_sources_are_copies_with_no_weak_imports(self):
        for name in native_adsp.FRONT_ENDS:
            text = (ADSP / name).read_text()
            self.assertNotIn('weak_import', text)
            self.assertIn('PE_parse_boot_arg', text if name == 'native_adsp.cpp' else (ADSP / 'native_adsp.cpp').read_text())
        front = (ADSP / 'native_adsp.cpp').read_text()
        self.assertIn('PE_vmm_present != 0', front)
        self.assertIn('mode < 1 || mode > 3', front)
        self.assertNotIn('sheng_fw_', front)
        battery = (ADSP / 'native_battery.cpp').read_text()
        self.assertIn('setAtCriticalLevel(false)', battery)
        self.assertIn('bm.sdp_limit_ua = 0', battery)
        self.assertIn('stageWanted >= 2', battery)
        self.assertIn('a68ee4ee67cc71792fbf519de7e3e28072c717b5', (UPSTREAM / 'UPSTREAM.txt').read_text())
        for name in (*native_adsp.UPSTREAM_C, *native_adsp.UPSTREAM_H):
            self.assertIn('BSD-2-Clause-Patent', (UPSTREAM / name).read_text().splitlines()[0])

    def test_join_split_image(self):
        joined, pieces = elf32()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            for i, piece in enumerate(pieces):
                (directory / f'adsp.b{i:02d}').write_bytes(piece)
            self.assertEqual(native_adsp.join_split_image(directory, 'adsp'), joined)
            (directory / 'adsp.b02').write_bytes(pieces[2][:10])
            with self.assertRaisesRegex(ValueError, 'shorter'):
                native_adsp.join_split_image(directory, 'adsp')

    def test_firmware_pin_and_node(self):
        adsp, dtb = b'A' * 8192, b'B' * 4096
        pin = native_adsp.pin_record(adsp, dtb)
        self.assertEqual(pin['adsp'], dict(bytes=8192, sha256=hashlib.sha256(adsp).hexdigest()))
        properties = native_adsp.node_properties(adsp, dtb, pin)
        self.assertEqual(properties['firmware'], adsp)
        self.assertEqual(properties['firmware-dtb'], dtb)
        self.assertEqual(properties['firmware-sha256'], hashlib.sha256(adsp).digest())
        self.assertTrue(all(len(key) <= 31 for key in properties))
        with self.assertRaisesRegex(ValueError, 'differ'):
            native_adsp.node_properties(adsp + b'x', dtb, pin)
        with self.assertRaisesRegex(ValueError, 'differ'):
            native_adsp.node_properties(adsp, dtb, pin | dict(schema='other'))

    def test_device_tree_node(self):
        adsp, dtb = b'A' * 8192, b'B' * 4096
        properties = native_adsp.node_properties(adsp, dtb, native_adsp.pin_record(adsp, dtb))
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'tree'
            path.write_bytes(node('device-tree', (), (node('chosen'), node('arm-io'))))
            configure_native_text_state(path, adsp_node=properties)
            tree = read_tree(path.read_bytes())
            added = tree[('device-tree', 'arm-io', 'native-adsp')]
            self.assertEqual(added['firmware'], adsp)
            self.assertEqual(added['firmware-dtb'], dtb)
            self.assertEqual(added['contract'], b'sheng-adsp-v1\0')
            path.write_bytes(node('device-tree', (), (node('chosen'), node('arm-io'))))
            with self.assertRaisesRegex(ValueError, 'ADSP node'):
                configure_native_text_state(path, adsp_node=dict(properties, firmware=b''))

    def test_packager_arguments(self):
        try:
            import package_macos26_ufs as packager
        except ImportError as error:
            self.skipTest(f'packager needs the UniXNU checkout: {error}')
        common = ['--kernel-build', 'k', '--qemu-observation', 'q', '--bootstrap-observation', 'b',
                  '--default-off-observation', 'd', '--firmware-build', 'f', '--backup-dir', 'x', '--output', 'o',
                  '--root-deployment', 'a', '--partition-plan', 'b', '--root-build', 'c', '--root-hash', 'd',
                  '--root-volume-uuid', '808eac5e-8275-478d-814c-2229d8eb8d07']

        def run(*arguments):
            stderr = io.StringIO()
            with mock.patch.object(sys, 'argv', ['package', *common, *arguments]), contextlib.redirect_stderr(stderr), \
                    mock.patch.object(packager, 'qualify', side_effect=AssertionError('qualification reached')):
                with self.assertRaises((SystemExit, AssertionError, OSError, ValueError)):
                    packager.main()
            return stderr.getvalue()

        self.assertIn('invalid choice', run('--adsp-stage', '4'))
        self.assertIn('--adsp-stage 3 needs', run('--adsp-stage', '3'))
        self.assertIn('--battery-stage requires', run('--battery-stage', '1', '--adsp-stage', '1'))
        self.assertIn('invalid choice', run('--battery-stage', '3'))
        self.assertIn('go together', run('--adsp-stage', '1', '--adsp-firmware-dir', '/tmp'))
        self.assertIn('--adsp-firmware-dir requires', run('--adsp-firmware-dir', '/tmp', '--adsp-firmware-pin', '/tmp/x'))
        self.assertNotIn('invalid choice', run('--adsp-stage', '1'))
        self.assertNotIn('needs', run('--adsp-stage', '2'))

    def test_storage_build_refuses_the_vm_transport(self):
        import build_macos26_storage
        with self.assertRaisesRegex(ValueError, 'UFS'):
            build_macos26_storage.build(ROOT, Path('/nonexistent'), {}, Path('/nonexistent'), None, {}, {}, transport='virtio', adsp=True)

    def test_workflow_builds_and_tests_it(self):
        workflow = (ROOT / '.github/workflows/hw-sheng-cn-1tb.yml').read_text()
        self.assertIn('--build-adsp', workflow)
        self.assertIn('test_native_adsp', workflow)


if __name__ == '__main__':
    unittest.main()
