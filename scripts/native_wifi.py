# SPDX-License-Identifier: GPL-3.0-or-later
"""Contract of the default-off Wi-Fi companion (platform/sheng/macos26/net/native_wifi.cpp).

Stage 1 (sheng-wifi=1) is read-only: upstream UniXNU-sheng package/WifiProbe.cpp at the pinned commit compiled unchanged, plus the WCN7850
power inputs through the SPMI observer. Stages 2 and 3 are the upstream power-up and link training, rewritten over an abstract register
interface (wcn_bringup.h, wcn_rpmh.h and wcn_pcie_tables.h are BSD-2-Clause-Patent from UniXNU-sheng at UPSTREAM_DERIVED_FROM). No firmware,
no DMA and no network interface exist yet. One definition shared by the storage builder, the storage-module audit and the packager.
"""
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLATFORM_DIR = 'platform/sheng/macos26/net'
PLATFORM_SOURCES = ('native_wifi.cpp', 'wcn_state.h', 'wcn_bringup.h', 'wcn_rpmh.h', 'wcn_pcie_tables.h')
INPUT_DIR = 'platform/sheng/macos26/input'
INPUT_SOURCES = ('spmi_keys.h',)
UPSTREAM_CPP = 'UniXNU-sheng/package/WifiProbe.cpp'
UPSTREAM_DERIVED_FROM = 'a68ee4ee67cc71792fbf519de7e3e28072c717b5'
STAGE_ARGUMENT = 'sheng-wifi'
STAGES = {'1': 'read-only snapshot of PCIe0, WCN power inputs and Wi-Fi and Bluetooth pins',
          '2': 'upstream power-up: sleep clock pin, six RPMh rails, WLAN_EN, PCIe0 clocks and PHY, link, endpoint ID',
          '3': 'stage 2, then BAR0 and a read of the MHI registers (no firmware, no DMA)'}
PERSONALITY = dict(IOClass='NativeWifi', IOProviderClass='IOPlatformExpertDevice',
                   IOMatchCategory='NativeWifi', IOProbeScore=500)


def source_hashes(root: Path = ROOT) -> dict:
    files = [Path(PLATFORM_DIR) / name for name in PLATFORM_SOURCES] + \
            [Path(INPUT_DIR) / name for name in INPUT_SOURCES] + [Path(UPSTREAM_CPP)]
    return {str(path).replace('\\', '/'): hashlib.sha256((root / path).read_bytes()).hexdigest() for path in files}


def evidence(root: Path = ROOT) -> dict:
    return dict(schema='sheng-wifi-v1', source_sha256=source_hashes(root), stage_argument=STAGE_ARGUMENT, stages=STAGES,
                default_off=True, hardware_only=True, vm_refused=True, read_only_stage=1, firmware_compiled_in=False,
                storage_writes=False, upstream_derived_from=UPSTREAM_DERIVED_FROM, hardware_qualified=False)


def validate(record: dict, root: Path = ROOT) -> None:
    if (record.get('schema') != 'sheng-wifi-v1' or record.get('stage_argument') != STAGE_ARGUMENT
            or record.get('default_off') is not True or record.get('hardware_only') is not True
            or record.get('vm_refused') is not True or record.get('read_only_stage') != 1
            or record.get('firmware_compiled_in') is not False or record.get('storage_writes') is not False
            or record.get('source_sha256') != source_hashes(root)):
        raise ValueError('Wi-Fi companion differs from the reviewed default-off contract')
