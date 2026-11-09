#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Build the native Metal bridge (AdrenoMetal's C ABI in Rust over the metal2vulkan runtime) as a macOS arm64e static library.

Inputs are public and pinned at the revisions UniXNU records: metal2vulkan, the patches AdrenoMetal keeps for it, and AdrenoMetal itself.
rustc's built-in arm64e-apple-darwin target leaves pointer authentication off, so Rust would call the signed function pointers of the
rest of the process as plain addresses. The target used here is that built-in one, as this rustc prints it, with clang's ABI selected
(pauthtest): signed calls, returns and static function pointers. The standard library is built from source for it.

This only proves the library compiles and links into an arm64e image. Nothing here runs it, and no Apple or vendor content goes into
the sources or the output.

    build_metal_bridge_macos.py [--prepare-only] [--translator DIR] [--adreno-metal DIR] [--unixnu DIR] [--out DIR]
"""
import argparse
from array import array
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
LOCK = 'config/metal-bridge-sources.json'
CARGO_LOCK = 'config/metal-bridge-Cargo.lock'
SCHEMA = 'sheng-metal-bridge-v1'
TOOLCHAIN = 'nightly-2026-11-03'   # metal2vulkan's own rust-toolchain.toml at the pin
TARGET = 'arm64e-apple-darwin'
ABI = 'pauthtest'
DEPLOYMENT = '26.5'
CLANG_TARGET = 'arm64e-apple-macos26.5'
RUSTFLAGS = '-Zpointer-authentication=-init-fini'   # dyld signs Mach-O initializers itself
LIBRARY = 'libadreno_metal_bridge.a'
PATCH_COUNT = 25
MINIMUM_LLVM = 21
LINK_LIBRARIES = ('-framework', 'Foundation', '-framework', 'Metal', '-framework', 'IOSurface', '-framework', 'IOKit', '-framework', 'CoreFoundation',
                  '-lobjc', '-lc++', '-lSystem', '-liconv')
SYSTEM_PREFIXES = ('/usr/lib/', '/System/Library/')
STRIPPED_ENVIRONMENT = ('RUSTFLAGS', 'CARGO_ENCODED_RUSTFLAGS', 'RUSTC_WRAPPER', 'RUSTC_WORKSPACE_WRAPPER', 'CARGO_BUILD_TARGET', 'CARGO_BUILD_RUSTFLAGS')
LICENSE_FILES = dict(translator=('LICENSE', 'LICENSE-GPL-2.0', 'LICENSE-GPL-3.0', 'LICENSE-LGPL-3.0'), adreno_metal=('LICENSE',))

# Instruction encodings counted in the library's code, keyed by the masked word.
WITH_REGISTER = {0xd63f0000: 'blr', 0xd63f081f: 'blraaz', 0xd63f0c1f: 'blrabz', 0xd61f0000: 'br', 0xd61f081f: 'braaz', 0xd61f0c1f: 'brabz',
                 0xd65f0000: 'ret', 0xd65f081f: 'retaa', 0xd65f0c1f: 'retab'}
WITH_MODIFIER = {0xd73f0800: 'blraa', 0xd73f0c00: 'blrab', 0xd71f0800: 'braa', 0xd71f0c00: 'brab'}
WHOLE_WORD = {0xd503233f: 'paciasp', 0xd503237f: 'pacibsp', 0xd50323bf: 'autiasp', 0xd50323ff: 'autibsp'}
DESTINATION = {0xdac123e0: 'paciza', 0xdac133e0: 'autiza'}
INSTRUCTIONS = tuple(WITH_REGISTER.values()) + tuple(WITH_MODIFIER.values()) + tuple(WHOLE_WORD.values()) + tuple(DESTINATION.values())
ARM64_RELOC_AUTHENTICATED_POINTER = 11
CODE_SECTION_FLAGS = 0x80000400


def fail(message):
    raise SystemExit('build_metal_bridge_macos: ' + message)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def pins(root=ROOT):
    """The locked sources by checkout name."""
    entries = json.loads((Path(root) / LOCK).read_text())['sources']
    return {Path(entry['path']).name: entry for entry in entries}


def git(directory, *arguments):
    return subprocess.run(['git', '-C', str(directory), *arguments], check=True, capture_output=True).stdout.decode().strip()


def apply_patch(source, path):
    """Apply one patch, checking first that all of it applies."""
    for arguments in (['--check'], []):
        result = subprocess.run(['git', '-C', str(source), 'apply', *arguments, str(path)], capture_output=True, text=True)
        if result.returncode:
            fail(f'{path.name} does not apply:\n{result.stderr[:2000]}')
    return dict(name=path.name, sha256=sha256(path.read_bytes()))


def working_tree(source):
    """The tree of the working copy, written through a scratch index so the checkout's own index stays as it was."""
    with tempfile.TemporaryDirectory(prefix='bridge-index-') as directory:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(directory) / 'index'))
        for step in (['read-tree', 'HEAD'], ['add', '-A']):
            subprocess.run(['git', '-C', str(source), *step], env=env, check=True, capture_output=True)
        return subprocess.check_output(['git', '-C', str(source), 'write-tree'], env=env, text=True).strip()


