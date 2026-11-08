# SPDX-License-Identifier: GPL-3.0-or-later
"""Contract of the default-off NT36532E touch companion (platform/sheng/macos26/input/native_touch.cpp).

The controller bring-up is upstream UniXNU-sheng package/TouchProbe.cpp with patches/UniXNU-sheng/0010 (firmware from
the device tree, CN pin configuration) plus the GENI SPI, Novatek protocol and THP touch-core libraries. One definition
shared by the storage builder (sources, personalities, evidence), the device-tree helper and the packager.
The vendor firmware is never part of a build: only a local package carries it in /arm-io/native-touch.
"""
import hashlib
from pathlib import Path
import struct

ROOT = Path(__file__).resolve().parents[1]
PLATFORM_SOURCES = ('native_touch.cpp', 'touch_pointer.h', 'touch_firmware_sha256.h')
UPSTREAM_CPP = 'UniXNU-sheng/package/TouchProbe.cpp'
UPSTREAM_HEADERS = ('UniXNU-sheng/package/TouchBoard.h', 'UniXNU-sheng/package/SmmuBypass.h',
                    'UniXNU-sheng/drivers/NativeNovatekTouch/NvtProtocol.h',
                    'UniXNU-sheng/drivers/NativeNovatekTouch/NvtTouchCore.h', 'UniXNU/drivers/NativeGeniSpi/GeniSpi.h')
UPSTREAM_C = ('UniXNU/drivers/NativeGeniSpi/GeniSpi.c', 'UniXNU-sheng/drivers/NativeNovatekTouch/NvtProtocol.c',
              'UniXNU-sheng/drivers/NativeNovatekTouch/NvtTouchCore.c')
PATCH = 'patches/UniXNU-sheng/0010-touch-firmware-from-device-tree-and-cn-pins.patch'
STAGE_ARGUMENT = 'sheng-touch'
FIRMWARE_SIZE = 245760
FIRMWARE_SHA256 = '750508bb4d75472d35ce5852ec8470d66afa6bf4a4274c136b3759034260ef59'
VENDOR_ID, PRODUCT_ID = 0x1209, 0x5354
PANEL = (3048, 2032)

PERSONALITY = dict(IOClass='NativeTouch', IOProviderClass='IOPlatformExpertDevice',
                   IOMatchCategory='NativeTouch', IOProbeScore=500)
EVENT_DRIVER = dict(CFBundleIdentifier='com.apple.iokit.IOHIDFamily', IOClass='IOHIDEventDriver',
                    IOProviderClass='IOHIDInterface', VendorID=VENDOR_ID, ProductID=PRODUCT_ID, IOProbeScore=3000)


def source_hashes(root: Path = ROOT) -> dict:
    files = [Path('platform/sheng/macos26/input') / name for name in PLATFORM_SOURCES]
    files += [Path(name) for name in (UPSTREAM_CPP, *UPSTREAM_HEADERS, *UPSTREAM_C, PATCH)]
    return {str(path).replace('\\', '/'): hashlib.sha256((root / path).read_bytes()).hexdigest() for path in files}


def evidence(root: Path = ROOT) -> dict:
    return dict(schema='sheng-touch-v1', source_sha256=source_hashes(root), stage_argument=STAGE_ARGUMENT,
                stages={'1': 'read-only clock/pin snapshot', '2': 'QUP clock votes and SPI pins',
                        '3': 'GENI SPI and chip id', '4': 'firmware download, THP frames, pointer'},
                default_off=True, hardware_only=True, vm_refused=True, firmware_compiled_in=False,
                firmware=dict(source='/arm-io/native-touch firmware property (local package only)', bytes=FIRMWARE_SIZE,
                              sha256=FIRMWARE_SHA256),
                pointer=dict(kind='single-touch absolute pointer, finger down = left button', logical_max=32767,
                             panel=list(PANEL), orientation_argument='native-touch-orient'),
                vendor_id=VENDOR_ID, product_id=PRODUCT_ID, storage_writes=False, hardware_qualified=False)


def validate(record: dict, root: Path = ROOT) -> None:
    if (record.get('schema') != 'sheng-touch-v1' or record.get('stage_argument') != STAGE_ARGUMENT
            or record.get('default_off') is not True or record.get('hardware_only') is not True
            or record.get('vm_refused') is not True or record.get('firmware_compiled_in') is not False
            or record.get('storage_writes') is not False or record.get('source_sha256') != source_hashes(root)):
        raise ValueError('touch companion differs from the reviewed default-off contract')


def checked_firmware(data: bytes) -> bytes:
    if len(data) != FIRMWARE_SIZE or hashlib.sha256(data).hexdigest() != FIRMWARE_SHA256:
        raise ValueError('NT36532 firmware differs from the captured device firmware')
    return data


def node_properties(firmware: bytes, *, flip_x=False, flip_y=False) -> dict:
    """Properties of /arm-io/native-touch (all values are bytes)."""
    checked_firmware(firmware)
    return {'compatible': b'sheng,nt36532\0', 'contract': b'sheng-touch-v1\0', 'firmware': firmware,
            'firmware-sha256': bytes.fromhex(FIRMWARE_SHA256), 'landscape-size': struct.pack('<II', *PANEL),
            'flip-x': struct.pack('<I', int(flip_x)), 'flip-y': struct.pack('<I', int(flip_y))}
