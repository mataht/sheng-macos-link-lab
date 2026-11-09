# SPDX-License-Identifier: GPL-3.0-or-later
"""Contract and build inputs of the default-off Adreno 740 kernel stack (platform/sheng/macos26/gpu).

The driver is upstream UniXNU code: Adreno740, the AdrenoPM4, AdrenoFirmware and ArmSmmu4K kits, the SM8550 common kit and the
sheng board's GpuPowerProbe.cpp, patched for a 16 KiB macOS kernel. The macOS front end adds an IOAccelerator subclass, the
start-up gate and firmware loading. Board headers are generated from pinned public Linux sources and the device tree, the same
way upstream does it; no firmware is fetched. Vendor firmware reaches the kernel only through a local package's device tree.
One definition shared by the storage builder, the storage-module audit and the packager.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
PLATFORM_DIR = 'platform/sheng/macos26/gpu'
FRONT_END = 'native_adreno_macos.cpp'
INPUTS = 'config/adreno-inputs.json'
ADRENO = 'gpu/Adreno740'
PM4 = 'gpu/AdrenoPM4'
FIRMWARE_KIT = 'gpu/AdrenoFirmware'
SMMU = 'gpu/ArmSmmu4K'
SOC = 'gpu/UniXNU-sm8550-common/kits/Sm8550Platform'
BOARD = 'gpu/UniXNU-sheng/package'
BOARD_SCRIPTS = 'gpu/UniXNU-sheng/scripts'
GENERATORS = ('fetch-sheng-dts.py', 'prepare-sheng-gmu.py', 'prepare-sheng-gmu-votes.py', 'prepare-sheng-gpu-smmu.py')
PATCHES = ('patches/Adreno740/0001-macos-16k-pages-and-window-server.patch',
           'patches/UniXNU-sheng-gpu/0001-macos-gpu-board-probe.patch')
# Hardware services for ADT nodes we do not create, a test client, the iPadOS accelerator and the weak empty firmware.
EXCLUDED_CPP = frozenset(('NativeAdrenoClient.cpp', 'NativeAdrenoService.cpp', 'NativeAdrenoGmuService.cpp',
                          'NativeAdrenoSmmuService.cpp', 'NativeAdrenoAccelerator.cpp', 'NativeAdrenoFirmwareDefaults.cpp'))
BOARD_CPP = 'GpuPowerProbe.cpp'
SOC_C = ('ShengGmu.c', 'ShengGmuCommandDb.c')
BOARD_C = ('ShengGmuVotes.c',)
FIRMWARE_KIT_C = ('AdrenoHfi.c', 'AdrenoHfiStartup.c', 'AdrenoHfiVotes.c', 'AdrenoFirmware.c')
PM4_C = ('AdrenoPM4.c',)
MODE_ARGUMENT = 'sheng-gpu-power'
IOACCELERATOR_SLOTS = 272
IOGRAPHICS_BUNDLE = 'com.apple.iokit.IOGraphicsFamily'
IOGRAPHICS_COMPATIBLE = '1.0.0b1'
START_PERSONALITY = dict(IOClass='NativeGpuStart', IOProviderClass='IOPlatformExpertDevice',
                         IOMatchCategory='NativeGpuStart', IOProbeScore=500)
ACCELERATOR_PERSONALITY = dict(IOClass='AGXAccelerator', IOProviderClass='NativeAdrenoGpuPublished',
                               IOMatchCategory='IOAccelerator', IOProbeScore=1000,
                               MetalPluginName='NativeAdrenoMetal', MetalPluginClassName='NativeAdrenoMetalDevice')
# (ADT property, upstream file): the pins come from config/adreno-inputs.json.
FIRMWARE = (('sqe-firmware', 'qcom/a740_sqe.fw'), ('gmu-firmware', 'qcom/gmu_gen70200.bin'),
            ('zap-firmware', 'qcom/sm8550/sheng/a740_zap.mbn'))
NODE_NAME = 'native-gpu'
LINUX_RAW = 'https://raw.githubusercontent.com/ianchb/sm8550-mainline/{commit}/{name}'


def inputs(root: Path = ROOT) -> dict:
    return json.loads((root / INPUTS).read_text())


def firmware_pins(root: Path = ROOT) -> list:
    files = inputs(root)['firmware']['files']
    return [dict(property=name, source=file, bytes=files[file]['bytes'], sha256=files[file]['sha256']) for name, file in FIRMWARE]


def pins_header(root: Path = ROOT) -> str:
    rows = []
    for pin in firmware_pins(root):
        digest = ','.join('0x' + pin['sha256'][i:i + 2] for i in range(0, 64, 2))
        rows.append(f'    {{"{pin["property"]}", {pin["bytes"]}u, {{{digest}}}}},')
    return ('#include <stdint.h>\n'
            'struct NativeGpuFirmwarePin {const char *property; uint32_t bytes; uint8_t sha256[32];};\n'
            'static const NativeGpuFirmwarePin kNativeGpuFirmwarePins[3] = {\n' + '\n'.join(rows) + '\n};\n')


def cpp_sources(root: Path = ROOT) -> list:
    sources = sorted(p for p in (root / ADRENO).glob('NativeAdreno*.cpp') if p.name not in EXCLUDED_CPP)
    return sources + [root / BOARD / BOARD_CPP]


def c_sources(root: Path = ROOT) -> list:
    return ([root / SOC / name for name in SOC_C] + [root / BOARD / name for name in BOARD_C]
            + [root / FIRMWARE_KIT / name for name in FIRMWARE_KIT_C]
            + sorted((root / SMMU).glob('ArmSmmu4K*.c')) + [root / PM4 / name for name in PM4_C])


def include_directories(root: Path, generated: Path) -> list:
    return [root / ADRENO, root / BOARD, root / SOC, root / FIRMWARE_KIT, root / SMMU, root / PM4,
            generated / 'build/sheng-gmu', generated / 'build/sheng-gpu-smmu']


def build_identifier(root: Path = ROOT) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in (root / ADRENO).glob('Native*') if p.is_file()):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def fetch_linux_sources(tree: Path, root: Path = ROOT) -> None:
    """Only the pinned public Linux files the generators read; the firmware entries of the same pin file are not fetched."""
    linux = inputs(root)['linux']
    for name, expected in linux['files'].items():
        path = tree / '.cache/adreno-source' / name
        if not path.is_file():
            url = LINUX_RAW.format(commit=linux['commit'], name=name)
            for attempt in range(4):
                try:
                    with urllib.request.urlopen(url, timeout=60) as response:
                        data = response.read()
                    break
                except OSError:
                    if attempt == 3:
                        raise
                    time.sleep(5 * (attempt + 1))
            if hashlib.sha256(data).hexdigest() != expected['sha256'] or len(data) != expected['bytes']:
                raise ValueError('pinned Linux source differs: ' + name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)


def generate_headers(root: Path, directory: Path, name: str, run, record: dict, env) -> Path:
    """Lay out the tree upstream's generators expect and run them. Returns the tree holding build/sheng-gmu and build/sheng-gpu-smmu."""
    tree = directory / 'tree'
    (tree / 'scripts/data').mkdir(parents=True, exist_ok=True)
    (tree / 'kits').mkdir(exist_ok=True)
    shutil.copyfile(root / INPUTS, tree / 'scripts/data/adreno-inputs.json')
    for script in GENERATORS:
        shutil.copyfile(root / BOARD_SCRIPTS / script, tree / 'scripts' / script)
    link = tree / 'kits/sheng'
    if not link.is_symlink():
        os.symlink((root / BOARD).resolve(), link)
    fetch_linux_sources(tree, root)
    for script in GENERATORS:
        run([sys.executable, tree / 'scripts' / script], name + '-adreno-' + Path(script).stem, record, cwd=tree, env=env)
    for header in ('build/sheng-gmu/ShengGmuSource.h', 'build/sheng-gmu/ShengGmuVotesSource.h',
                   'build/sheng-gmu/ShengGmuCommandDbSource.h', 'build/sheng-gpu-smmu/ShengGpuSmmuSource.h'):
        if not (tree / header).is_file():
            raise ValueError('generator produced no ' + header)
    return tree


