# SPDX-License-Identifier: GPL-3.0-or-later
"""Contract of the default-off hardware key companion (platform/sheng/macos26/input/native_button_keys.cpp).

One definition shared by the storage builder (personalities, evidence), the storage validator and the packager.
"""
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ('native_button_keys.cpp', 'spmi_keys.h')
BOOT_ARGUMENT = 'native-button-keys=1'
VENDOR_ID, PRODUCT_ID = 0x1209, 0x5348

PERSONALITY = dict(IOClass='NativeButtonKeys', IOProviderClass='IOPlatformExpertDevice',
                   IOMatchCategory='NativeButtonKeys', IOProbeScore=500)
EVENT_DRIVER = dict(CFBundleIdentifier='com.apple.iokit.IOHIDFamily', IOClass='IOHIDEventDriver',
                    IOProviderClass='IOHIDInterface', VendorID=VENDOR_ID, ProductID=PRODUCT_ID, IOProbeScore=3000)


def source_hashes(root: Path = ROOT) -> dict:
    return {name: hashlib.sha256((root / 'platform/sheng/macos26/input' / name).read_bytes()).hexdigest() for name in SOURCES}


def evidence(root: Path = ROOT) -> dict:
    return dict(schema='sheng-button-keys-v1', source_sha256=source_hashes(root), boot_argument=BOOT_ARGUMENT,
                default_off=True, hardware_only=True, vm_refused=True, read_only_spmi=True,
                spmi_regions=dict(core='0x0c400000', cnfg='0x0c42d000', obsrvr='0x0c440000', ee=0),
                keys=dict(volume_up='Up arrow (GPIO6 of PM8550, SID 1)', volume_down='Down arrow (PON RESIN, SID 0)',
                          power='Return (PON KPDPWR, SID 0)', both_volume='Tab'),
                vendor_id=VENDOR_ID, product_id=PRODUCT_ID, poll_ms=20, chord_window_ms=80, hardware_qualified=False)


def validate(record: dict, root: Path = ROOT) -> None:
    if (record.get('schema') != 'sheng-button-keys-v1' or record.get('boot_argument') != BOOT_ARGUMENT
            or record.get('default_off') is not True or record.get('hardware_only') is not True
            or record.get('vm_refused') is not True or record.get('read_only_spmi') is not True
            or record.get('source_sha256') != source_hashes(root)):
        raise ValueError('hardware key companion differs from the reviewed default-off read-only contract')
