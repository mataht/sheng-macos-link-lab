# SPDX-License-Identifier: GPL-3.0-or-later
"""Touch companion: pointer mapping, upstream library host compile, contract, device-tree node, packager arguments."""
import contextlib
import io
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
import native_touch  # noqa: E402
from native_devtree import configure_native_text_state  # noqa: E402


def node(name, properties=(), children=()):
    props = (('name', name.encode() + b'\0'), *properties)
    return (struct.pack('<II', len(props), len(children)) +
            b''.join(struct.pack('<32sI', key.encode(), len(value)) + value + bytes(-len(value) % 4)
                     for key, value in props) + b''.join(children))


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


class NativeTouchTests(unittest.TestCase):
    def compiler(self, name):
        found = shutil.which(name)
        if not found:
            self.skipTest('requires ' + name)
        return found

    def test_pointer_mapping(self):
        compiler = shutil.which('g++') or self.compiler('clang++')
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / 'pointer'
            build = subprocess.run([compiler, '-std=c++17', '-Wall', '-Wextra', '-Werror', '-fsanitize=address,undefined',
                                    str(ROOT / 'tests/touch_pointer.cpp'), '-o', str(executable)],
                                   capture_output=True, text=True, timeout=60)
            self.assertEqual(build.returncode, 0, build.stderr)
            run = subprocess.run([str(executable)], capture_output=True, text=True, timeout=30)
            self.assertEqual(run.returncode, 0, run.stderr)

    def test_upstream_libraries_compile_as_c(self):
        compiler = shutil.which('gcc') or self.compiler('clang')
        includes = sorted({str((ROOT / name).parent) for name in native_touch.UPSTREAM_HEADERS})
        with tempfile.TemporaryDirectory() as temporary:
            for source in native_touch.UPSTREAM_C:
                build = subprocess.run([compiler, '-std=gnu17', '-c', str(ROOT / source), '-o',
                                        str(Path(temporary) / (Path(source).stem + '.o')),
                                        *[part for include in includes for part in ('-I', include)]],
                                       capture_output=True, text=True, timeout=60)
                self.assertEqual(build.returncode, 0, source + ': ' + build.stderr)

    def test_contract_evidence_and_firmware_pin(self):
        record = native_touch.evidence()
        native_touch.validate(record)
        self.assertIs(record['firmware_compiled_in'], False)
        self.assertIn(native_touch.PATCH, record['source_sha256'])
        for change in (dict(default_off=False), dict(firmware_compiled_in=True),
                       dict(source_sha256=dict(record['source_sha256'], **{native_touch.UPSTREAM_CPP: '0' * 64}))):
            with self.assertRaisesRegex(ValueError, 'default-off contract'):
                native_touch.validate(record | change)
        for firmware in (b'', bytes(native_touch.FIRMWARE_SIZE)):
            with self.assertRaisesRegex(ValueError, 'captured device firmware'):
                native_touch.node_properties(firmware)

    def test_touch_probe_has_no_weak_kernel_imports(self):
        # The kernel collection linker (native_storage_module) accepts only non-weak flat imports; CI 42445927283 was
        # refused locally for weak armcpu_spi_attach and native_hid_input_command. The only weak declaration left is
        # the firmware hook, which native_touch.cpp defines inside the same module.
        source = (ROOT / native_touch.UPSTREAM_CPP)
        if not source.exists():
            self.skipTest('upstream checkout not present')
        weak = [line.strip() for line in source.read_text().splitlines()
                if 'weak_import' in line and not line.lstrip().startswith('//')]
        self.assertEqual(len(weak), 1, weak)
        self.assertIn('native_touch_firmware_bytes', weak[0])
        glue = (ROOT / 'platform/sheng/macos26/input/native_touch.cpp').read_text()
        self.assertIn('native_touch_firmware_bytes(uint32_t', glue)

    def test_device_tree_node_is_added_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'kernel.adt'
            path.write_bytes(node('device-tree', children=(node('chosen'), node('arm-io'))))
            props = {'compatible': b'sheng,nt36532\0', 'firmware': b'\x01' * 40, 'flip-x': struct.pack('<I', 0)}
            configure_native_text_state(path, development_root=True, touch_node=props)
            tree = read_tree(path.read_bytes())
            self.assertEqual(tree[('device-tree', 'arm-io', 'native-touch')]['firmware'], b'\x01' * 40)
            before = path.read_bytes()
            configure_native_text_state(path, development_root=True, touch_node=props)
            self.assertEqual(path.read_bytes(), before)
            with self.assertRaisesRegex(ValueError, 'conflicts'):
                configure_native_text_state(path, development_root=True, touch_node=props | {'firmware': b'\x02' * 40})
            with self.assertRaisesRegex(ValueError, 'firmware'):
                configure_native_text_state(path, development_root=True, touch_node={'compatible': b'x\0'})

    def test_packager_touch_arguments(self):
        import package_macos26_ufs as packager
        common = ['--kernel-build', 'k', '--qemu-observation', 'q', '--bootstrap-observation', 'b',
                  '--default-off-observation', 'd', '--firmware-build', 'f', '--backup-dir', 'x', '--output', 'o',
                  '--root-deployment', 'a', '--partition-plan', 'b', '--root-build', 'c', '--root-hash', 'd',
                  '--root-volume-uuid', '808eac5e-8275-478d-814c-2229d8eb8d07']

        def run(*arguments):
            stderr = io.StringIO()
            with mock.patch.object(sys, 'argv', ['package', *common, *arguments]), contextlib.redirect_stderr(stderr), \
                    mock.patch.object(packager, 'qualify', side_effect=AssertionError('qualification reached')):
                with self.assertRaises((SystemExit, AssertionError)) as raised:
                    packager.main()
            return raised.exception, stderr.getvalue()

        error, text = run('--touch-firmware', 'fw.bin')
        self.assertIn('require --touch-stage', text)
        error, text = run('--touch-stage', '4')
        self.assertIn('--touch-firmware', text)


if __name__ == '__main__':
    unittest.main()
