#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Derive the hardware display source from the VM-run Stage18B source; offline, writes only platform files."""
import argparse
import difflib
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DISPLAY = ROOT / 'platform/sheng/macos26/display'
# Exact inputs, relative to the research worktree that holds the Stage15B and Stage18B CI payloads.
INPUTS = {
    'original': ('out/stage15-gop-copy-path/original/DroidVMDisplay.cpp',
                 '050ae69eec6f0340c4f3659285dfb16fce13058648a044f9149b89d40344bcac'),
    'stage18b_source': ('out/stage18b-display-awake/payload/gop-copy-path/display/NativeGOPDisplay.cpp',
                        'd5c31fd87d26a1c6755af85a59f950fc532b8b363f97650f1c27e90eb9b26339'),
    'stage18b_patch': ('out/stage18b-display-awake/payload/gop-copy-path/display/0001-native-gop-display.patch',
                       'fcb283d657c6f15e25f5829de411852e3c6f4f5e18eee61966e86dae64f8dc10'),
    'stage15b_scanout': ('out/stage15b-source-staging/payload/gop-copy-path/display/gop_scanout.h',
                         '65f31d0597f77975df933c86f0acd729a223d50e44323fd0d0f7b80d857179c9'),
}
HEADER_MARKER = '--- a/IOMobileFramebufferABI.h\n'

OLD_HELPERS = '''// Explicit VM diagnostic policy. These are requests to the original PM
// user-activity consumer, not HID events, a display assertion, or power overrides.
// State is file-local so the paired NativeGOPDisplay class/virtual ABI is unchanged.
static IOService *gVMKeepAwakeOwner = nullptr;
static uint64_t gVMKeepAwakeDeadline = UINT64_MAX;

static bool nativeGopStartVMKeepAwake(IOService *owner)
{
    IOPMrootDomain *root = IOService::getPMRootDomain();
    if (!root) {
        IOLog("NativeGOPDisplay: VM keep-awake root domain not ready\\n");
        return false;
    }
    IOService *expected = nullptr;
    if (!__atomic_compare_exchange_n(&gVMKeepAwakeOwner, &expected, owner, false,
                                      __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE)) {
        IOLog("NativeGOPDisplay: VM keep-awake already has an owner\\n");
        return false;
    }
    uint64_t deadline;
    clock_interval_to_deadline(30, kSecondScale, &deadline);
    __atomic_store_n(&gVMKeepAwakeDeadline, deadline, __ATOMIC_RELEASE);
    root->requestUserActive(owner, "sheng-qemu-display-observation");
    IOLog("NativeGOPDisplay: VM requestUserActive submitted (start)\\n");
    return true;
}

static void nativeGopVMKeepAwakeTick(IOService *owner)
{
    if (__atomic_load_n(&gVMKeepAwakeOwner, __ATOMIC_ACQUIRE) != owner)
        return;
    uint64_t deadline = __atomic_load_n(&gVMKeepAwakeDeadline, __ATOMIC_ACQUIRE);
    if (deadline == UINT64_MAX || mach_absolute_time() < deadline)
        return;
    uint64_t next;
    clock_interval_to_deadline(30, kSecondScale, &next);
    if (!__atomic_compare_exchange_n(&gVMKeepAwakeDeadline, &deadline, next, false,
                                      __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE))
        return;
    IOPMrootDomain *root = IOService::getPMRootDomain();
    if (!root) {
        IOLog("NativeGOPDisplay: VM keep-awake root domain unavailable; no request\\n");
        return;
    }
    root->requestUserActive(owner, "sheng-qemu-display-observation");
    IOLog("NativeGOPDisplay: VM requestUserActive submitted (periodic)\\n");
}
'''

