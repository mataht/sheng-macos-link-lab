# SPDX-License-Identifier: GPL-3.0-or-later
"""Contract of the default-off CPU frequency companion (platform/sheng/macos26/power/native_cpufreq.cpp).

The register work is upstream UniXNU-sheng package/CpuFreqProbe.cpp (SM8550 EPSS frequency domains and L3), unchanged;
the macOS front end only starts it under sheng-cpufreq=<1..3> on hardware and prints its report row. Mode 1 is
read-only; modes 2 and 3 raise the CPU (and L3) levels and are separate experiments. One definition shared by the
storage builder (sources, personality, evidence), the storage-module audit and the packager.
"""
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLATFORM_DIR = 'platform/sheng/macos26/power'
PLATFORM_SOURCES = ('native_cpufreq.cpp',)
UPSTREAM_CPP = 'UniXNU-sheng/package/CpuFreqProbe.cpp'
MODE_ARGUMENT = 'sheng-cpufreq'
MODES = {'1': 'read-only report of each EPSS domain (enable, current and requested level, highest LUT entry)',
         '2': 'additionally request the highest non-turbo level in domain 0',
         '3': 'the same in every domain once enabled, and the highest L3 level'}
PERSONALITY = dict(IOClass='NativeCpuFreq', IOProviderClass='IOPlatformExpertDevice',
                   IOMatchCategory='NativeCpuFreq', IOProbeScore=500)


def source_hashes(root: Path = ROOT) -> dict:
    files = [Path(PLATFORM_DIR) / name for name in PLATFORM_SOURCES] + [Path(UPSTREAM_CPP)]
    return {str(path).replace('\\', '/'): hashlib.sha256((root / path).read_bytes()).hexdigest() for path in files}


def evidence(root: Path = ROOT) -> dict:
    return dict(schema='sheng-cpufreq-v1', source_sha256=source_hashes(root), mode_argument=MODE_ARGUMENT, modes=MODES,
                default_off=True, hardware_only=True, vm_refused=True, read_only_mode=1, storage_writes=False,
                hardware_qualified=False)


def validate(record: dict, root: Path = ROOT) -> None:
    if (record.get('schema') != 'sheng-cpufreq-v1' or record.get('mode_argument') != MODE_ARGUMENT
            or record.get('default_off') is not True or record.get('hardware_only') is not True
            or record.get('vm_refused') is not True or record.get('read_only_mode') != 1
            or record.get('storage_writes') is not False or record.get('source_sha256') != source_hashes(root)):
        raise ValueError('CPU frequency companion differs from the reviewed default-off contract')