def source_hashes(root: Path = ROOT) -> dict:
    files = [Path(PLATFORM_DIR) / FRONT_END, Path(INPUTS), *map(Path, PATCHES)]
    return {str(path).replace('\\', '/'): hashlib.sha256((root / path).read_bytes()).hexdigest() for path in files}


def evidence(root: Path = ROOT) -> dict:
    return dict(schema='sheng-adreno-v1', source_sha256=source_hashes(root), mode_argument=MODE_ARGUMENT, default_off=True,
                hardware_only=True, vm_refused=True, firmware_compiled_in=False, hardware_qualified=False,
                firmware=firmware_pins(root), node=NODE_NAME, build_identifier=build_identifier(root),
                accelerator_class=ACCELERATOR_PERSONALITY['IOClass'])


def validate(record: dict, root: Path = ROOT) -> None:
    if (record.get('schema') != 'sheng-adreno-v1' or record.get('mode_argument') != MODE_ARGUMENT
            or record.get('default_off') is not True or record.get('hardware_only') is not True
            or record.get('vm_refused') is not True or record.get('firmware_compiled_in') is not False
            or record.get('hardware_qualified') is not False or record.get('firmware') != firmware_pins(root)
            or record.get('source_sha256') != source_hashes(root)):
        raise ValueError('Adreno stack differs from the reviewed default-off contract')


def checked_firmware(directory: Path, root: Path = ROOT) -> dict:
    """Read the three local firmware files, refusing any that differ from the pins. Returns ADT properties (local packages only)."""
    properties = {'compatible': b'sheng,adreno740\0', 'contract': b'sheng-adreno-v1\0'}
    for pin in firmware_pins(root):
        data = (Path(directory) / pin['source']).read_bytes()
        if len(data) != pin['bytes'] or hashlib.sha256(data).hexdigest() != pin['sha256']:
            raise ValueError(pin['source'] + ' differs from the pinned firmware')
        properties[pin['property']] = data
    return properties
