#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Contract of the default-off audio DSP and battery companion (platform/sheng/macos26/adsp).

The cores in platform/sheng/macos26/adsp/upstream are unmodified copies from UniXNU-sheng package/ at UPSTREAM_COMMIT; native_adsp.cpp and
native_battery.cpp are macOS front ends derived from AdspProbe.cpp and ShengBattery.cpp. One definition shared by the storage builder
(sources, personality, evidence), the storage-module audit, the device-tree helper and the packager.

The vendor firmware is never part of a build: only a local package carries it, in /arm-io/native-adsp, and only when its sizes and
SHA-256 digests equal a local pin file written by the `join` command below from files read off the tablet.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import sys

ROOT = Path(__file__).resolve().parents[1]
PLATFORM_DIR = 'platform/sheng/macos26/adsp'
UPSTREAM_COMMIT = 'a68ee4ee67cc71792fbf519de7e3e28072c717b5'
FRONT_ENDS = ('native_adsp.cpp', 'native_battery.cpp')
HEADERS = ('adsp_sha256.h',)
UPSTREAM_C = ('QcomGlink.c', 'QcomMdt.c', 'QcomQmp.c', 'QcomQrtr.c', 'QcomRpmh.c', 'QcomSmem.c', 'QcomSmp2p.c', 'ShengBattmgr.c', 'ShengQrtr.c')
UPSTREAM_H = ('QcomGlink.h', 'QcomMdt.h', 'QcomQmp.h', 'QcomQrtr.h', 'QcomRpmh.h', 'QcomScm.h', 'QcomSmem.h', 'QcomSmp2p.h', 'ShengAdsp.h',
              'ShengBattmgr.h')
STAGE_ARGUMENT = 'sheng-adsp'
BATTERY_ARGUMENT = 'sheng-battery'
STAGES = {'1': 'read-only snapshot of SMEM, SMP2P, AOSS and RSC state; no writes',
          '2': 'stage 1 plus image digests, ELF headers, command database lookup, one PAS-is-supported secure call; no state changes',
          '3': 'starts the DSP: load state, RPMh proxy votes, PAS load and reset, GLINK, IPC router'}
BATTERY_STAGES = {'1': 'reads the battery manager and logs the values, nothing published', '2': 'also publishes the IOPMPowerSource'}
PERSONALITY = dict(IOClass='NativeAdsp', IOProviderClass='IOPlatformExpertDevice', IOMatchCategory='NativeAdsp', IOProbeScore=500)
BATTERY_CLASS = 'NativeBattery'
# The class layout and virtual table measured from the 25F71 kernel collection and DWARF of the kernel built from the same sources.
POWER_SOURCE_ABI = dict(base_class='IOPMPowerSource', object_bytes=0x138, vtable_slots=273)
NODE = 'native-adsp'
CONTRACT = 'sheng-adsp-v1'
MAX_IMAGE_BYTES = 128 << 20
# DarwinBoot reads kernel.macho.adt with this limit (drivers/DarwinBoot: ReadFile (Root, TreePath, SIZE_16MB, ...)): a larger file stops
# the boot before the kernel runs.
DARWINBOOT_ADT_LIMIT = 16 << 20
IMAGES = ('adsp', 'adsp_dtb')


def source_paths():
    files = [Path(PLATFORM_DIR) / name for name in (*FRONT_ENDS, *HEADERS)]
    files += [Path(PLATFORM_DIR) / 'upstream' / name for name in (*UPSTREAM_C, *UPSTREAM_H)]
    return files


def source_hashes(root: Path = ROOT) -> dict:
    return {str(path).replace('\\', '/'): hashlib.sha256((root / path).read_bytes()).hexdigest() for path in source_paths()}


def evidence(root: Path = ROOT) -> dict:
    return dict(schema='sheng-adsp-v1', source_sha256=source_hashes(root), upstream_commit=UPSTREAM_COMMIT, stage_argument=STAGE_ARGUMENT,
                stages=STAGES, battery_argument=BATTERY_ARGUMENT, battery_stages=BATTERY_STAGES, power_source_abi=POWER_SOURCE_ABI,
                default_off=True, hardware_only=True, vm_refused=True, read_only_stage=1, firmware_compiled_in=False,
                firmware_source='/arm-io/native-adsp (local package only, sizes and SHA-256 pinned by the packager)',
                charger_writes_default=False, critical_level_asserted=False, storage_writes=False, hardware_qualified=False)


