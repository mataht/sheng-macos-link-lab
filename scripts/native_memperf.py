# SPDX-License-Identifier: GPL-3.0-or-later
"""Contract of the default-off memory performance reader (platform/sheng/macos26/power/native_memperf.cpp).

sheng-memperf=1 reports the L3 vote and table, cmd-db bus resources and the apps RSC TCS commands, and benchmarks its own buffer
through three cache modes. It issues no register writes. One definition shared by the storage builder, the storage-module audit
and the packager.
"""
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLATFORM_DIR = 'platform/sheng/macos26/power'
PLATFORM_SOURCES = ('native_memperf.cpp', 'rpmh_readout.h')
MODE_ARGUMENT = 'sheng-memperf'
MODES = {'1': 'read-only report and a benchmark of the driver\'s own buffer'}
PERSONALITY = dict(IOClass='NativeMemPerf', IOProviderClass='IOPlatformExpertDevice',
                   IOMatchCategory='NativeMemPerf', IOProbeScore=500)


def source_hashes(root: Path = ROOT) -> dict:
    files = [Path(PLATFORM_DIR) / name for name in PLATFORM_SOURCES]
    return {str(path).replace('\\', '/'): hashlib.sha256((root / path).read_bytes()).hexdigest() for path in files}


def evidence(root: Path = ROOT) -> dict:
    return dict(schema='sheng-memperf-v1', source_sha256=source_hashes(root), mode_argument=MODE_ARGUMENT, modes=MODES,
                default_off=True, hardware_only=True, vm_refused=True, read_only_mode=1, register_writes=False,
                storage_writes=False, hardware_qualified=False)


def validate(record: dict, root: Path = ROOT) -> None:
    if (record.get('schema') != 'sheng-memperf-v1' or record.get('mode_argument') != MODE_ARGUMENT
            or record.get('default_off') is not True or record.get('hardware_only') is not True
            or record.get('vm_refused') is not True or record.get('read_only_mode') != 1
            or record.get('register_writes') is not False or record.get('storage_writes') is not False
            or record.get('source_sha256') != source_hashes(root)):
        raise ValueError('memory performance reader differs from the reviewed default-off contract')
