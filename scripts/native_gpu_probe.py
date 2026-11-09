# SPDX-License-Identifier: GPL-3.0-or-later
"""Contract and plugin build of the QEMU-only GPU trust-path probe (platform/sheng/macos26/gpu).

The probe publishes an IOAccelerator service and installs an empty ad-hoc-signed Metal plugin on the Data volume, to test
whether the original WindowServer can load code we built. It touches no GPU, is off unless sheng-gpu-probe=1, refuses to
start outside a VM, and is linked only into the VirtIO bundle, which never reaches hardware. One definition shared by the
storage builder (sources, personality, evidence), the storage-module audit and the local trust-cache tool.
"""
import hashlib
from pathlib import Path
import plistlib
import struct
import subprocess
import zlib

ROOT = Path(__file__).resolve().parents[1]
PLATFORM_DIR = 'platform/sheng/macos26/gpu'
PLUGIN_SOURCE = 'ShengProbeMetal.m'
DRIVER_SOURCE = 'native_gpu_probe.cpp'
LINK_STUB = 'IOGPU.tbd'
FAKE_CORE = 'native_gpu_fake_core.h'
ABI_HEADER = 'NativeAdrenoABI.h'   # Adreno740 (MIT) unchanged, at the commit in ABI_SOURCE
ABI_SOURCE = dict(repository='https://github.com/UniXNU/Adreno740', commit='6fa7bae2a5b3b523e790eb3e0348af4d98969e4d',
                  sha256='d1c8a27d6e61787073c2c56a779ff4d5c43d86f2d23ac0c0fba0368af8dcdeae')
SOURCES = (DRIVER_SOURCE, PLUGIN_SOURCE, LINK_STUB, FAKE_CORE, ABI_HEADER)
BOOT_ARGUMENT = 'sheng-gpu-probe=1'
FAKE_BOOT_ARGUMENT = 'sheng-gpu-fake=1'
ACCELERATOR_CLASS = 'AGXAccelerator'
PLUGIN_NAME = 'ShengProbeMetal'
PLUGIN_CLASS = 'ShengProbeMetalDevice'
PLUGIN_IDENTIFIER = 'org.sheng.probe.ShengProbeMetal'
DEPLOYMENT = '26.5'
IOACCELERATOR_SLOTS = 272
IOGRAPHICS_BUNDLE = 'com.apple.iokit.IOGraphicsFamily'
IOGRAPHICS_COMPATIBLE = '1.0.0b1'
PERSONALITY = dict(IOClass=ACCELERATOR_CLASS, IOProviderClass='IOPlatformExpertDevice',
                   IOMatchCategory='IOAccelerator', IOProbeScore=500,
                   MetalPluginName=PLUGIN_NAME, MetalPluginClassName=PLUGIN_CLASS)
SHA256_TRUNCATED = 2


REAL_PLUGIN = 'NativeAdrenoMetal'
REAL_TURNIP = 'libvulkan_freedreno.dylib'
DEVICE_TREE_ROOM = 15 << 20   # DarwinBoot's limit is 16 MiB for the whole tree


def real_plugin_files(bundle: Path) -> tuple:
    contents = Path(bundle) / 'Contents'
    return contents / 'Info.plist', contents / 'MacOS' / REAL_PLUGIN, contents / 'MacOS' / REAL_TURNIP


def packed_file(data: bytes) -> bytes:
    """The file's length (64-bit little endian), then its zlib stream, as the driver unpacks it."""
    return struct.pack('<Q', len(data)) + zlib.compress(data, 9)


def real_plugin_node(bundle: Path) -> dict:
    """Device-tree properties that carry a locally built plugin bundle into a QEMU boot (/arm-io/native-gpu-plugin).

    Compressed because DarwinBoot refuses a device tree over 16 MiB (it refused 37.9 MB uncompressed, attempt A-01)."""
    info, executable, turnip = real_plugin_files(bundle)
    node = {'compatible': b'sheng,qemu-gpu-plugin\0', 'info-plist': packed_file(info.read_bytes()),
            'executable': packed_file(executable.read_bytes()), 'turnip': packed_file(turnip.read_bytes())}
    if sum(map(len, node.values())) > DEVICE_TREE_ROOM:
        raise ValueError('the plugin bundle does not fit the device tree limit even compressed')
    return node


def real_plugin_cdhashes(bundle: Path) -> tuple:
    """The CDHashes a trust cache must name for this bundle, as hex."""
    _, executable, turnip = real_plugin_files(bundle)
    return tuple(cdhash(path.read_bytes())[0].hex() for path in (executable, turnip))


def source_hashes(root: Path = ROOT) -> dict:
    return {name: hashlib.sha256((root / PLATFORM_DIR / name).read_bytes()).hexdigest() for name in SOURCES}


def info_plist() -> bytes:
    return plistlib.dumps(dict(CFBundleExecutable=PLUGIN_NAME, CFBundleIdentifier=PLUGIN_IDENTIFIER,
                               CFBundleName=PLUGIN_NAME, CFBundlePackageType='BNDL', CFBundleVersion='1',
                               CFBundleInfoDictionaryVersion='6.0', NSPrincipalClass=PLUGIN_CLASS))


