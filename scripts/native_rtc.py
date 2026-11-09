# SPDX-License-Identifier: GPL-3.0-or-later
"""Contract of the default-off wall clock companion (platform/sheng/macos26/power/native_rtc.cpp).

The macOS front end reads the PMK8550 RTC through the SPMI observer channel under sheng-rtc=<1..2> on hardware. Mode 1 is
read-only and leaves the calendar alone; mode 2 hands a time to the platform expert hook of
platform/sheng/macos26/patches/machine-platform-time.patch and publishes IORTC. Nothing is written to the PMIC. One definition
shared by the storage builder (sources, personality, evidence), the storage-module audit and the packager.
"""
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLATFORM_DIR = 'platform/sheng/macos26/power'
PLATFORM_SOURCES = ('native_rtc.cpp', 'native_rtc.h')
SPMI_HEADER = 'platform/sheng/macos26/input/spmi_keys.h'
MACHINE_PATCH = 'platform/sheng/macos26/patches/machine-platform-time.patch'
MODE_ARGUMENT = 'sheng-rtc'
OFFSET_ARGUMENT = 'sheng-rtc-offset'
FLOOR_ARGUMENT = 'sheng-rtc-floor'
MODES = {'1': 'read the PMIC RTC and log it; the calendar and IORTC are left alone',
         '2': 'additionally give the calendar RTC + offset (or the packaging time) and publish IORTC'}
PERSONALITY = dict(IOClass='NativeRtc', IOProviderClass='IOPlatformExpertDevice',
                   IOMatchCategory='NativeRtc', IOProbeScore=500)


def source_hashes(root: Path = ROOT) -> dict:
    files = [Path(PLATFORM_DIR) / name for name in PLATFORM_SOURCES] + [Path(SPMI_HEADER), Path(MACHINE_PATCH)]
    return {str(path).replace('\\', '/'): hashlib.sha256((root / path).read_bytes()).hexdigest() for path in files}


def evidence(root: Path = ROOT) -> dict:
    return dict(schema='sheng-rtc-v1', source_sha256=source_hashes(root), mode_argument=MODE_ARGUMENT, modes=MODES,
                offset_argument=OFFSET_ARGUMENT, floor_argument=FLOOR_ARGUMENT, default_off=True, hardware_only=True,
                vm_refused=True, read_only_mode=1, pmic_writes=False, storage_writes=False, hardware_qualified=False)


def validate(record: dict, root: Path = ROOT) -> None:
    if (record.get('schema') != 'sheng-rtc-v1' or record.get('mode_argument') != MODE_ARGUMENT
            or record.get('default_off') is not True or record.get('hardware_only') is not True
            or record.get('vm_refused') is not True or record.get('read_only_mode') != 1
            or record.get('pmic_writes') is not False or record.get('storage_writes') is not False
            or record.get('source_sha256') != source_hashes(root)):
        raise ValueError('wall clock companion differs from the reviewed default-off contract')


def boot_arguments(mode, offset=None, floor=None) -> str:
    """The boot arguments for a packaged mode; a value must fit the kernel's 32-bit boot-argument numbers."""
    for name, value in ((OFFSET_ARGUMENT, offset), (FLOOR_ARGUMENT, floor)):
        if value is not None and not -2 ** 31 <= value < 2 ** 31:
            raise ValueError(f'{name} does not fit 32 bits')
    if floor is not None and floor <= 0:
        raise ValueError(f'{FLOOR_ARGUMENT} must be positive')
    parts = [f'{MODE_ARGUMENT}={mode}']
    if offset is not None:
        parts.append(f'{OFFSET_ARGUMENT}={offset}')
    if floor is not None:
        parts.append(f'{FLOOR_ARGUMENT}={floor}')
    return ' '.join(parts)
