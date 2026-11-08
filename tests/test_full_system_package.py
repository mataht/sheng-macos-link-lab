# SPDX-License-Identifier: GPL-3.0-or-later
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import package_macos26_ufs as packager  # noqa: E402
import prepare_macos26_full_root as prepare  # noqa: E402
import inspect_macos26_full_image as inspector  # noqa: E402

spec = importlib.util.spec_from_file_location('image_fixture', ROOT / 'tests/test_full_system_qemu.py')
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
OVERLAY_HEADER = ROOT / 'platform/sheng/macos26/storage/full_root_overlay.h'


class FullSystemPackageTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.dir = Path(folder.name)
        image = self.dir / 'disk.img'
        image.write_bytes(fixture.variant())
        report = self.dir / 'disk.img.json'
        report.write_text(json.dumps(inspector.inspect(image)))
        self.package = self.dir / 'package'
        prepare.prepare(image, report, self.package)
        self.contract = json.loads((self.package / 'macos26-full-root.json').read_text())
        build = json.loads((self.package / 'build.json').read_text())
        self.plan = self.dir / 'plan'
        self.plan.mkdir()
        (self.plan / 'plan.json').write_text(json.dumps(dict(
            applied=True, gpt_readback_verified=True, full_name='macossystem', full_bytes=64 << 30,
            full_unique_guid='12345678-0000-4000-8000-000000000001')))
        self.install = self.dir / 'install'
        self.install.mkdir()
        self.install_record = dict(verified=True, destination='/dev/block/by-name/macossystem',
                                   destination_guid='12345678-0000-4000-8000-000000000001',
                                   manifest_sha256=build['manifest_sha256'], window=build['window'])
        (self.install / 'install.json').write_text(json.dumps(self.install_record))

    def test_deployment_binds_plan_package_and_install(self):
        media, partition = packager.qualify_full_deployment(self.plan, self.package, self.install, self.contract)
        self.assertEqual(media['sha256'], self.contract['sha256'])
        self.assertEqual(partition['root_unique_guid'], '12345678-0000-4000-8000-000000000001')
        for change in (dict(verified=False), dict(destination='/dev/block/by-name/macosroot'),
                       dict(destination_guid='12345678-0000-4000-8000-000000000002'),
                       dict(manifest_sha256='0' * 64)):
            (self.install / 'install.json').write_text(json.dumps(self.install_record | change))
            with self.assertRaisesRegex(ValueError, 'macossystem'):
                packager.qualify_full_deployment(self.plan, self.package, self.install, self.contract)
        (self.install / 'install.json').write_text(json.dumps(self.install_record))
        with self.assertRaisesRegex(ValueError, 'macossystem'):
            packager.qualify_full_deployment(self.plan, self.package, self.install, self.contract | dict(sha256='0' * 64))

    def test_driver_requires_full_identity_and_current_overlay_header(self):
        build = dict(full_system_root=True, full_root_overlay=dict(
            self.contract['overlay'], volume_group=self.contract['volume_group'], seed_read_only=True,
            header_sha256=hashlib.sha256(OVERLAY_HEADER.read_bytes()).hexdigest()))
        packager.qualify_full_driver(dict(build=build), self.contract)
        for change in (dict(full_system_root=False), dict(full_root_overlay=dict(build['full_root_overlay'], header_sha256='0' * 64)),
                       dict(full_root_overlay=dict(build['full_root_overlay'], volume_group='other'))):
            with self.assertRaisesRegex(ValueError, 'full-system root identity'):
                packager.qualify_full_driver(dict(build=build | change), self.contract)

    def test_dyld_evidence_must_be_this_kernel_on_this_image(self):
        patch = ROOT / 'platform/sheng/macos26/patches/0053-development-dyld-commpage.patch'
        build = self.dir / 'kernel'
        build.mkdir()
        (build / 'build.json').write_text(json.dumps(dict(patches={
            str(patch.relative_to(ROOT)): hashlib.sha256(patch.read_bytes()).hexdigest()})))
        identity = dict(uuid='U', kernel_sha256='K')
        good = dict(uuid='U', kernel_sha256='K', development_no_ignition=True, qemu_no_ignition=False,
                    observed_globals={'_sheng_development_no_ignition_enabled': '0x1'},
                    actual_commpage_dyld_flags='0x9', boot_args='-v -sheng-development-no-ignition -nobsdmgroot',
                    full_system={k: self.contract[k] for k in ('volume_group', 'root_snapshot', 'bytes')}
                    | dict(system_volume=self.contract['volume_uuid']),
                    userland_started=True, launchd_main_started=True)
        observation = self.dir / 'run'
        observation.mkdir()
        (observation / 'result.json').write_text(json.dumps(good))
        packager.qualify_full_system_dyld(build, observation, identity, self.contract)
        for change in (dict(kernel_sha256='other'), dict(boot_args='-v'), dict(launchd_main_started=False),
                       dict(actual_commpage_dyld_flags='0x1'),
                       dict(full_system=dict(good['full_system'], root_snapshot='com.apple.os.update-X'))):
            (observation / 'result.json').write_text(json.dumps(good | change))
            with self.assertRaisesRegex(ValueError, 'full-system image'):
                packager.qualify_full_system_dyld(build, observation, identity, self.contract)

    def test_persistent_writes_require_the_compiled_fail_stop_window(self):
        header = ROOT / 'platform/sheng/macos26/storage/full_root_writes.h'
        writes = dict(boot_argument='native-ufs-root-writes=1', default_off=True, window_only=True,
                      header_excluded=True, fail_stop=True, exclusive_with_overlay=True,
                      header_sha256=hashlib.sha256(header.read_bytes()).hexdigest())
        self.assertEqual(packager.qualify_full_writes(dict(build=dict(full_root_writes=writes))), writes)
        for change in (dict(fail_stop=False), dict(window_only=False), dict(header_sha256='0' * 64), dict(default_off=False)):
            with self.assertRaisesRegex(ValueError, 'fail-stop persistent'):
                packager.qualify_full_writes(dict(build=dict(full_root_writes=writes | change)))
        with self.assertRaisesRegex(ValueError, 'fail-stop persistent'):
            packager.qualify_full_writes(dict(build={}))

    def run_main(self, *arguments):
        common = ['--kernel-build', 'k', '--qemu-observation', 'q', '--bootstrap-observation', 'b',
                  '--default-off-observation', 'd', '--firmware-build', 'f', '--backup-dir', 'x', '--output', 'o']
        stderr = io.StringIO()
        with mock.patch.object(sys, 'argv', ['package', *common, *arguments]), contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                packager.main()
        self.assertEqual(raised.exception.code, 2)
        return stderr.getvalue()

    def write_candidates(self, **changes):
        base = dict(kernel_sha256='k', bootkc_sha256='b', native_kernel_uuid='u', modules={'a': {}, 'b': {}}, qemu_only=False,
                    candidate_sha256='hw', storage_driver=dict(bundle='org.unixnu.driver.NativeUfs', build={}))
        sibling = dict(base, qemu_only=True, candidate_sha256='vm', storage_driver=dict(
            bundle='org.unixnu.driver.NativeVirtioBlock', build=dict(qemu_only_display=True)))
        sibling.update(changes)
        ours, theirs = self.dir / 'hw', self.dir / 'vm'
        for path, value in ((ours, base), (theirs, sibling)):
            path.mkdir(exist_ok=True)
            (path / 'security-relink.json').write_text(json.dumps(value))
        return ours, theirs

    def test_sibling_must_be_the_same_ci_kernel_and_modules(self):
        ours, theirs = self.write_candidates()
        self.assertEqual(packager.qualify_full_sibling(ours, theirs)['candidate_sha256'], 'vm')
        ours, theirs = self.write_candidates(kernel_sha256='other')
        with self.assertRaisesRegex(ValueError, 'same CI kernel'):
            packager.qualify_full_sibling(ours, theirs)
        ours, theirs = self.write_candidates(modules={'a': {}, 'c': {}})
        with self.assertRaisesRegex(ValueError, 'module set'):
            packager.qualify_full_sibling(ours, theirs)
        ours, theirs = self.write_candidates(storage_driver=dict(bundle='org.unixnu.driver.NativeUfs', build={}))
        with self.assertRaisesRegex(ValueError, 'VirtIO display build'):
            packager.qualify_full_sibling(ours, theirs)

    def test_display_needs_the_table_seen_in_qemu(self):
        manifest = hashlib.sha256((ROOT / 'config/macos26-full-ui-pages.json').read_bytes()).hexdigest()
        build = self.dir / 'display'
        build.mkdir()
        (build / 'build.json').write_text(json.dumps(dict(full_ui_policy=dict(manifest_sha256=manifest))))
        driver = dict(build=dict(native_gop_display={'x': 1}))
        seen = dict(full_ui_policy=dict(manifest_sha256=manifest, boot_argument_enabled=True),
                    observed_globals={'_sheng_recovery_ui_pages_applied': '0x6'})
        record = packager.qualify_full_display(build, driver, seen)
        self.assertEqual(record['boot_argument'], 'native-gop-display=1 sheng-full-ui=1')
        self.assertEqual(record['qemu_pages_applied'], 6)
        with self.assertRaises(ValueError):
            packager.qualify_full_display(build, driver, dict(seen, observed_globals={}))
        with self.assertRaises(ValueError):
            packager.qualify_full_display(build, dict(build={}), seen)

    def test_mode_arguments_are_exclusive(self):
        self.assertIn('Recovery root requires', self.run_main())
        self.assertIn('--full-system requires', self.run_main('--full-system'))
        full = ['--full-system', '--full-system-plan', 'p', '--full-system-package', 'k', '--full-system-install', 'i',
                '--root-overlay-mib', '1024', '--development-no-ignition', '--dyld-observation', 'o']
        self.assertIn('excludes Recovery', self.run_main(*full, '--root-hash', 'h'))
        self.assertIn('excludes Recovery', self.run_main(*full, '--isc-overlay'))
        self.assertIn('also requires --full-system-trust-cache', self.run_main(*full))
        ready = full + ['--full-system-trust-cache', 't', '--full-system-sibling', 's',
                        '--platform-uuid', 'c6de5fe2-c2a3-4a1d-b2c8-92a28a45383a']
        self.assertIn('require the GOP display', self.run_main(*ready, '--button-keys'))
        self.assertIn('256..8192', self.run_main(*full[:-4], '128', '--development-no-ignition', '--dyld-observation', 'o'))
        self.assertIn('exactly one', self.run_main(*full, '--root-writes'))
        no_mode = [a for a in full if a not in ('--root-overlay-mib', '1024')]
        self.assertIn('exactly one', self.run_main(*no_mode))
        self.assertIn('require --full-system', self.run_main('--root-deployment', 'a', '--partition-plan', 'b',
                                                             '--root-build', 'c', '--root-hash', 'd',
                                                             '--root-volume-uuid', '808eac5e-8275-478d-814c-2229d8eb8d07',
                                                             '--root-overlay-mib', '512'))


if __name__ == '__main__':
    unittest.main()
