"""User-waived display diagnostic for a hardware package.

Used only when the user explicitly decides to boot a candidate whose same-kernel QEMU display window could not finish.
The waiver is bound to the packaged kernel and to a partial same-kernel QEMU display fixture. That fixture must show
its retained-log stream complete up to the halt, with GOP started, display on and WindowServer started and never exited.
The record lists exactly which display checks were NOT observed. This never substitutes for
display_diagnostic_evidence.qualify_display_observation or recovery_ui_evidence.qualify_language_selector, and it
does not qualify the hardware display.
"""
import hashlib
import json
from pathlib import Path
import re
import tomllib

SCHEMA = 'sheng-display-waiver-v1'
RENDER_PAGES = {0, 1, 4}
REQUIRED_ARGS = ('native-gop-display=1', 'sheng-recovery-ui=1', 'sheng-boot-progress=1')
# Checks that a waiver may leave unobserved; every other check must hold in the partial fixture.
WAIVABLE = ('first_swap', 'render_pages_applied')


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def qualify_display_waiver(build, waiver_path, identity):
    waiver_path = Path(waiver_path).resolve()
    waiver = json.loads(waiver_path.read_text(encoding='utf-8'))
    if waiver.get('schema') != SCHEMA or waiver.get('decided_by') != 'user':
        raise ValueError('display waiver must be an explicit user decision')
    decision = waiver.get('user_decision')
    if not isinstance(decision, str) or not decision.strip():
        raise ValueError('display waiver lacks the user decision text')
    if waiver.get('kernel_sha256') != identity['kernel_sha256'] or waiver.get('uuid') != identity['uuid']:
        raise ValueError('display waiver differs from the packaged kernel')
    fixture = Path(waiver['partial_fixture']).resolve()
    inputs = {'waiver': digest(waiver_path)}

    def read(name):
        path = fixture / name
        inputs[name] = digest(path)
        return path.read_bytes()

    failure = json.loads(read('gdb-failure.json'))
    if failure.get('kernel_sha256') != identity['kernel_sha256']:
        raise ValueError('partial display fixture differs from the packaged kernel')
    compiled = json.loads((Path(build) / 'build.json').read_text())['recovery_ui_policy']
    if compiled.get('default_off') is not True or compiled.get('original_signature_validation_required') is not True \
            or compiled.get('disk_writes') is not False:
        raise ValueError('requires the authenticated memory-only recovery UI policy')
    toml = tomllib.loads(read('esp/EFI/UniXNU/unixnu.toml').decode())
    args = toml['entry'][0]['args'].split()
    if any(required not in args for required in REQUIRED_ARGS):
        raise ValueError('partial fixture lacks the GOP, UI policy or progress boot arguments')
    samples = [json.loads(line) for line in read('stream-console.jsonl').decode().splitlines() if line.strip()]
    if not samples or any(s.get('missing_bytes') for s in samples) or any(s.get('guest_dropped_bytes') for s in samples):
        raise ValueError('requires a complete retained-log stream up to the halt')
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
        'no_panic_text_in_stream': 'panic(' not in log,
    }
    unobserved = sorted(k for k, v in checks.items() if not v)
    if any(k not in WAIVABLE for k in unobserved):
        raise ValueError('partial display evidence fails a non-waivable check: ' + ', '.join(unobserved))
    if unobserved != sorted(waiver.get('waived_checks', [])):
        raise ValueError('waiver must list exactly the unobserved checks: ' + ', '.join(unobserved))
    screen = waiver.get('failure_screen')
    if screen:
        if digest(fixture / screen['name']) != screen['sha256']:
            raise ValueError('failure screen differs from the waiver record')
        inputs[screen['name']] = screen['sha256']
    return dict(schema=SCHEMA, waived=True, user_decision=decision.strip(), decided_utc=waiver.get('decided_utc'),
                reason=waiver.get('reason'), waiver=str(waiver_path), fixture=str(fixture), input_sha256=inputs,
                checks=checks, waived_checks=unobserved, ui_pages_applied=sorted(applied),
                windowserver_pids=sorted(set(execs)), qemu_display_window_complete=False,
                boot_argument='native-gop-display=1 sheng-recovery-ui=1',
                language_selection_qualified=False, input_qualified=False,
                hardware_display_qualified=False, gpu_acceleration=False)