def cdhash(executable: bytes) -> tuple[bytes, int]:
    """(CDHash, hash type) of a thin arm64e Mach-O: the first 20 bytes of the primary CodeDirectory's SHA-256."""
    magic, cpu, subtype, _, count, size = struct.unpack_from('<6I', executable)
    if magic != 0xfeedfacf or cpu != 0x0100000c or subtype & 0xff != 2:
        raise ValueError('requires a thin arm64e Mach-O')
    offset, end = 32, 32 + size
    signature = None
    for _ in range(count):
        command, length = struct.unpack_from('<II', executable, offset)
        if command == 0x1d:
            signature = struct.unpack_from('<II', executable, offset + 8)
        offset += length
        if offset > end:
            raise ValueError('load commands exceed their declared size')
    if signature is None:
        raise ValueError('plugin has no code signature')
    blob_at, blob_size = signature
    superblob, _, slots = struct.unpack_from('>III', executable, blob_at)
    if superblob != 0xfade0cc0 or blob_at + blob_size > len(executable):
        raise ValueError('malformed code signature')
    for index in range(slots):
        kind, relative = struct.unpack_from('>II', executable, blob_at + 12 + index * 8)
        if kind != 0:
            continue
        at = blob_at + relative
        directory, length = struct.unpack_from('>II', executable, at)
        hash_type = executable[at + 37]
        if directory != 0xfade0c02 or hash_type != SHA256_TRUNCATED:
            raise ValueError('expected a SHA-256 CodeDirectory')
        return hashlib.sha256(executable[at:at + length]).digest()[:20], hash_type
    raise ValueError('code signature has no CodeDirectory')


def payload_header(info: bytes, executable: bytes) -> str:
    def array(name: str, data: bytes) -> str:
        lines = [','.join(str(b) for b in data[i:i + 32]) for i in range(0, len(data), 32)]
        return f'static const unsigned char {name}[] = {{\n' + ',\n'.join(lines) + '\n};\n'

    return (f'#define SHENG_GPU_PROBE_NAME "{PLUGIN_NAME}"\n' + array('gShengGpuProbeInfoPlist', info)
            + array('gShengGpuProbeExecutable', executable))


def build_plugin(root: Path, directory: Path, name: str, run, record: dict, env, sdk: Path) -> dict:
    """Compile, ad-hoc sign and describe the plugin, and generate the header that embeds it in the driver."""
    directory.mkdir(exist_ok=True)
    for file in SOURCES:
        (directory / file).write_bytes((root / PLATFORM_DIR / file).read_bytes())
    executable = directory / PLUGIN_NAME
    run(['xcrun', '--sdk', 'macosx', 'clang', '-target', f'arm64e-apple-macos{DEPLOYMENT}', '-isysroot', sdk, '-bundle',
         '-O1', '-Wall', '-Wextra', '-Werror', '-fno-objc-arc', '-fobjc-link-runtime', '-framework', 'Foundation',
         '-framework', 'IOKit', directory / PLUGIN_SOURCE, directory / LINK_STUB, '-o', executable],
        name + '-gpu-probe-plugin', record, env=env)
    run(['/usr/bin/codesign', '--force', '--sign', '-', '--identifier', PLUGIN_IDENTIFIER, executable],
        name + '-gpu-probe-sign', record, env=env)
    output = subprocess.run(['/usr/bin/codesign', '-dvvv', str(executable)], env=env, capture_output=True, text=True, check=True)
    (directory / 'codesign.txt').write_text(output.stderr + output.stdout)
    binary = executable.read_bytes()
    info = info_plist()
    (directory / 'Info.plist').write_bytes(info)
    (directory / 'gpu_probe_payload.h').write_text(payload_header(info, binary))
    digest, hash_type = cdhash(binary)
    return dict(plugin_sha256=hashlib.sha256(binary).hexdigest(), plugin_bytes=len(binary), plugin_cdhash=digest.hex(),
                plugin_cd_hash_type=hash_type, info_plist_sha256=hashlib.sha256(info).hexdigest())


def evidence(root: Path, plugin: dict) -> dict:
    return dict(schema='sheng-gpu-probe-v1', source_sha256=source_hashes(root), boot_argument=BOOT_ARGUMENT,
                default_off=True, qemu_only=True, hardware_admitted=False, gpu_acceleration=False,
                plugin_name=PLUGIN_NAME, plugin_class=PLUGIN_CLASS, **plugin)


def validate(record: dict, root: Path = ROOT) -> None:
    if (record.get('schema') != 'sheng-gpu-probe-v1' or record.get('boot_argument') != BOOT_ARGUMENT
            or record.get('default_off') is not True or record.get('qemu_only') is not True
            or record.get('hardware_admitted') is not False or record.get('gpu_acceleration') is not False
            or record.get('plugin_name') != PLUGIN_NAME or record.get('plugin_class') != PLUGIN_CLASS
            or record.get('plugin_cd_hash_type') != SHA256_TRUNCATED or len(record.get('plugin_cdhash', '')) != 40
            or record.get('source_sha256') != source_hashes(root)):
        raise ValueError('GPU probe differs from the reviewed QEMU-only default-off contract')
