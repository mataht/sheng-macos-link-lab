# SPDX-License-Identifier: GPL-3.0-or-later
"""Contract of the default-off panel brightness companion (platform/sheng/macos26/power/native_backlight.cpp).

The KTZ8866 register logic in ktz8866_i2c.h is derived from upstream UniXNU-sheng package/AppleARMBacklight.cpp. Stage 1 only
reads MMIO, stage 2 adds I2C reads, stages 3 and 4 may change the brightness and always clamp it to the visible range. One
definition shared by the storage builder, the storage-module audit and the packager.
"""
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLATFORM_DIR = 'platform/sheng/macos26/power'
PLATFORM_SOURCES = ('native_backlight.cpp', 'ktz8866_i2c.h')
STAGE_ARGUMENT = 'sheng-backlight'
LEVEL_ARGUMENT = 'sheng-backlight-level'
MAX_LEVEL = 2047
MIN_VISIBLE_LEVEL = 64
STAGES = {'1': 'read-only MMIO snapshot, no I2C traffic',
          '2': 'clock votes if needed and I2C reads of both chips',
          '3': 'also write the boot-argument level once, clamped',
          '4': 'also accept brightness from macOS through setProperties, clamped'}
PERSONALITY = dict(IOClass='NativeBacklight', IOProviderClass='IOPlatformExpertDevice',
                   IOMatchCategory='NativeBacklight', IOProbeScore=500)


def source_hashes(root: Path = ROOT) -> dict:
    files = [Path(PLATFORM_DIR) / name for name in PLATFORM_SOURCES]
    return {str(path).replace('\\', '/'): hashlib.sha256((root / path).read_bytes()).hexdigest() for path in files}


def evidence(root: Path = ROOT) -> dict:
    return dict(schema='sheng-backlight-v1', source_sha256=source_hashes(root), stage_argument=STAGE_ARGUMENT, stages=STAGES,
                level_argument=LEVEL_ARGUMENT, max_level=MAX_LEVEL, min_visible_level=MIN_VISIBLE_LEVEL, default_off=True,
                hardware_only=True, vm_refused=True, read_only_stage=1, storage_writes=False, hardware_qualified=False)


def validate(record: dict, root: Path = ROOT) -> None:
    if (record.get('schema') != 'sheng-backlight-v1' or record.get('stage_argument') != STAGE_ARGUMENT
            or record.get('default_off') is not True or record.get('hardware_only') is not True
            or record.get('vm_refused') is not True or record.get('read_only_stage') != 1
            or record.get('storage_writes') is not False or record.get('min_visible_level') != MIN_VISIBLE_LEVEL
            or record.get('source_sha256') != source_hashes(root)):
        raise ValueError('backlight companion differs from the reviewed default-off contract')


def boot_arguments(stage, level=None) -> str:
    """Boot arguments for a packaged stage. A level is refused unless the stage can use it, and must be in the visible range."""
    if level is not None:
        if stage < 3:
            raise ValueError(f'{LEVEL_ARGUMENT} needs stage 3 or 4')
        if not MIN_VISIBLE_LEVEL <= level <= MAX_LEVEL:
            raise ValueError(f'{LEVEL_ARGUMENT} must be within {MIN_VISIBLE_LEVEL}..{MAX_LEVEL}')
        return f'{STAGE_ARGUMENT}={stage} {LEVEL_ARGUMENT}={level}'
    return f'{STAGE_ARGUMENT}={stage}'
