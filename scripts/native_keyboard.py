# SPDX-License-Identifier: GPL-3.0-or-later
"""Contract of the default-off keyboard cover companion (platform/sheng/macos26/input/native_keyboard.cpp).

The GENI I2C core, the WN8030 codec and the RPMh request builder are files of upstream UniXNU-sheng, imported unchanged in
platform/sheng/macos26/input/keyboard/. One definition shared by the storage builder (sources, compile commands, personalities,
evidence), the storage validator, the device-tree helper and the packager. The controller RAM image is a vendor file and is never part
of a build: only a local package carries it in /arm-io/native-keyboard.
"""
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLATFORM_DIR = 'platform/sheng/macos26/input'
PLATFORM_SOURCES = ('native_keyboard.cpp', 'spmi_keys.h')
IMPORTED_DIR = PLATFORM_DIR + '/keyboard'
IMPORTED_C = ('GeniI2c.c', 'Wn8030.c', 'QcomRpmh.c')
IMPORTED_HEADERS = ('GeniI2c.h', 'Wn8030.h', 'QcomRpmh.h')
# Project files beside the imports: the imported I2C core and RPMh builder compile under private link names, because other companions in the
# same kernel module import the same upstream files.
NAME_WRAPPERS = ('KeyboardNames.h', 'KeyboardGeniI2c.c', 'KeyboardRpmh.c')
COMPILED_C = ('KeyboardGeniI2c.c', 'KeyboardRpmh.c', 'Wn8030.c')
UPSTREAM_HEADER = 'UniXNU-sheng/package/TouchBoard.h'
IMPORTED_COMMIT = 'a68ee4ee67cc71792fbf519de7e3e28072c717b5'
STAGE_ARGUMENT = 'sheng-kbd'
RAILS_ARGUMENT = 'sheng-kbd-rails=1'
KHZ_ARGUMENT = 'sheng-kbd-khz'
POLL_READ_ARGUMENT = 'sheng-kbd-poll-read'
NODE = 'native-keyboard'
# Size observed on the CN tablet and in upstream's record; digest from upstream's proprietary-files.txt. Python only, so a tablet whose
# file differs needs a change here and a new package, not a new kernel.
FIRMWARE_SIZE = 82524
FIRMWARE_SHA256 = '9eb7213603474d926d3af1124d3c14d63cbfc58ea2f7f7d9fedc088093fd63c5'
VENDOR_ID, PRODUCT_ID = 0x1209, 0x534b

PERSONALITY = dict(IOClass='NativeKeyboard', IOProviderClass='IOPlatformExpertDevice',
                   IOMatchCategory='NativeKeyboard', IOProbeScore=500)
EVENT_DRIVER = dict(CFBundleIdentifier='com.apple.iokit.IOHIDFamily', IOClass='IOHIDEventDriver',
                    IOProviderClass='IOHIDInterface', VendorID=VENDOR_ID, ProductID=PRODUCT_ID, IOProbeScore=3000)


def source_hashes(root: Path = ROOT) -> dict:
    files = [Path(PLATFORM_DIR) / name for name in PLATFORM_SOURCES]
    files += [Path(IMPORTED_DIR) / name for name in (*IMPORTED_C, *IMPORTED_HEADERS, *NAME_WRAPPERS)]
    files += [Path(UPSTREAM_HEADER)]
    return {str(path).replace('\\', '/'): hashlib.sha256((root / path).read_bytes()).hexdigest() for path in files}


def evidence(root: Path = ROOT) -> dict:
    return dict(schema='sheng-keyboard-v1', source_sha256=source_hashes(root), stage_argument=STAGE_ARGUMENT,
                stages={'1': 'read-only snapshot of the rails, pins and clocks', '2': 'QUP2 clock votes, I2C pins, engine identity',
                        '3': 'reset and sleep lines, engine setup, boot ROM id', '4': 'RAM image, boot, frames, attach, keep-awake',
                        '5': 'boot keyboard HID device'},
                options=dict(rails=RAILS_ARGUMENT, khz=KHZ_ARGUMENT, poll_read=POLL_READ_ARGUMENT),
                default_off=True, hardware_only=True, vm_refused=True, firmware_compiled_in=False, storage_writes=False,
                rail_votes_default_off=True, authentication_answered=False, imported_commit=IMPORTED_COMMIT,
                firmware=dict(source=f'/arm-io/{NODE} firmware property (local package only)', bytes=FIRMWARE_SIZE, sha256=FIRMWARE_SHA256),
                vendor_id=VENDOR_ID, product_id=PRODUCT_ID, hardware_qualified=False)


