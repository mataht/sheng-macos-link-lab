#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Build Turnip (Mesa's Vulkan driver for Adreno) with the Darwin kernel backend as a macOS arm64e library.

Inputs are public and pinned: Mesa, the Mesa patches UniXNU records, Adreno740's ABI header and our own 16 KiB page patch. The result is
an ad-hoc signed libvulkan_freedreno.dylib with its CDHash recorded, for the Metal plugin to load beside itself. This only proves the
library builds. No GPU, device or kernel service is exercised, and no Apple or vendor content goes into the sources or the output.

    build_turnip_macos.py [--prepare-only] [--no-own-patches] [--mesa DIR] [--unixnu DIR] [--adreno DIR] [--out DIR] [-j N]
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
LOCK = 'config/turnip-sources.json'
OWN_PATCHES = ('platform/sheng/macos26/gpu/mesa/0001-turnip-darwin-16k-pages.patch',)
TARGET = 'arm64e-apple-macos26.5'
ABI_VERSION = 2
LIBRARY = 'libvulkan_freedreno.dylib'
IDENTIFIER = 'org.sheng.turnip'
SCHEMA = 'sheng-turnip-v1'
REQUIRED_EXPORTS = ('_vk_icdGetInstanceProcAddr', '_vkGetInstanceProcAddr')
SYSTEM_PREFIXES = ('/usr/lib/', '/System/Library/')


def fail(message):
    raise SystemExit('build_turnip_macos: ' + message)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def pins(root=ROOT):
    """The locked sources by checkout name."""
    entries = json.loads((Path(root) / LOCK).read_text())['sources']
    return {Path(entry['path']).name: entry for entry in entries}


def normalized_patch(text):
    """git apply needs 'new file mode' on a git-style diff that creates a file; one recorded upstream patch leaves it out."""
    return re.sub(r'^(diff --git a/(\S+) b/\2\n)(--- /dev/null\n)', r'\1new file mode 100644\n\3', text, flags=re.M)


def git(directory, *arguments):
    return subprocess.run(['git', '-C', str(directory), *arguments], check=True, capture_output=True).stdout


def apply_patch(mesa, name, data):
    """Apply one patch, checking first that all of it applies."""
    with tempfile.TemporaryDirectory(prefix='turnip-patch-') as directory:
        path = Path(directory) / 'change.patch'
        path.write_bytes(normalized_patch(data.decode()).encode())
        for arguments in (['--check'], []):
            result = subprocess.run(['git', '-C', str(mesa), 'apply', *arguments, str(path)], capture_output=True, text=True)
            if result.returncode:
                fail(f'{name} does not apply:\n{result.stderr[:2000]}')
    return dict(path=name, sha256=sha256(data))


def abi_version(adreno):
    found = re.search(r'NativeAdrenoAbiVersion\s*=\s*(\d+)', (Path(adreno) / 'NativeAdrenoABI.h').read_text())
    return int(found.group(1)) if found else None


def prepare(mesa, unixnu, adreno, root=ROOT, own_patches=True):
    """Check every pin, then apply upstream's patches in their recorded order and then ours. Returns what was applied."""
    locked = pins(root)
    inputs = json.loads((Path(unixnu) / 'scripts/data/adreno-compiler-inputs.json').read_text())
    head = git(mesa, 'rev-parse', 'HEAD').decode().strip()
    tree = git(mesa, 'rev-parse', 'HEAD^{tree}').decode().strip()
    if not head == inputs['mesa']['commit'] == locked['mesa']['commit']:
        fail(f'Mesa is at {head}, not the pinned commit')
    if tree != inputs['mesa']['tree']:
        fail(f'Mesa tree {tree} differs from the recorded tree')
    if git(mesa, 'status', '--porcelain').strip():
        fail('refusing a Mesa checkout that already has changes')
    if abi_version(adreno) != ABI_VERSION:
        fail(f'Adreno740 ABI version {abi_version(adreno)} is not {ABI_VERSION}')
    applied = [apply_patch(mesa, name, (Path(unixnu) / name).read_bytes()) for name in inputs['patches']]
    own = []
    if own_patches:
        own = [apply_patch(mesa, name, (Path(root) / name).read_bytes()) for name in OWN_PATCHES]
    diff = git(mesa, 'diff', 'HEAD', '--binary')
    extras = {name.decode(): sha256((Path(mesa) / name.decode()).read_bytes())
              for name in git(mesa, 'ls-files', '--others', '--exclude-standard', '-z').split(b'\0') if name}
    return dict(mesa_commit=head, mesa_tree=tree, upstream_patches=applied, own_patches=own, source_diff_sha256=sha256(diff), extra_sources=extras)


