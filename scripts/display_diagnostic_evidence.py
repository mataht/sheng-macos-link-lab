"""Display-only diagnostic gate for a hardware package.

Same-kernel QEMU evidence that, with the GOP companion and the recovery UI policy enabled, WindowServer stays up and the
GOP companion submits a first frame. This does NOT qualify language selection, input or a usable Recovery GUI, and it
never replaces recovery_ui_evidence.qualify_language_selector; packages built with it carry display_diagnostic only.
"""
import hashlib
import json
from pathlib import Path
import re
import tomllib

SCHEMA = 'sheng-display-diagnostic-v1'
RENDER_PAGES = {0, 1, 4}  # native-no-metal scaled plan, BGRA framebuffer, software BGRA pool
REQUIRED_ARGS = ('native-gop-display=1', 'sheng-recovery-ui=1', 'sheng-boot-progress=1')


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def qualify_display_observation(build, fixture, identity):
    fixture = Path(fixture).resolve()
    inputs = {}

    def read(name):
        path = fixture / name
        inputs[name] = digest(path)
        return path.read_bytes()

    result = json.loads(read('result.json'))
    if result.get('kernel_sha256') != identity['kernel_sha256'] or result.get('uuid') != identity['uuid']:
        raise ValueError('display evidence differs from the packaged kernel')
    compiled = json.loads((Path(build) / 'build.json').read_text())['recovery_ui_policy']
    if result.get('recovery_ui_policy') is not True or compiled.get('default_off') is not True \
            or compiled.get('original_signature_validation_required') is not True or compiled.get('disk_writes') is not False:
        raise ValueError('requires the authenticated memory-only recovery UI policy')
    if result.get('native_gop_display') is not True:
        raise ValueError('requires the GOP display observation')
    toml = tomllib.loads(read('esp/EFI/UniXNU/unixnu.toml').decode())
    args = toml['entry'][0]['args'].split()
    if any(required not in args for required in REQUIRED_ARGS):
        raise ValueError('display evidence lacks the GOP, UI policy or progress boot arguments')
    samples = [json.loads(line) for line in read('stream-console.jsonl').decode().splitlines() if line.strip()]
    if not samples or any(s.get('missing_bytes') for s in samples) or any(s.get('guest_dropped_bytes') for s in samples):
        raise ValueError('requires a complete retained-log stream')
    log = read('stream-console.bin').decode('utf-8', 'replace')
    applied = {int(n) for n in re.findall(r'Recovery UI policy (\d+) applied', log)}
    failures = re.findall(r'Recovery UI policy \d+ (?:requires|page digest)', log)
    execs = re.findall(r'SHENG-PROGRESS: exec WindowServer pid=(\d+)', log)
    exits = re.findall(r'SHENG-PROGRESS: exit WindowServer pid=(\d+) wait-status=(\d+)', log)
    checks = {
        'gop_started': 'NativeGOPDisplay: started, display' in log,
        'display_on': 'NativeGOPDisplay: display on' in log,
        'first_swap': 'NativeGOPDisplay: first swap submitted' in log,
        'render_pages_applied': RENDER_PAGES <= applied,
        'no_policy_failures': not failures,
        'windowserver_started': bool(execs),
        'windowserver_never_exited': not exits,
        'no_panic_text': 'panic(' not in log,
    }
    if not all(checks.values()):
        raise ValueError('display diagnostic conditions failed: ' + ', '.join(k for k, v in checks.items() if not v))
    read('screen.png')
    return dict(schema=SCHEMA, fixture=str(fixture), input_sha256=inputs, checks=checks,
                ui_pages_applied=sorted(applied), windowserver_pids=sorted(set(execs)),
                observation_cpus=(result.get('smp_observation') or {}).get('cpu_count'),
                observation_platform_uuid=result.get('qemu_platform_uuid'),
                boot_argument='native-gop-display=1 sheng-recovery-ui=1',
                language_selection_qualified=False, input_qualified=False,
                hardware_display_qualified=False, gpu_acceleration=False)