def validate(record: dict, root: Path = ROOT) -> None:
    if (record.get('schema') != 'sheng-adsp-v1' or record.get('stage_argument') != STAGE_ARGUMENT
            or record.get('battery_argument') != BATTERY_ARGUMENT or record.get('default_off') is not True
            or record.get('hardware_only') is not True or record.get('vm_refused') is not True
            or record.get('read_only_stage') != 1 or record.get('firmware_compiled_in') is not False
            or record.get('charger_writes_default') is not False or record.get('critical_level_asserted') is not False
            or record.get('storage_writes') is not False or record.get('source_sha256') != source_hashes(root)):
        raise ValueError('ADSP companion differs from the reviewed default-off contract')


def build_objects(root, destination, command, obj, wrapper, sdk, compile_record, directory, run, record, env, objects) -> None:
    """Compile the companion with the kernel extension's own flags (the two C++ front ends, the C cores)."""
    adsp_dir = destination / 'adsp'
    adsp_dir.mkdir(exist_ok=True)
    for name in (*FRONT_ENDS, *HEADERS):
        (adsp_dir / name).write_bytes((root / PLATFORM_DIR / name).read_bytes())
    upstream_dir = root / PLATFORM_DIR / 'upstream'
    base = list(command)
    user_headers = base.index(str(sdk / 'usr/include'))
    if base[user_headers - 1] != '-I':
        raise ValueError('unexpected user SDK include option')
    del base[user_headers - 1:user_headers + 1]
    base[1:1] = ['-I', str(adsp_dir), '-I', str(upstream_dir)]
    base += ['-I', str(sdk / 'System/Library/Frameworks/Kernel.framework/Versions/A/Headers')]

    def compile_one(source_file, object_file, language):
        one = [str(object_file) if arg == str(obj) else str(source_file) if arg == str(wrapper) else arg for arg in base]
        if language == 'c':
            one = ['-xc' if arg == '-xc++' else '-std=gnu17' if arg.startswith('-std=gnu++') else arg for arg in one
                   if arg not in ('--driver-mode=g++', '-fsized-deallocation', '-fapple-kext', '-fc++-static-destructors=none')]
            one += ['-Wno-unused-command-line-argument']
        run(one, directory + '-adsp-' + object_file.stem + '-compile', record, cwd=Path(compile_record['directory']), env=env)
        objects.append(str(object_file))

    for name, stem in (('native_adsp.cpp', 'NativeAdsp'), ('native_battery.cpp', 'NativeBattery')):
        compile_one(adsp_dir / name, adsp_dir / (stem + '.o'), 'c++')
    for name in UPSTREAM_C:
        compile_one(upstream_dir / name, adsp_dir / (Path(name).stem + '.o'), 'c')


# ---- host side: firmware from the tablet to a local package ----

def _program_headers(headers: bytes):
    """(offset, size) of each program header's segment; elf64 is taken from the ident byte."""
    if len(headers) < 52 or headers[:4] != b'\x7fELF' or headers[5] != 1 or headers[4] not in (1, 2):
        raise ValueError('not a little-endian ELF image')
    elf64 = headers[4] == 2
    if elf64:
        if len(headers) < 64:
            raise ValueError('truncated ELF header')
        phoff, = struct.unpack_from('<Q', headers, 32)
        phentsize, phnum = struct.unpack_from('<HH', headers, 54)
    else:
        phoff, = struct.unpack_from('<I', headers, 28)
        phentsize, phnum = struct.unpack_from('<HH', headers, 42)
    if phentsize != (56 if elf64 else 32) or not 0 < phnum <= 64 or phoff + phentsize * phnum > len(headers):
        raise ValueError('bad program header table')
    result = []
    for i in range(phnum):
        at = phoff + i * phentsize
        if elf64:
            offset, = struct.unpack_from('<Q', headers, at + 8)
            size, = struct.unpack_from('<Q', headers, at + 32)
        else:
            offset, size = struct.unpack_from('<I', headers, at + 4)[0], struct.unpack_from('<I', headers, at + 16)[0]
        result.append((offset, size))
    return result