def cross_file_text(tools, flags):
    """A Meson cross file: arm64e is a different ABI from the runner's arm64, so nothing built for the target may run on the build machine."""
    return ("[binaries]\nc = %r\ncpp = %r\nobjc = %r\nar = %r\nstrip = %r\npkg-config = '/usr/bin/false'\n"
            % (tools['clang'], tools['clang++'], tools['clang'], tools['ar'], tools['strip']) +
            "[host_machine]\nsystem = 'darwin'\ncpu_family = 'aarch64'\ncpu = 'arm64'\nendian = 'little'\n"
            "[properties]\nneeds_exe_wrapper = true\n[built-in options]\n" +
            ''.join('%s = %r\n' % (key, flags) for key in ('c_args', 'cpp_args', 'objc_args', 'c_link_args', 'cpp_link_args', 'objc_link_args')))


def meson_options(adreno, source_id, build_id):
    """UniXNU's recorded options for its arm64e target, built for the Darwin backend only."""
    return ['-Dgallium-drivers=', '-Dvulkan-drivers=freedreno', '-Dfreedreno-kmds=darwin', f'-Dtu-darwin-abi-dir={adreno}', '-Dplatforms=macos',
            '-Dglx=disabled', '-Degl=disabled', '-Dgbm=disabled', '-Dopengl=false', '-Dgles1=disabled', '-Dgles2=disabled', '-Dllvm=disabled',
            '-Dshared-glapi=disabled', '-Dxmlconfig=disabled', '-Dshader-cache=enabled', '-Dtools=', '-Dbuild-tests=false', '-Dvideo-codecs=',
            '-Dvulkan-layers=', '-Dlibunwind=disabled', '-Dlmsensors=disabled', '-Dvalgrind=disabled', '-Dexpat=disabled', '-Dzstd=disabled',
            '-Dbuildtype=release', '-Db_ndebug=true', f'-Dtu-builtin-source-id={source_id}', '-Dtu-builtin-pack-header=', f'-Dtu-build-id={build_id}']


def identities(sources, compiler):
    """The two identities upstream's options need: 64 and 40 hex digits, bound to the exact source and compiler."""
    profile = dict(mesa_revision=sources['mesa_commit'], target=TARGET, source_diff_sha256=sources['source_diff_sha256'],
                   extra_sources=sources['extra_sources'], compiler=compiler)
    source_id = sha256(json.dumps(profile, sort_keys=True, separators=(',', ':')).encode())
    build_id = sha256(json.dumps({key: profile[key] for key in ('mesa_revision', 'source_diff_sha256', 'extra_sources')}, sort_keys=True).encode())[:40]
    return source_id, build_id


def is_arm64e(data):
    """A thin arm64e Mach-O: 64-bit magic, CPU_TYPE_ARM64, subtype 2."""
    if len(data) < 12:
        return False
    magic, cpu_type, cpu_subtype = struct.unpack_from('<III', data)
    return magic == 0xfeedfacf and cpu_type == 0x0100000c and cpu_subtype & 0xff == 2


def foreign_libraries(otool_text, install_name):
    """Linked libraries outside the system (a Homebrew path would never exist on the target)."""
    names = [line.strip().split(' ')[0] for line in otool_text.splitlines()[1:] if line.strip()]
    return [name for name in names if name != install_name and not name.startswith(SYSTEM_PREFIXES)]


def validate(record):
    """The contract of a build record: proves compilation only, and carries nothing that may not be public."""
    expected = dict(schema=SCHEMA, target=TARGET, abi_version=ABI_VERSION, hardware_qualified=False, runtime_tested=False,
                    apple_content=False, vendor_firmware=False)
    if any(record.get(key) != value for key, value in expected.items()):
        fail('build record breaks the contract')
    if len(record.get('upstream_patches', [])) != 17 or len(record.get('own_patches', [])) != len(OWN_PATCHES):
        fail('build record has the wrong patch set')
    if not re.fullmatch('[0-9a-f]{40}', record.get('cdhash', '')) or record.get('foreign_libraries'):
        fail('build record has no CDHash or links a non-system library')


def tool_paths():
    found = {}
    for tool in ('clang', 'clang++', 'ar', 'strip', 'nm', 'otool'):
        found[tool] = subprocess.check_output(['xcrun', '--sdk', 'macosx', '--find', tool], text=True).strip()
    return found


def run_logged(command, log, env):
    with log.open('w') as stream:
        return subprocess.run([str(part) for part in command], env=env, stdout=stream, stderr=subprocess.STDOUT).returncode