def runtime_id(commit, tree):
    """The identity that keys the shader cache, computed as UniXNU's bridge build does: the pin and the patched tree."""
    return sha256(f'{commit}\n{tree}'.encode())[:32]


def prepare(translator, adreno_metal, unixnu, root=ROOT):
    """Check every pin against UniXNU's record, then apply AdrenoMetal's patches in name order, which is UniXNU's order. Returns what was applied."""
    locked = pins(root)
    inputs = json.loads((Path(unixnu) / 'scripts/data/gpu-metal-vulkan-inputs.json').read_text())['translator']
    head = git(translator, 'rev-parse', 'HEAD')
    if not head == inputs['commit'] == locked['metal2vulkan']['commit']:
        fail(f'metal2vulkan is at {head}, not the pinned commit')
    metal_head = git(adreno_metal, 'rev-parse', 'HEAD')
    if not metal_head == git(unixnu, 'ls-tree', 'HEAD', 'drivers/AdrenoMetal').split()[2] == locked['AdrenoMetal']['commit']:
        fail(f'AdrenoMetal is at {metal_head}, not the commit UniXNU pins')
    if git(translator, 'status', '--porcelain'):
        fail('refusing a metal2vulkan checkout that already has changes')
    for name, digest in inputs['files'].items():
        if sha256((Path(translator) / name).read_bytes()) != digest:
            fail(f'metal2vulkan {name} differs from the recorded digest')
    channel = re.search(r'^channel\s*=\s*"([^"]+)"', (Path(translator) / 'rust-toolchain.toml').read_text(encoding='utf-8'), re.M)
    if not channel or channel.group(1) != TOOLCHAIN:
        fail(f'metal2vulkan asks for a toolchain other than {TOOLCHAIN}')
    patches = sorted((Path(adreno_metal) / 'bridge/patches').glob('*.patch'))
    if len(patches) != PATCH_COUNT:
        fail(f'expected {PATCH_COUNT} recorded patches, found {len(patches)}')
    applied = [apply_patch(translator, path) for path in patches]
    tree = working_tree(translator)
    return dict(translator_commit=head, adreno_metal_commit=metal_head, unixnu_commit=git(unixnu, 'rev-parse', 'HEAD'), patches=applied, patched_tree=tree,
                runtime_id=runtime_id(head, tree))


def target_spec(builtin):
    """This rustc's own arm64e-apple-darwin with clang's pointer authentication ABI selected, and nothing else changed."""
    spec = json.loads(builtin)
    spec.pop('is-builtin', None)
    spec.update({'abi': ABI, 'llvm-abiname': ABI})
    return spec


def manifest_text(adreno_metal, translator):
    """The crate that holds the bridge. It is LTO'd as one unit because WindowServer's frame time goes through small runtime functions across the crates."""
    bridge = Path(adreno_metal) / 'bridge/NativeMetalBridge.rs'
    crates = Path(translator) / 'crates'
    return '\n'.join(['[package]', 'name = "adreno-metal-bridge"', 'version = "0.1.0"', 'edition = "2024"', '[workspace]',
                      '[lib]', 'crate-type = ["staticlib"]', 'path = ' + json.dumps(bridge.as_posix()),
                      '[dependencies]', 'metal2vulkan = { path = ' + json.dumps((crates / 'metal2vulkan').as_posix()) + ', features = ["ash"] }',
                      'metal2vulkan-runtime = { path = ' + json.dumps((crates / 'metal2vulkan-runtime').as_posix()) + ' }', 'libc = "0.2"',
                      '[profile.release]', 'panic = "unwind"', 'debug = 1', 'lto = "fat"', 'codegen-units = 1', ''])