def join_split_image(directory: Path, stem: str) -> bytes:
    """The joined image of a split NAME.b00, NAME.b01, ... directory: the bytes of program header i come from NAME.bNN at the segment's
    file offset, NAME.b00 being the ELF and program headers; gaps read as zero (the rule upstream's qcom_mdt_split uses on the device)."""
    headers = (directory / (stem + '.b00')).read_bytes()
    segments = _program_headers(headers)
    if segments[0][0] != 0 or segments[0][1] < 52 or segments[0][1] > len(headers):
        raise ValueError(f'{stem}.b00 is not the header piece')
    total = max(offset + size for offset, size in segments if size)
    if total > MAX_IMAGE_BYTES:
        raise ValueError(f'{stem} exceeds the {MAX_IMAGE_BYTES} byte bound')
    image = bytearray(total)
    image[:segments[0][1]] = headers[:segments[0][1]]
    for index, (offset, size) in enumerate(segments[1:], start=1):
        if not size:
            continue
        piece = (directory / f'{stem}.b{index:02d}').read_bytes()
        if len(piece) < size:
            raise ValueError(f'{stem}.b{index:02d} is shorter than its program header says')
        image[offset:offset + size] = piece[:size]
    return bytes(image)


def pin_record(adsp: bytes, dtb: bytes) -> dict:
    return dict(schema=CONTRACT + '-pin',
                adsp=dict(bytes=len(adsp), sha256=hashlib.sha256(adsp).hexdigest()),
                adsp_dtb=dict(bytes=len(dtb), sha256=hashlib.sha256(dtb).hexdigest()))


def checked_firmware(adsp: bytes, dtb: bytes, pin: dict) -> None:
    if pin.get('schema') != CONTRACT + '-pin' or pin_record(adsp, dtb) != pin:
        raise ValueError('ADSP images differ from the local pin (sizes or SHA-256)')


def node_properties(adsp: bytes, dtb: bytes, pin: dict) -> dict:
    """Properties of /arm-io/native-adsp (all values are bytes); property names fit the 31-character ADT limit."""
    checked_firmware(adsp, dtb, pin)
    return {'compatible': b'sheng,adsp-firmware\0', 'contract': CONTRACT.encode() + b'\0', 'firmware': adsp,
            'firmware-sha256': bytes.fromhex(pin['adsp']['sha256']), 'firmware-dtb': dtb,
            'firmware-dtb-sha256': bytes.fromhex(pin['adsp_dtb']['sha256'])}


def check_adt_size(path: Path) -> int:
    """Refuse an ADT the bootloader would reject, instead of producing a package that cannot boot."""
    size = path.stat().st_size
    if size > DARWINBOOT_ADT_LIMIT:
        raise ValueError(f'the ADT with the ADSP images is {size} bytes; DarwinBoot refuses more than {DARWINBOOT_ADT_LIMIT} '
                         '(see hw-build/driver-port/adsp-battery-status.md, firmware delivery)')
    return size


def main() -> int:
    parser = argparse.ArgumentParser(description='Local ADSP firmware handling; nothing here touches a device.')
    sub = parser.add_subparsers(dest='command', required=True)
    join = sub.add_parser('join', help='join NAME.bNN files into adsp.mbn and adsp_dtb.mbn and write the pin')
    join.add_argument('--image-dir', type=Path, required=True, help='directory holding the adsp.b* and adsp_dtb.b* files')
    join.add_argument('--output', type=Path, required=True, help='a local, never committed directory')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    images = {name: join_split_image(args.image_dir, name) for name in IMAGES}
    for name, data in images.items():
        (args.output / (name + '.mbn')).write_bytes(data)
    pin = pin_record(images['adsp'], images['adsp_dtb'])
    (args.output / 'adsp-firmware-pin.json').write_text(json.dumps(pin, indent=2) + '\n')
    print(json.dumps(pin, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