def validate(record: dict, root: Path = ROOT) -> None:
    if (record.get('schema') != 'sheng-keyboard-v1' or record.get('stage_argument') != STAGE_ARGUMENT
            or record.get('default_off') is not True or record.get('hardware_only') is not True
            or record.get('vm_refused') is not True or record.get('firmware_compiled_in') is not False
            or record.get('storage_writes') is not False or record.get('rail_votes_default_off') is not True
            or record.get('source_sha256') != source_hashes(root)):
        raise ValueError('keyboard companion differs from the reviewed default-off contract')


def checked_firmware(data: bytes) -> bytes:
    if len(data) != FIRMWARE_SIZE or hashlib.sha256(data).hexdigest() != FIRMWARE_SHA256:
        raise ValueError('MCU_Upgrade.bin differs from the pinned controller image')
    if data[0x7fc0:0x7fc7] != b'NANO IC':
        raise ValueError('MCU_Upgrade.bin has no controller image header')
    return data


def node_properties(firmware: bytes) -> dict:
    """Properties of /arm-io/native-keyboard (all values are bytes)."""
    checked_firmware(firmware)
    return {'compatible': b'sheng,wn8030\0', 'contract': b'sheng-keyboard-v1\0', 'firmware': firmware,
            'firmware-sha256': bytes.fromhex(FIRMWARE_SHA256)}


def boot_arguments(stage: int, *, rails: bool = False, khz: int | None = None, poll_read_ms: int | None = None) -> list:
    if stage not in range(1, 6):
        raise ValueError('keyboard stage must be 1..5')
    arguments = [f'{STAGE_ARGUMENT}={stage}']
    if rails:
        arguments.append(RAILS_ARGUMENT)
    if khz is not None:
        if khz not in (100, 400, 1000):
            raise ValueError('keyboard bus speed must be 100, 400 or 1000 kHz')
        arguments.append(f'{KHZ_ARGUMENT}={khz}')
    if poll_read_ms is not None:
        if not 10 <= poll_read_ms <= 1000:
            raise ValueError('keyboard frame polling must be 10..1000 ms')
        arguments.append(f'{POLL_READ_ARGUMENT}={poll_read_ms}')
    return arguments


def compile_plan(command: list, object_path, wrapper, sdk: Path, keyboard_dir: Path, root: Path = ROOT) -> list:
    """(label, argv, object path) for each translation unit, from the storage driver's own compile command."""
    base = list(command)
    user_headers = base.index(str(sdk / 'usr/include'))
    if base[user_headers - 1] != '-I':
        raise ValueError('unexpected user SDK include option')
    del base[user_headers - 1:user_headers + 1]
    base[1:1] = ['-I', str(keyboard_dir / 'include'), '-I', str(keyboard_dir)]
    base += ['-I', str(sdk / 'System/Library/Frameworks/Kernel.framework/Versions/A/Headers'), '-I', str((root / UPSTREAM_HEADER).parent)]

    def one(source, output):
        return [str(output) if arg == str(object_path) else str(source) if arg == str(wrapper) else arg for arg in base]

    target = keyboard_dir / 'NativeKeyboard.o'
    plan = [('keyboard-NativeKeyboard', one(keyboard_dir / 'native_keyboard.cpp', target), target)]
    for name in COMPILED_C:
        target = keyboard_dir / (Path(name).stem + '.o')
        argv = ['-xc' if arg == '-xc++' else '-std=gnu17' if arg.startswith('-std=gnu++') else arg
                for arg in one(keyboard_dir / name, target)
                if arg not in ('--driver-mode=g++', '-fsized-deallocation', '-fapple-kext', '-fc++-static-destructors=none')]
        plan.append(('keyboard-' + Path(name).stem, argv + ['-Wno-unused-command-line-argument'], target))
    return plan