def air_inputs(translator, adreno_metal):
    """Environment variable the bridge reads at compile time -> the LLVM assembly it embeds as AIR (public fixtures and AdrenoMetal's scanout kernel)."""
    fixtures = Path(translator) / 'crates/metal2vulkan-validation/fixtures/public'
    return dict(NMB_FIXTURE_AIR=fixtures / 'examples/runtime_examples.ll', NMB_FC_AIR=fixtures / 'kernel_function_constant_gate_fold.ll',
                NMB_SCANOUT_AIR=Path(adreno_metal) / 'shaders/scanout_convert.ll')


def build_env(base, tools, sdk, air, identity):
    """The compile environment. cc-rs would read a Linux triple out of the pauthtest ABI, so its defaults are off and the C flags name the target."""
    env = {key: value for key, value in base.items() if key not in STRIPPED_ENVIRONMENT}
    key = TARGET.replace('-', '_')
    c_flags = f'-target {CLANG_TARGET} -isysroot {sdk} -O2'
    env.update(air)
    env.update(NMB_RUNTIME_ID=identity, RUSTFLAGS=RUSTFLAGS, RUSTUP_TOOLCHAIN=TOOLCHAIN, MACOSX_DEPLOYMENT_TARGET=DEPLOYMENT, SDKROOT=sdk, CRATE_CC_NO_DEFAULTS='1',
               CARGO_TERM_COLOR='never', **{f'CC_{key}': tools['clang'], f'CXX_{key}': tools['clang++'], f'AR_{key}': tools['ar'], f'CFLAGS_{key}': c_flags,
                                            f'CXXFLAGS_{key}': c_flags})
    return env


def archive_members(data):
    """(name, bytes) of every member of a BSD-format static library, leaving out its symbol tables."""
    if data[:8] != b'!<arch>\n':
        fail('the library is not a static archive')
    members, offset = [], 8
    while offset + 60 <= len(data):
        header = data[offset:offset + 60]
        if header[58:60] != b'`\n':
            fail(f'bad archive member header at offset {offset}')
        name, size = header[:16].decode().rstrip(), int(header[48:58])
        body = data[offset + 60:offset + 60 + size]
        if name.startswith('#1/'):
            length = int(name[3:])
            name, body = body[:length].rstrip(b'\0').decode(), body[length:]
        if not name.startswith(('__.SYMDEF', '/')):
            members.append((name, body))
        offset += 60 + size + (size & 1)
    return members


def object_identity(data):
    """The CPU subtype of a 64-bit arm64 Mach-O, or None for anything else."""
    if len(data) < 32:
        return None
    magic, cpu_type, cpu_subtype = struct.unpack_from('<III', data)
    return cpu_subtype if magic == 0xfeedfacf and cpu_type == 0x0100000c else None


def is_arm64e(data):
    subtype = object_identity(data)
    return subtype is not None and subtype & 0xff == 2


def code_and_relocations(data):
    """The instruction bytes of a Mach-O object's code sections, and how many of its relocations are signed pointers."""
    commands = struct.unpack_from('<8I', data)[4]
    offset, code, signed = 32, [], 0
    for _ in range(commands):
        command, size = struct.unpack_from('<II', data, offset)
        if command == 0x19:
            for index in range(struct.unpack_from('<I', data, offset + 64)[0]):
                section = offset + 72 + 80 * index
                length = struct.unpack_from('<Q', data, section + 40)[0]
                start, _, relocation_offset, relocations, flags = struct.unpack_from('<5I', data, section + 48)
                if flags & CODE_SECTION_FLAGS:
                    code.append(data[start:start + length])
                signed += sum(info >> 28 == ARM64_RELOC_AUTHENTICATED_POINTER
                              for _, info in struct.iter_unpack('<II', data[relocation_offset:relocation_offset + 8 * relocations]))
        offset += size
    return code, signed


