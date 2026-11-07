import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from display_diagnostic_evidence import qualify_display_observation  # noqa: E402

IDENTITY = dict(kernel_sha256='ab' * 32, uuid='119B8C44-A448-3ADE-985B-8AD45E59B630')
GOOD_LOG = '\n'.join([
    'NativeGOPDisplay: started, display 800x600',
    'SHENG: Recovery UI policy 0 applied - authenticated memory only',
    'SHENG: Recovery UI policy 1 applied - authenticated memory only',
    'SHENG: Recovery UI policy 4 applied - authenticated memory only',
    'SHENG-PROGRESS: exec WindowServer pid=82 wait-status=0',
    'NativeGOPDisplay: display on',
    'NativeGOPDisplay: first swap submitted',
]) + '\n'


class DisplayDiagnosticTest(unittest.TestCase):
    def fixture(self, log=GOOD_LOG, args='cpus=1 native-gop-display=1 sheng-recovery-ui=1 sheng-boot-progress=1',
                kernel=IDENTITY['kernel_sha256'], missing=0):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        build, fixture = root / 'build', root / 'fixture'
        build.mkdir()
        (build / 'build.json').write_text(json.dumps(dict(recovery_ui_policy=dict(
            default_off=True, original_signature_validation_required=True, disk_writes=False))))
        (fixture / 'esp/EFI/UniXNU').mkdir(parents=True)
        (fixture / 'esp/EFI/UniXNU/unixnu.toml').write_text(
            f'version = 1\n[[entry]]\nid = "native26"\ntype = "xnu"\npath = "Payloads/kernel.macho"\nargs = "{args}"\n')
        (fixture / 'result.json').write_text(json.dumps(dict(kernel_sha256=kernel, uuid=IDENTITY['uuid'],
            recovery_ui_policy=True, native_gop_display=True, smp_observation=dict(cpu_count=1),
            qemu_platform_uuid='cbc69f0a-b9a2-4a1b-8307-3476e224736f')))
        (fixture / 'stream-console.jsonl').write_text(json.dumps(dict(missing_bytes=missing, guest_dropped_bytes=0)) + '\n')
        (fixture / 'stream-console.bin').write_text(log)
        (fixture / 'screen.png').write_bytes(b'\x89PNG\r\n\x1a\n')
        return build, fixture

    def test_accepts_stable_windowserver_and_first_frame(self):
        build, fixture = self.fixture()
        result = qualify_display_observation(build, fixture, IDENTITY)
        self.assertTrue(all(result['checks'].values()))
        self.assertFalse(result['language_selection_qualified'])
        self.assertFalse(result['input_qualified'])
        self.assertEqual(result['boot_argument'], 'native-gop-display=1 sheng-recovery-ui=1')

    def test_rejects_windowserver_exit(self):
        build, fixture = self.fixture(GOOD_LOG + 'SHENG-PROGRESS: exit WindowServer pid=82 wait-status=256\n')
        with self.assertRaisesRegex(ValueError, 'windowserver_never_exited'):
            qualify_display_observation(build, fixture, IDENTITY)

    def test_rejects_missing_render_page(self):
        build, fixture = self.fixture(GOOD_LOG.replace('policy 1 applied', 'policy 2 applied'))
        with self.assertRaisesRegex(ValueError, 'render_pages_applied'):
            qualify_display_observation(build, fixture, IDENTITY)

    def test_rejects_policy_failure_and_panic(self):
        build, fixture = self.fixture(GOOD_LOG + 'SHENG: Recovery UI policy 3 page digest or instructions differ\npanic(cpu 0\n')
        with self.assertRaisesRegex(ValueError, 'no_policy_failures.*no_panic_text'):
            qualify_display_observation(build, fixture, IDENTITY)

    def test_rejects_other_kernel_incomplete_log_and_missing_args(self):
        build, fixture = self.fixture(kernel='cd' * 32)
        with self.assertRaisesRegex(ValueError, 'differs from the packaged kernel'):
            qualify_display_observation(build, fixture, IDENTITY)
        build, fixture = self.fixture(missing=12)
        with self.assertRaisesRegex(ValueError, 'complete retained-log stream'):
            qualify_display_observation(build, fixture, IDENTITY)
        build, fixture = self.fixture(args='cpus=1 native-gop-display=1')
        with self.assertRaisesRegex(ValueError, 'boot arguments'):
            qualify_display_observation(build, fixture, IDENTITY)

    def test_rejects_missing_first_swap(self):
        build, fixture = self.fixture(GOOD_LOG.replace('first swap submitted', 'swap pending'))
        with self.assertRaisesRegex(ValueError, 'first_swap'):
            qualify_display_observation(build, fixture, IDENTITY)


if __name__ == '__main__':
    unittest.main()
