# SPDX-License-Identifier: GPL-3.0-or-later
"""Contract of the default-off USB companion (platform/sheng/macos26/usb/native_usb.cpp).

Stages (boot argument native-usb=N, hardware only):
  1  read-only report of clock and power state; controller registers only when powered and clocked
  2  rails, power domains, clocks and resets

Register work is platform/sheng/macos26/usb/usb_hw.h (host tested); rail votes adapted from UniXNU QcomDwc3 e4efe95c
(BSD-2-Clause-Patent). One definition shared by the storage builder, the storage-module audit and the packager.
"""
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLATFORM_DIR = 'platform/sheng/macos26/usb'
PLATFORM_SOURCES = ('native_usb.cpp', 'usb_hw.h', 'usb_rpmh_names.h', 'QcomRpmh.h', 'QcomRpmh.c', 'native_usb_rpmh.c')
COMPILE_C = ('native_usb_rpmh.c',)
ALSO_COPIED = ('platform/sheng/macos26/input/spmi_keys.h',)
STAGE_ARGUMENT = 'native-usb'
STAGES = {'1': 'read-only report: clock and power state, controller registers only when powered and clocked',
          '2': 'rails, power domains, clocks and resets'}
PERSONALITY = dict(IOClass='NativeUsb', IOProviderClass='IOPlatformExpertDevice',
                   IOMatchCategory='NativeUsb', IOProbeScore=500)
UPSTREAM_SOURCE = dict(repository='https://github.com/UniXNU/QcomDwc3.git', commit='e4efe95c903a737e0f5955e5b50a8fb4d2a7d718',
                       rpmh_repository='https://github.com/UniXNU/UniXNU-sheng.git',
                       rpmh_commit='a68ee4ee67cc71792fbf519de7e3e28072c717b5')


def source_hashes(root: Path = ROOT) -> dict:
    files = [Path(PLATFORM_DIR) / name for name in PLATFORM_SOURCES] + [Path(name) for name in ALSO_COPIED]
    return {str(path).replace('\\', '/'): hashlib.sha256((root / path).read_bytes()).hexdigest() for path in files}


def evidence(root: Path = ROOT) -> dict:
    return dict(schema='sheng-usb-v1', source_sha256=source_hashes(root), stage_argument=STAGE_ARGUMENT, stages=STAGES,
                default_off=True, hardware_only=True, vm_refused=True, storage_writes=False,
                dma=False, apple_usb_stack=False, upstream=UPSTREAM_SOURCE, hardware_qualified=False)


def validate(record: dict, root: Path = ROOT) -> None:
    if (record.get('schema') != 'sheng-usb-v1' or record.get('stage_argument') != STAGE_ARGUMENT
            or record.get('default_off') is not True or record.get('hardware_only') is not True
            or record.get('vm_refused') is not True or record.get('storage_writes') is not False
            or record.get('dma') is not False or record.get('source_sha256') != source_hashes(root)):
        raise ValueError('USB companion differs from the reviewed default-off contract')