def defined_symbols(data):
    """The external symbols a Mach-O object defines, from its symbol table. Xcode's nm cannot be used: it reads the bitcode Rust's newer LLVM embeds."""
    commands = struct.unpack_from('<8I', data)[4]
    offset, names = 32, set()
    for _ in range(commands):
        command, size = struct.unpack_from('<II', data, offset)
        if command == 2:
            symbols, count, strings = struct.unpack_from('<III', data, offset + 8)
            for index in range(count):
                name, kind = struct.unpack_from('<IB', data, symbols + 16 * index)
                if kind & 1 and kind & 0xe == 0xe:
                    start = strings + name
                    names.add(data[start:data.index(b'\0', start)].decode())
        offset += size
    return names


def census(code):
    """How many of each interesting instruction a run of code holds."""
    counts = dict.fromkeys(INSTRUCTIONS, 0)
    words = array('I')
    words.frombytes(bytes(code[:len(code) & ~3]))
    for word in words:
        name = WITH_REGISTER.get(word & 0xfffffc1f) or WITH_MODIFIER.get(word & 0xfffffc00) or WHOLE_WORD.get(word) or DESTINATION.get(word & 0xffffffe0)
        if name:
            counts[name] += 1
    return counts


def inspect_library(data):
    """What the archive holds: member CPU subtypes and the pointer-authentication evidence in its code."""
    totals, members, foreign, defined = dict.fromkeys(INSTRUCTIONS, 0), [], [], set()
    signed = 0
    for name, body in archive_members(data):
        subtype = object_identity(body)
        members.append(dict(name=name, bytes=len(body), cpu_subtype=None if subtype is None else hex(subtype)))
        if not is_arm64e(body):
            foreign.append(name)
            continue
        code, relocations = code_and_relocations(body)
        signed += relocations
        defined |= defined_symbols(body)
        for part in code:
            for key, value in census(part).items():
                totals[key] += value
    totals.update(authenticated_calls=sum(totals[key] for key in ('blraaz', 'blrabz', 'blraa', 'blrab')), auth_relocations=signed)
    return dict(members=members, non_arm64e_members=foreign, census=totals, defined=defined)


def header_exports(text):
    """The C functions a bridge header declares."""
    return set(re.findall(r'\b(nmb_[a-z0-9_]+)\s*\(', text))


def foreign_libraries(otool_text):
    """Linked libraries outside the system (a Homebrew path would never exist on the target)."""
    names = [line.strip().split(' ')[0] for line in otool_text.splitlines()[1:] if line.strip()]
    return [name for name in names if not name.startswith(SYSTEM_PREFIXES)]


def validate(record):
    """The contract of a build record: proves compiling and linking only, and carries nothing that may not be public."""
    expected = dict(schema=SCHEMA, target=TARGET, abi=ABI, toolchain=TOOLCHAIN, hardware_qualified=False, runtime_tested=False, apple_content=False, vendor_firmware=False)
    if any(record.get(key) != value for key, value in expected.items()):
        fail('build record breaks the contract')
    if len(record.get('patches', [])) != PATCH_COUNT:
        fail('build record has the wrong patch set')
    if record.get('non_arm64e_members'):
        fail(f"the library holds objects that are not arm64e: {record['non_arm64e_members'][:8]}")
    counts = record.get('census', {})
    if not (counts.get('authenticated_calls') and counts.get('pacibsp') and counts.get('paciza')):
        fail('the library shows no pointer authentication: calls %s, pacibsp %s, paciza %s'
             % (counts.get('authenticated_calls'), counts.get('pacibsp'), counts.get('paciza')))
    if record.get('missing_exports'):
        fail(f"the library does not define {record['missing_exports'][:8]}")
    link = record.get('link_check', {})
    if not link.get('ok') or link.get('foreign_libraries'):
        fail('the library does not link into an arm64e image against system libraries only')


def tool_paths():
    return {tool: subprocess.check_output(['xcrun', '--sdk', 'macosx', '--find', tool], text=True).strip() for tool in ('clang', 'clang++', 'ar', 'nm', 'otool')}


def rust_tool(name):
    result = subprocess.run(['rustup', 'which', '--toolchain', TOOLCHAIN, name], capture_output=True, text=True)
    if result.returncode:
        fail(f'{name} of toolchain {TOOLCHAIN} is not installed: {result.stderr.strip()}')
    return result.stdout.strip()


