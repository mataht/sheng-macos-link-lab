# SPDX-License-Identifier: GPL-3.0-or-later
"""Contract of the default-off USB companion (platform/sheng/macos26/usb/native_usb.cpp).

Stages (boot argument native-usb=N, hardware only):
  1  read-only report of clock and power state; controller registers only when powered and clocked
  2  rails, power domains, clocks and resets
  3  eUSB2 repeater and PHY, host role, xHCI reset and port power, port status polling (USB 2, no DMA)
  4  (only with --build-usb-host) macOS 26's own AppleUSBXHCI on the controller; needs the emit --usb-components candidate

The stage 3 PHY table is never part of a build: only a local package carries it, in /arm-io/native-usb, pinned by size and SHA-256.

Register work is platform/sheng/macos26/usb/usb_hw.h (host tested); rail votes adapted from UniXNU QcomDwc3 e4efe95c
(BSD-2-Clause-Patent). One definition shared by the storage builder, the storage-module audit and the packager.
"""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLATFORM_DIR = 'platform/sheng/macos26/usb'
PLATFORM_SOURCES = ('native_usb.cpp', 'usb_hw.h', 'usb_rpmh_names.h', 'QcomRpmh.h', 'QcomRpmh.c', 'native_usb_rpmh.c', 'usb_phy_table_sha256.h')
HOST_SOURCES = ('native_usb_host.cpp', 'OriginalUsbXhciABI.h')
COMPILE_C = ('native_usb_rpmh.c',)
ABI_CONFIG = 'config/macos26-usb-host-slots.json'
MODULES_CONFIG = 'config/macos26-usb-modules.json'
XHCI_BUNDLE = 'com.apple.driver.usb.AppleUSBXHCI'
ALSO_COPIED = ('platform/sheng/macos26/input/spmi_keys.h', 'platform/sheng/macos26/input/touch_firmware_sha256.h')
STAGE_ARGUMENT = 'native-usb'
STAGES = {'1': 'read-only report: clock and power state, controller registers only when powered and clocked',
          '2': 'rails, power domains, clocks and resets',
          '3': 'eUSB2 repeater and PHY, host role, xHCI reset and port power, port status polling (USB 2, no DMA)'}
HOST_STAGE = {'4': "macOS 26's own AppleUSBXHCI on the controller (USB 2, DMA through a stage 1 SMMU identity context)"}
PHY_TABLE_BYTES = 138
PHY_TABLE_SHA256 = '0e7834aaf5bd67ac2777040161106588629dfa8e7449328be9911788862fb51f'
PERSONALITY = dict(IOClass='NativeUsb', IOProviderClass='IOPlatformExpertDevice',
                   IOMatchCategory='NativeUsb', IOProbeScore=500)
UPSTREAM_SOURCE = dict(repository='https://github.com/UniXNU/QcomDwc3.git', commit='e4efe95c903a737e0f5955e5b50a8fb4d2a7d718',
                       rpmh_repository='https://github.com/UniXNU/UniXNU-sheng.git',
                       rpmh_commit='a68ee4ee67cc71792fbf519de7e3e28072c717b5')


def source_hashes(root: Path = ROOT, host: bool = False) -> dict:
    names = list(PLATFORM_SOURCES) + (list(HOST_SOURCES) if host else [])
    files = [Path(PLATFORM_DIR) / name for name in names] + [Path(name) for name in ALSO_COPIED]
    if host:
        files += [Path(ABI_CONFIG), Path(MODULES_CONFIG)]
    return {str(path).replace('\\', '/'): hashlib.sha256((root / path).read_bytes()).hexdigest() for path in files}


def modules_contract(root: Path = ROOT) -> dict:
    contract = json.loads((root / MODULES_CONFIG).read_text())
    if (contract.get('build') != '25F71' or contract.get('xnu') != '12377.121.6' or contract.get('architecture') != 'arm64e'
            or XHCI_BUNDLE not in contract.get('original_modules', ())):
        raise ValueError('USB module contract differs from the paired native kernel')
    return contract


def evidence(root: Path = ROOT, host: bool = False) -> dict:
    return dict(schema='sheng-usb-host-v1' if host else 'sheng-usb-v1', source_sha256=source_hashes(root, host),
                stage_argument=STAGE_ARGUMENT, stages={**STAGES, **(HOST_STAGE if host else {})},
                default_off=True, hardware_only=True, vm_refused=True, storage_writes=False, host=host,
                dma=host, apple_usb_stack=host, upstream=UPSTREAM_SOURCE, phy_table_compiled_in=False,
                phy_table=dict(source='/arm-io/native-usb table property (local package only)', bytes=PHY_TABLE_BYTES,
                               sha256=PHY_TABLE_SHA256),
                hardware_qualified=False)


def validate(record: dict, root: Path = ROOT) -> None:
    host = record.get('host') is True
    if (record.get('schema') != ('sheng-usb-host-v1' if host else 'sheng-usb-v1') or record.get('stage_argument') != STAGE_ARGUMENT
            or record.get('default_off') is not True or record.get('hardware_only') is not True
            or record.get('vm_refused') is not True or record.get('storage_writes') is not False
            or record.get('dma') is not host or record.get('apple_usb_stack') is not host
            or record.get('phy_table_compiled_in') is not False
            or record.get('source_sha256') != source_hashes(root, host)):
        raise ValueError('USB companion differs from the reviewed default-off contract')


def checked_phy_table(data: bytes) -> bytes:
    if len(data) != PHY_TABLE_BYTES or hashlib.sha256(data).hexdigest() != PHY_TABLE_SHA256:
        raise ValueError('USB PHY table differs from the pinned local file')
    return data


def node_properties(table: bytes) -> dict:
    """Properties of /arm-io/native-usb (all values are bytes)."""
    checked_phy_table(table)
    return {'compatible': b'sheng,usb-phy-table\0', 'contract': b'sheng-usb-phy-v1\0', 'table': table,
            'table-sha256': bytes.fromhex(PHY_TABLE_SHA256)}


def personality_policy(entry: dict, bundle: str, contract: dict) -> dict:
    """The boot catalog sees only the USB personalities the host stack needs; IOResources services wait for the stage-4 gate."""
    personalities = entry.get('IOKitPersonalities')
    if not personalities:
        return entry
    if bundle not in contract['keep_personalities'] and bundle not in contract['gated_personalities']:
        entry.pop('IOKitPersonalities', None)
        return entry
    for name in contract['gated_personalities'].get(bundle, []):
        personalities[name]['IOResourceMatch'] = contract['gate_resource']
    return entry