# The VM path keeps its symbols, reason string and log text; the hardware switch reuses the same owner/deadline.
NEW_HELPERS = '''// Explicit display-activity policy, default off. These are requests to the original PM
// user-activity consumer, not HID events, a display assertion, or power overrides.
// native-gop-vm-keep-awake=1 is the Stage18B VMM-only observation switch. native-gop-keep-awake=1
// sends the same request on hardware (PE_vmm_present == 0), which has no input driver yet.
// State is file-local so the paired NativeGOPDisplay class/virtual ABI is unchanged.
static IOService *gVMKeepAwakeOwner = nullptr;
static uint64_t gVMKeepAwakeDeadline = UINT64_MAX;
// Written by the owner before the deadline is published (release); ticks read it after loading the deadline (acquire).
static bool gKeepAwakeHardware = false;

static bool nativeGopHardwareKeepAwakeAllowed(unsigned hardware, unsigned vm, uint32_t vmm)
{
    return hardware == 0 || (hardware == 1 && vm == 0 && vmm == 0);
}

static const char *nativeGopKeepAwakeLabel(bool hardware)
{
    return hardware ? "hardware" : "VM";
}

static const char *nativeGopKeepAwakeReason(bool hardware)
{
    return hardware ? "sheng-native-display-keep-awake" : "sheng-qemu-display-observation";
}

static bool nativeGopStartVMKeepAwake(IOService *owner, bool hardware)
{
    IOPMrootDomain *root = IOService::getPMRootDomain();
    if (!root) {
        IOLog("NativeGOPDisplay: %s keep-awake root domain not ready\\n", nativeGopKeepAwakeLabel(hardware));
        return false;
    }
    IOService *expected = nullptr;
    if (!__atomic_compare_exchange_n(&gVMKeepAwakeOwner, &expected, owner, false,
                                      __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE)) {
        IOLog("NativeGOPDisplay: %s keep-awake already has an owner\\n", nativeGopKeepAwakeLabel(hardware));
        return false;
    }
    __atomic_store_n(&gKeepAwakeHardware, hardware, __ATOMIC_RELAXED);
    uint64_t deadline;
    clock_interval_to_deadline(30, kSecondScale, &deadline);
    __atomic_store_n(&gVMKeepAwakeDeadline, deadline, __ATOMIC_RELEASE);
    root->requestUserActive(owner, nativeGopKeepAwakeReason(hardware));
    IOLog("NativeGOPDisplay: %s requestUserActive submitted (start)\\n", nativeGopKeepAwakeLabel(hardware));
    return true;
}

static void nativeGopVMKeepAwakeTick(IOService *owner)
{
    if (__atomic_load_n(&gVMKeepAwakeOwner, __ATOMIC_ACQUIRE) != owner)
        return;
    uint64_t deadline = __atomic_load_n(&gVMKeepAwakeDeadline, __ATOMIC_ACQUIRE);
    if (deadline == UINT64_MAX || mach_absolute_time() < deadline)
        return;
    bool hardware = __atomic_load_n(&gKeepAwakeHardware, __ATOMIC_RELAXED);
    uint64_t next;
    clock_interval_to_deadline(30, kSecondScale, &next);
    if (!__atomic_compare_exchange_n(&gVMKeepAwakeDeadline, &deadline, next, false,
                                      __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE))
        return;
    IOPMrootDomain *root = IOService::getPMRootDomain();
    if (!root) {
        IOLog("NativeGOPDisplay: %s keep-awake root domain unavailable; no request\\n", nativeGopKeepAwakeLabel(hardware));
        return;
    }
    root->requestUserActive(owner, nativeGopKeepAwakeReason(hardware));
    IOLog("NativeGOPDisplay: %s requestUserActive submitted (periodic)\\n", nativeGopKeepAwakeLabel(hardware));
}
'''

REPLACEMENTS = (
    ('// Exact paired pexpert.h XNU_KERNEL_PRIVATE declaration; VM opt-in only.\n',
     '// Exact paired pexpert.h XNU_KERNEL_PRIVATE declaration; selects the VM or hardware opt-in.\n'),
    (OLD_HELPERS, NEW_HELPERS),
    ('        IOLog("NativeGOPDisplay: VM keep-awake requires explicit VMM opt-in\\n");\n'
     '        return false;\n'
     '    }\n',
     '        IOLog("NativeGOPDisplay: VM keep-awake requires explicit VMM opt-in\\n");\n'
     '        return false;\n'
     '    }\n'
     '    unsigned hwKeepAwake = 0;\n'
     '    PE_parse_boot_argn("native-gop-keep-awake", &hwKeepAwake, sizeof(hwKeepAwake));\n'
     '    if (!nativeGopHardwareKeepAwakeAllowed(hwKeepAwake, vmKeepAwake, PE_vmm_present)) {\n'
     '        IOLog("NativeGOPDisplay: hardware keep-awake requires explicit non-VMM opt-in\\n");\n'
     '        return false;\n'
     '    }\n'),
    ('    if (vmKeepAwake == 1 && !nativeGopStartVMKeepAwake(this)) {\n',
     '    if ((vmKeepAwake == 1 || hwKeepAwake == 1) && !nativeGopStartVMKeepAwake(this, hwKeepAwake == 1)) {\n'),
)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def full_patch(original: str, source: str, header_hunk: str) -> str:
    return ''.join(difflib.unified_diff(original.splitlines(True), source.splitlines(True),
                                        fromfile='a/DroidVMDisplay.cpp', tofile='b/DroidVMDisplay.cpp')) + header_hunk