def find_llvm_as():
    """llvm-as of LLVM 21 or newer, which Xcode does not ship."""
    candidates = [os.environ.get('LLVM_AS'), '/opt/homebrew/opt/llvm/bin/llvm-as', shutil.which('llvm-as')]
    path = next((candidate for candidate in candidates if candidate and Path(candidate).exists()), None)
    if not path:
        fail('llvm-as is required')
    version = subprocess.check_output([path, '--version'], text=True)
    major = re.search(r'LLVM version (\d+)', version)
    if not major or int(major.group(1)) < MINIMUM_LLVM:
        fail(f'llvm-as is older than LLVM {MINIMUM_LLVM}')
    return path, major.group(0)


def assemble_air(llvm_as, inputs, out):
    """Assemble the embedded shaders. Returns the environment naming them and their digests."""
    env, digests = {}, {}
    for name, source in inputs.items():
        target = out / f'{name.lower()}.air'
        subprocess.run([llvm_as, '-o', str(target), str(source)], check=True)
        env[name] = str(target)
        digests[name] = sha256(target.read_bytes())
    return env, digests


def link_check(tools, sdk, library, out):
    """Link the whole library into an arm64e bundle, which shows every import resolves against the SDK. The bundle is deleted afterwards."""
    source, bundle, log = out / 'link-check.c', out / 'link-check.bundle', out / 'logs/link-check.log'
    source.write_text('int sheng_link_check;\n')
    with log.open('w') as stream:
        result = subprocess.run([tools['clang'], '-target', CLANG_TARGET, '-isysroot', sdk, '-bundle', '-o', str(bundle), str(source),
                                 f'-Wl,-force_load,{library}', *LINK_LIBRARIES], stdout=stream, stderr=subprocess.STDOUT)
    text = log.read_text()
    outcome = dict(ok=result.returncode == 0, warnings=len(re.findall(r'^ld: warning', text, re.M)), log_tail=text[-1500:])
    if result.returncode == 0:
        data = bundle.read_bytes()
        linked = subprocess.check_output([tools['otool'], '-L', str(bundle)], text=True)
        (out / 'logs/link-check-otool-L.txt').write_text(linked)
        undefined = subprocess.run([tools['nm'], '--no-llvm-bc', '-u', str(bundle)], capture_output=True, text=True)
        outcome.update(ok=is_arm64e(data), bytes=len(data), cpu_subtype=hex(object_identity(data)), foreign_libraries=foreign_libraries(linked),
                       undefined_symbols=len(undefined.stdout.split()) if undefined.returncode == 0 else None)
        bundle.unlink()
    return outcome


