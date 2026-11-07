import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from display_waiver_evidence import qualify_display_waiver  # noqa: E402

IDENTITY = dict(kernel_sha256='ab' * 32, uuid='2D532696-7C00-351D-9D98-F8FC369380E6')
PARTIAL_LOG = '\n'.join([
    'NativeGOPDisplay: started, display 800x600',
    'SHENG-PROGRESS: exec WindowServer pid=82 wait-status=0',
    'SHENG: Recovery UI policy 4 applied - authenticated memory only',
    'NativeGOPDisplay: display on',
    'SHENG: Recovery UI policy 1 applied - authenticated memory only',
]) + '\n'
SCREEN = b'\x89PNG\r\n\x1a\nscreen'


class DisplayWaiverTest(unittest.TestCase):
    def fixture(self, log=PARTIAL_LOG, waived=('first_swap', 'render_pages_applied'), kernel=IDENTITY['kernel_sha256'],
                decided_by='user', decision='直接打包上机', missing=0, screen_sha=None):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        build, fixture = root / 'build', root / 'fixture'
        build.mkdir()
        (build / 'build.json').write_text(json.dumps(dict(recovery_ui_policy=dict(
            default_off=True, original_signature_validation_required=True, disk_writes=False))))
        (fixture / 'esp/EFI/UniXNU').mkdir(parents=True)
        (fixture / 'esp/EFI/UniXNU/unixnu.toml').write_text(
            'version = 1\n[[entry]]\nid = "native26"\ntype = "xnu"\npath = "Payloads/kernel.macho"\n'
            'args = "cpus=1 native-gop-display=1 sheng-recovery-ui=1 sheng-boot-progress=1"\n')
        (fixture / 'gdb-failure.json').write_text(json.dumps(dict(kernel_sha256=kernel)))
        (fixture / 'stream-console.jsonl').write_text(json.dumps(dict(missing_bytes=missing, guest_dropped_bytes=0)) + '\n')
        (fixture / 'stream-console.bin').write_text(log)
        (fixture / 'failure-screen.png').write_bytes(SCREEN)
        waiver = root / 'waiver.json'
        waiver.write_text(json.dumps(dict(schema='sheng-display-waiver-v1', decided_by=decided_by,
            user_decision=decision, kernel_sha256=IDENTITY['kernel_sha256'], uuid=IDENTITY['uuid'],
            partial_fixture=str(fixture), waived_checks=list(waived),
            failure_screen=dict(name='failure-screen.png', sha256=screen_sha or hashlib.sha256(SCREEN).hexdigest()))),
            encoding='utf-8')
        return build, waiver

    def test_accepts_exactly_listed_unobserved_checks(self):
        build, waiver = self.fixture()
        result = qualify_display_waiver(build, waiver, IDENTITY)
        self.assertTrue(result['waived'])
        self.assertEqual(result['waived_checks'], ['first_swap', 'render_pages_applied'])
        self.assertFalse(result['qemu_display_window_complete'])
        self.assertFalse(result['hardware_display_qualified'])
        self.assertEqual(result['boot_argument'], 'native-gop-display=1 sheng-recovery-ui=1')

    def test_rejects_unlisted_or_overlisted_checks(self):
        build, waiver = self.fixture(waived=('first_swap',))
        with self.assertRaisesRegex(ValueError, 'exactly the unobserved'):
            qualify_display_waiver(build, waiver, IDENTITY)
        log = PARTIAL_LOG + 'SHENG: Recovery UI policy 0 applied - x\nNativeGOPDisplay: first swap submitted\n'
        build, waiver = self.fixture(log=log)
        with self.assertRaisesRegex(ValueError, 'exactly the unobserved'):
            qualify_display_waiver(build, waiver, IDENTITY)

    def test_rejects_non_waivable_failures(self):
        build, waiver = self.fixture(log=PARTIAL_LOG + 'SHENG-PROGRESS: exit WindowServer pid=82 wait-status=256\n')
        with self.assertRaisesRegex(ValueError, 'non-waivable'):
            qualify_display_waiver(build, waiver, IDENTITY)
        build, waiver = self.fixture(log=PARTIAL_LOG.replace('display on', 'display off'))
        with self.assertRaisesRegex(ValueError, 'non-waivable'):
            qualify_display_waiver(build, waiver, IDENTITY)

    def test_rejects_other_kernel_non_user_and_incomplete_stream(self):
        build, waiver = self.fixture(kernel='cd' * 32)
        with self.assertRaisesRegex(ValueError, 'partial display fixture differs'):
            qualify_display_waiver(build, waiver, IDENTITY)
        build, waiver = self.fixture(decided_by='agent')
        with self.assertRaisesRegex(ValueError, 'explicit user decision'):
            qualify_display_waiver(build, waiver, IDENTITY)
        build, waiver = self.fixture(decision='  ')
        with self.assertRaisesRegex(ValueError, 'decision text'):
            qualify_display_waiver(build, waiver, IDENTITY)
        build, waiver = self.fixture(missing=4)
        with self.assertRaisesRegex(ValueError, 'complete retained-log stream'):
            qualify_display_waiver(build, waiver, IDENTITY)
        build, waiver = self.fixture(screen_sha='00' * 32)
        with self.assertRaisesRegex(ValueError, 'failure screen'):
            qualify_display_waiver(build, waiver, IDENTITY)
        with self.assertRaisesRegex(ValueError, 'differs from the packaged kernel'):
            qualify_display_waiver(build, waiver, dict(IDENTITY, uuid='00000000-0000-0000-0000-000000000000'))


if __name__ == '__main__':
    unittest.main()