def build(args, sources):
    for program in ('meson', 'ninja', 'glslangValidator', 'bison'):
        if not shutil.which(program):
            fail(program + ' is required')
    tools = tool_paths()
    sdk = subprocess.check_output(['xcrun', '--sdk', 'macosx', '--show-sdk-path'], text=True).strip()
    sdk_version = subprocess.check_output(['xcrun', '--sdk', 'macosx', '--show-sdk-version'], text=True).strip()
    compiler = subprocess.check_output([tools['clang'], '--version'], text=True)
    out = Path(args.out).resolve()
    (out / 'logs').mkdir(parents=True)
    source_id, build_id = identities(sources, compiler)
    options = meson_options(Path(args.adreno).resolve(), source_id, build_id)
    cross = out / 'arm64e-macos.cross'
    cross.write_text(cross_file_text(tools, ['-target', TARGET, '-isysroot', sdk, '-fstack-protector-strong']))
    env = {key: value for key, value in os.environ.items() if not key.startswith(('IR3_', 'FD_MESA_', 'NIR_', 'MESA_SHADER_', 'TU_'))}
    env.update(CC=tools['clang'], CXX=tools['clang++'], CFLAGS='', CXXFLAGS='', CPPFLAGS='', LDFLAGS='')
    work = out / 'build'
    configure = out / 'logs/configure.log'
    if run_logged(['meson', 'setup', work, args.mesa, '--wrap-mode=nodownload', '--cross-file', cross, *options], configure, env):
        fail('configure failed\n' + '\n'.join(configure.read_text().splitlines()[-30:]))
    compile_log = out / 'logs/build.log'
    if run_logged(['ninja', '-C', work, f'-j{args.jobs}'], compile_log, env):
        errors = [line for line in compile_log.read_text().splitlines() if ': error:' in line or 'fatal error:' in line or line.startswith('FAILED:')]
        fail('build failed\n' + '\n'.join(errors)[:6000])
    library = out / LIBRARY
    shutil.copyfile(work / 'src/freedreno/vulkan' / LIBRARY, library)
    if not is_arm64e(library.read_bytes()):
        fail('the library is not thin arm64e')
    symbols = subprocess.check_output([tools['nm'], '-gU', str(library)], text=True).split()
    missing = [name for name in REQUIRED_EXPORTS if name not in symbols]
    if missing:
        fail(f'the library does not export {missing}')
    subprocess.run(['/usr/bin/codesign', '--force', '--sign', '-', '--identifier', IDENTIFIER, str(library)], check=True)
    signature = subprocess.run(['/usr/bin/codesign', '-dvvv', str(library)], capture_output=True, text=True, check=True)
    cdhash = re.search(r'^CDHash=([0-9a-f]{40})$', signature.stderr + signature.stdout, re.M)
    if not cdhash:
        fail('codesign reported no CDHash')
    install_name = subprocess.check_output([tools['otool'], '-D', str(library)], text=True).splitlines()[-1].strip()
    linked = subprocess.check_output([tools['otool'], '-L', str(library)], text=True)
    (out / 'logs/otool-L.txt').write_text(linked)
    undefined = subprocess.check_output([tools['nm'], '-u', str(library)], text=True).split()
    data = library.read_bytes()
    record = dict(schema=SCHEMA, built_utc=datetime.now(timezone.utc).isoformat(), target=TARGET, sdk_version=sdk_version, compiler=compiler.splitlines()[0],
                  abi_version=ABI_VERSION, bytes=len(data), sha256=sha256(data), cdhash=cdhash.group(1), identifier=IDENTIFIER, install_name=install_name,
                  foreign_libraries=foreign_libraries(linked, install_name), undefined_symbols=len(undefined), source_id=source_id, build_id=build_id,
                  meson_options=[option.replace(str(Path(args.adreno).resolve()), 'ADRENO740') for option in options], hardware_qualified=False,
                  runtime_tested=False, apple_content=False, vendor_firmware=False, **sources)
    validate(record)
    (out / 'build.json').write_text(json.dumps(record, indent=2) + '\n')
    print(f'PASS: {LIBRARY} {len(data)} bytes, CDHash {cdhash.group(1)}; nothing was run')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--prepare-only', action='store_true', help='check the pins and apply the patches, build nothing')
    parser.add_argument('--no-own-patches', action='store_true', help="apply upstream's patches only (to regenerate our patch)")
    parser.add_argument('--mesa', default=str(ROOT / 'sources/turnip/mesa'))
    parser.add_argument('--unixnu', default=str(ROOT / 'sources/turnip/UniXNU'))
    parser.add_argument('--adreno', default=str(ROOT / 'sources/turnip/Adreno740'))
    parser.add_argument('--out', default=str(ROOT / 'out/turnip-macos'))
    parser.add_argument('-j', '--jobs', type=int, default=os.cpu_count() or 4)
    args = parser.parse_args()
    sources = prepare(args.mesa, args.unixnu, args.adreno, own_patches=not args.no_own_patches)
    print(f"Prepared Mesa {sources['mesa_commit'][:12]}: {len(sources['upstream_patches'])} upstream + {len(sources['own_patches'])} own patches")
    if not args.prepare_only:
        build(args, sources)


if __name__ == '__main__':
    sys.exit(main())