def section(text: str, begin: str, end: str) -> str:
    return text[text.index(begin):text.index(end)]


def derive(research: Path) -> dict:
    data = {}
    for key, (relative, digest) in INPUTS.items():
        raw = (research / relative).read_bytes()
        if sha(raw) != digest:
            raise ValueError(f'{key} input differs from the reviewed Stage18B payload: {relative}')
        data[key] = raw.decode()
    stage18b_patch = data['stage18b_patch']
    if stage18b_patch.count(HEADER_MARKER) != 1:
        raise ValueError('Stage18B patch has no single framebuffer header hunk')
    header_hunk = stage18b_patch[stage18b_patch.index(HEADER_MARKER):]
    # The derivation method must reproduce the Stage18B patch byte for byte before it is trusted.
    if full_patch(data['original'], data['stage18b_source'], header_hunk) != stage18b_patch:
        raise ValueError('Stage18B patch is not the unified diff of its source')
    source = data['stage18b_source']
    for old, new in REPLACEMENTS:
        if source.count(old) != 1:
            raise ValueError('Stage18B source anchor not found exactly once: ' + old.splitlines()[0])
        source = source.replace(old, new)
    before = data['stage18b_source']
    for begin, end in (('class NativeGOPDisplay :', 'OSDefineMetaClassAndStructors'),
                       ('void NativeGOPDisplay::stop(IOService *provider)', 'bool NativeGOPDisplay::start_hardware()'),
                       ('bool NativeGOPDisplay::start_hardware()', 'void NativeGOPDisplay::copySurface(')):
        if section(before, begin, end) != section(source, begin, end):
            raise ValueError('unexpected change outside the keep-awake policy: ' + begin)
    if before[before.index('void NativeGOPDisplay::copySurface('):] != source[source.index('void NativeGOPDisplay::copySurface('):]:
        raise ValueError('pixel copy, power-state or later code changed')
    patch = full_patch(data['original'], source, header_hunk)
    (DISPLAY / '0001-native-gop-display.patch').write_bytes(patch.encode())
    (DISPLAY / 'gop_scanout.h').write_bytes(data['stage15b_scanout'].encode())
    record = dict(
        schema='sheng-hw-display-keep-awake-v1', status='source_only', compiled=False, ci_submitted=False,
        hardware_run=False, vm_run=False,
        inputs={key: dict(path=relative, sha256=digest) for key, (relative, digest) in INPUTS.items()},
        stage18b_patch_reproduced=True, class_declaration_byte_identical=True,
        stop_and_hardware_start_unchanged=True, pixel_copy_and_power_state_unchanged=True,
        framebuffer_header_hunk_unchanged=True,
        vm_switch=dict(boot_argument='native-gop-vm-keep-awake=1', guard='PE_vmm_present == 1',
                       reason='sheng-qemu-display-observation', log_text_unchanged=True),
        hardware_switch=dict(boot_argument='native-gop-keep-awake=1',
                             guard='PE_vmm_present == 0 and native-gop-vm-keep-awake unset',
                             reason='sheng-native-display-keep-awake', default_off=True),
        source_sha256=sha(source.encode()), patch_sha256=sha(patch.encode()),
        scanout_sha256=sha(data['stage15b_scanout'].encode()),
        note=('Requests the original PM user-activity path every 30 s; requestUserActive returns void, so a submitted '
              'request does not prove the display stayed on. System sleep is refused separately by the ADT '
              'sleep-disabled policy.'))
    (DISPLAY / 'hw-keep-awake.json').write_text(json.dumps(record, indent=2) + '\n')
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--research-worktree', type=Path, required=True,
                        help='checkout that holds out/stage15-gop-copy-path, out/stage15b-source-staging and out/stage18b-display-awake')
    args = parser.parse_args()
    print(json.dumps(derive(args.research_worktree), indent=2))


if __name__ == '__main__':
    main()