def build(args, sources):
    tools = tool_paths()
    sdk = subprocess.check_output(['xcrun', '--sdk', 'macosx', '--show-sdk-path'], text=True).strip()
    sdk_version = subprocess.check_output(['xcrun', '--sdk', 'macosx', '--show-sdk-version'], text=True).strip()
    rustc, cargo = rust_tool('rustc'), rust_tool('cargo')
    llvm_as, llvm_version = find_llvm_as()
    translator, adreno_metal = Path(args.translator).resolve(), Path(args.adreno_metal).resolve()
    out = Path(args.out).resolve()
    (out / 'logs').mkdir(parents=True)
    builtin = subprocess.check_output([rustc, '-Z', 'unstable-options', '--print', 'target-spec-json', '--target', TARGET], text=True)
    (out / 'rustc-builtin-target.json').write_text(builtin)
    spec_path = out / f'{TARGET}.json'
    spec_path.write_text(json.dumps(target_spec(builtin), indent=2, sort_keys=True) + '\n')
    air, air_digests = assemble_air(llvm_as, air_inputs(translator, adreno_metal), out)
    manifest = out / 'Cargo.toml'
    manifest.write_text(manifest_text(adreno_metal, translator))
    pinned_lock = ROOT / CARGO_LOCK
    locked = []
    if pinned_lock.exists():
        shutil.copyfile(pinned_lock, out / 'Cargo.lock')
        locked = ['--locked']
    env = build_env(os.environ, tools, sdk, air, sources['runtime_id'])
    env.update(RUSTC=rustc, PATH=str(Path(rustc).parent) + os.pathsep + env['PATH'])
    log = out / 'logs/cargo.log'
    target_dir = out / 'target'
    try:
        with log.open('w') as stream:
            result = subprocess.run([cargo, 'build', '--release', '-Zbuild-std=std,panic_unwind', '-Zjson-target-spec', *locked, '--manifest-path', str(manifest),
                                     '--target', str(spec_path), '--target-dir', str(target_dir)], cwd=out, env=env, stdout=stream, stderr=subprocess.STDOUT,
                                    timeout=args.timeout)
    except subprocess.TimeoutExpired:
        fail(f'cargo did not finish within {args.timeout} s\n' + log.read_text()[-6000:])
    if result.returncode:
        fail('cargo failed\n' + log.read_text()[-6000:])
    library = out / LIBRARY
    shutil.copyfile(target_dir / TARGET / 'release' / LIBRARY, library)
    data = library.read_bytes()
    licenses = out / 'licenses'
    licenses.mkdir()
    for prefix, directory in (('translator', translator), ('adreno_metal', adreno_metal)):
        for name in LICENSE_FILES[prefix]:
            shutil.copyfile(directory / name, licenses / f"{'metal2vulkan' if prefix == 'translator' else 'AdrenoMetal'}-{name}")
    inspection = inspect_library(data)
    defined = inspection.pop('defined')
    declared = sorted(header_exports((adreno_metal / 'bridge/NativeMetalBridge.h').read_text(encoding='utf-8'))
                      | header_exports((adreno_metal / 'bridge/NativeMetalObjects.h').read_text(encoding='utf-8')))
    record = dict(schema=SCHEMA, built_utc=datetime.now(timezone.utc).isoformat(), target=TARGET, abi=ABI, deployment_target=DEPLOYMENT, sdk_version=sdk_version, toolchain=TOOLCHAIN,
                  rustc=subprocess.check_output([rustc, '-Vv'], text=True), cargo=subprocess.check_output([cargo, '-V'], text=True).strip(),
                  clang=subprocess.check_output([tools['clang'], '--version'], text=True).splitlines()[0], llvm_as=llvm_version, rustflags=RUSTFLAGS,
                  target_spec_sha256=sha256(spec_path.read_bytes()), cargo_lock_sha256=sha256((out / 'Cargo.lock').read_bytes()), cargo_lock_pinned=bool(locked), air_sha256=air_digests,
                  bytes=len(data), sha256=sha256(data), declared_exports=len(declared), missing_exports=[name for name in declared if '_' + name not in defined],
                  license='metal2vulkan GPL-2.0-or-later OR LGPL-3.0-or-later; AdrenoMetal MIT', hardware_qualified=False, runtime_tested=False, apple_content=False,
                  vendor_firmware=False, **sources, **inspection)
    record['link_check'] = link_check(tools, sdk, library, out)
    (out / 'build.json').write_text(json.dumps(record, indent=2) + '\n')
    validate(record)
    counts = record['census']
    print(f"PASS: {LIBRARY} {len(data)} bytes, {counts['authenticated_calls']} authenticated calls, {counts['blr']} plain, {counts['paciza']} pointers signed in code; nothing was run")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--prepare-only', action='store_true', help='check the pins and apply the patches, build nothing')
    parser.add_argument('--translator', default=str(ROOT / 'sources/metal-bridge/metal2vulkan'))
    parser.add_argument('--adreno-metal', default=str(ROOT / 'sources/metal-bridge/AdrenoMetal'))
    parser.add_argument('--unixnu', default=str(ROOT / 'sources/metal-bridge/UniXNU'))
    parser.add_argument('--out', default=str(ROOT / 'out/metal-bridge'))
    parser.add_argument('--timeout', type=int, default=6600, help='seconds allowed for cargo')
    args = parser.parse_args()
    sources = prepare(args.translator, args.adreno_metal, args.unixnu)
    print(f"Prepared metal2vulkan {sources['translator_commit'][:12]}: {len(sources['patches'])} patches, runtime id {sources['runtime_id']}")
    if not args.prepare_only:
        build(args, sources)


if __name__ == '__main__':
    sys.exit(main())
