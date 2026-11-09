#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Assemble the Metal plugin bundle for macOS arm64e: DroidVM's plugin class, AdrenoMetal's Metal frontend, the Rust bridge and Turnip.

The plugin source and the frontend are fetched at their pinned commits and compiled as they are. The bridge and Turnip come from the
jobs before this one. Both Mach-O files are ad-hoc signed and their CDHashes recorded, because a local trust cache has to name them.

This only proves the bundle links. Nothing here loads it, and no Apple or vendor content goes into the sources or the output. The bundle
contains metal2vulkan (GPL-2.0-or-later OR LGPL-3.0-or-later): it is a CI artifact for testing and must not be published as a release
until the license question in g3-plan.md is settled.

    build_metal_plugin_macos.py [--plugin-source DIR] [--adreno-metal DIR] [--bridge DIR] [--turnip DIR] [--out DIR]
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import struct
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
LOCK = 'config/metal-plugin-sources.json'
SCHEMA = 'sheng-metal-plugin-v1'
TARGET = 'arm64e-apple-macos26.5'
NAME = 'NativeAdrenoMetal'
PRINCIPAL_CLASS = 'NativeAdrenoMetalDevice'
IDENTIFIER = 'org.sheng.NativeAdrenoMetal'
TURNIP = 'libvulkan_freedreno.dylib'
TURNIP_IDENTIFIER = 'org.sheng.turnip'
BRIDGE = 'libadreno_metal_bridge.a'
PLUGIN_SOURCE = 'overlay/metal/NativeAdrenoMetalPlugin.mm'
FRONTEND_SOURCE = 'NativeBridgeMetalFrontend.mm'
LINK_STUB = 'platform/sheng/macos26/gpu/IOGPU.tbd'
LINK_LIBRARIES = ('-framework', 'Foundation', '-framework', 'Metal', '-framework', 'IOSurface', '-framework', 'IOKit', '-framework', 'CoreFoundation',
                  '-lobjc', '-lc++', '-liconv')
SYSTEM_PREFIXES = ('/usr/lib/', '/System/Library/')
LICENSES = 'plugin BSD-2-Clause-Patent; AdrenoMetal MIT; metal2vulkan GPL-2.0-or-later OR LGPL-3.0-or-later; Mesa MIT'


def fail(message):
    raise SystemExit('build_metal_plugin_macos: ' + message)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def pins(root=ROOT):
    entries = json.loads((Path(root) / LOCK).read_text())['sources']
    return {Path(entry['path']).name: entry for entry in entries}


def head(directory):
    return subprocess.check_output(['git', '-C', str(directory), 'rev-parse', 'HEAD'], text=True).strip()


def check_sources(plugin_source, adreno_metal, root=ROOT):
    """The two checkouts must be exactly the locked commits. Returns the digests of the two compiled files."""
    locked = pins(root)
    for checkout, name in ((plugin_source, 'macOS-DroidVM'), (adreno_metal, 'AdrenoMetal')):
        if head(checkout) != locked[name]['commit']:
            fail(f'{name} is not at the pinned commit')
    plugin = Path(plugin_source) / PLUGIN_SOURCE
    frontend = Path(adreno_metal) / FRONTEND_SOURCE
    if 'SPDX-License-Identifier: BSD-2-Clause-Patent' not in plugin.read_text(encoding='utf-8').splitlines()[0]:
        fail('the plugin source lost its license header')
    return dict(plugin_commit=locked['macOS-DroidVM']['commit'], adreno_metal_commit=locked['AdrenoMetal']['commit'],
                plugin_source_sha256=sha256(plugin.read_bytes()), frontend_source_sha256=sha256(frontend.read_bytes()))


def info_plist():
    return plistlib.dumps(dict(CFBundleExecutable=NAME, CFBundleIdentifier=IDENTIFIER, CFBundleName=NAME, CFBundlePackageType='BNDL', CFBundleVersion='1',
                               CFBundleShortVersionString='1', CFBundleInfoDictionaryVersion='6.0', CFBundleSupportedPlatforms=['MacOSX'],
                               NSPrincipalClass=PRINCIPAL_CLASS))


def compile_command(clang, sdk, include, source, output):
    return [clang, '-target', TARGET, '-isysroot', sdk, '-fobjc-arc', '-O2', '-g', '-std=c++17', '-Wno-deprecated-declarations', '-c', '-x', 'objective-c++',
            '-I', str(include), str(source), '-o', str(output)]


def link_command(clang, sdk, objects, bridge, stub, output):
    return [clang, '-target', TARGET, '-isysroot', sdk, '-bundle', '-Wl,-dead_strip', *map(str, objects), str(bridge), str(stub), *LINK_LIBRARIES, '-o', str(output)]


def is_arm64e(data):
    if len(data) < 12:
        return False
    magic, cpu_type, cpu_subtype = struct.unpack_from('<III', data)
    return magic == 0xfeedfacf and cpu_type == 0x0100000c and cpu_subtype & 0xff == 2


def foreign_libraries(otool_text, own=()):
    """Linked libraries outside the system (a build-machine path would never exist on the target)."""
    names = [line.strip().split(' ')[0] for line in otool_text.splitlines()[1:] if line.strip()]
    return [name for name in names if name not in own and not name.startswith(SYSTEM_PREFIXES)]


def describe_signature(codesign_text):
    found = re.search(r'^CDHash=([0-9a-f]{40})$', codesign_text, re.M)
    if not found:
        fail('codesign reported no CDHash')
    return found.group(1)


def validate(record):
    """The contract of a build record: proves linking only, and carries nothing that may not be public."""
    expected = dict(schema=SCHEMA, target=TARGET, runtime_tested=False, hardware_qualified=False, apple_content=False, vendor_firmware=False, release=False)
    if any(record.get(key) != value for key, value in expected.items()):
        fail('build record breaks the contract')
    for part in ('plugin', 'turnip'):
        entry = record.get(part, {})
        if not re.fullmatch('[0-9a-f]{40}', entry.get('cdhash', '')) or entry.get('foreign_libraries'):
            fail(f'{part} has no CDHash or links a non-system library')
    if not record.get('principal_class_defined'):
        fail(f'{NAME} does not define {PRINCIPAL_CLASS}')


def tools():
    return {tool: subprocess.check_output(['xcrun', '--sdk', 'macosx', '--find', tool], text=True).strip() for tool in ('clang++', 'nm', 'otool', 'strip')}


def signed(path, identifier, found):
    """Strip local symbols, ad-hoc sign, and describe the result."""
    subprocess.run([found['strip'], '-x', str(path)], check=True, capture_output=True)
    subprocess.run(['/usr/bin/codesign', '--force', '--sign', '-', '--identifier', identifier, str(path)], check=True)
    signature = subprocess.run(['/usr/bin/codesign', '-dvvv', str(path)], capture_output=True, text=True, check=True)
    data = path.read_bytes()
    if not is_arm64e(data):
        fail(f'{path.name} is not thin arm64e')
    return dict(bytes=len(data), sha256=sha256(data), cdhash=describe_signature(signature.stderr + signature.stdout), identifier=identifier)


def build(args, sources):
    found = tools()
    sdk = subprocess.check_output(['xcrun', '--sdk', 'macosx', '--show-sdk-path'], text=True).strip()
    sdk_version = subprocess.check_output(['xcrun', '--sdk', 'macosx', '--show-sdk-version'], text=True).strip()
    out = Path(args.out).resolve()
    (out / 'logs').mkdir(parents=True)
    work = out / 'objects'
    work.mkdir()
    adreno_metal = Path(args.adreno_metal).resolve()
    inputs = [(Path(args.plugin_source).resolve() / PLUGIN_SOURCE, work / 'plugin.o'), (adreno_metal / FRONTEND_SOURCE, work / 'frontend.o')]
    for source, target in inputs:
        log = out / 'logs' / (target.stem + '.log')
        with log.open('w') as stream:
            result = subprocess.run(compile_command(found['clang++'], sdk, adreno_metal, source, target), stdout=stream, stderr=subprocess.STDOUT)
        if result.returncode:
            errors = [line for line in log.read_text().splitlines() if ': error:' in line or 'fatal error:' in line]
            fail(f'{source.name} did not compile\n' + '\n'.join(errors)[:6000])
    contents = out / f'{NAME}.bundle/Contents'
    executable = contents / 'MacOS' / NAME
    executable.parent.mkdir(parents=True)
    log = out / 'logs/link.log'
    with log.open('w') as stream:
        result = subprocess.run(link_command(found['clang++'], sdk, [target for _, target in inputs], Path(args.bridge).resolve() / BRIDGE, ROOT / LINK_STUB, executable),
                                stdout=stream, stderr=subprocess.STDOUT)
    if result.returncode:
        fail('link failed\n' + log.read_text()[-6000:])
    (contents / 'Info.plist').write_bytes(info_plist())
    plugin = signed(executable, IDENTIFIER, found)
    turnip_path = contents / 'MacOS' / TURNIP
    shutil.copyfile(Path(args.turnip).resolve() / TURNIP, turnip_path)
    turnip = signed(turnip_path, TURNIP_IDENTIFIER, found)
    plugin_links = subprocess.check_output([found['otool'], '-L', str(executable)], text=True)
    turnip_links = subprocess.check_output([found['otool'], '-L', str(turnip_path)], text=True)
    (out / 'logs/otool-L.txt').write_text(plugin_links + '\n' + turnip_links)
    plugin['foreign_libraries'] = foreign_libraries(plugin_links)
    turnip['foreign_libraries'] = foreign_libraries(turnip_links, ('@rpath/' + TURNIP,))
    symbols = subprocess.run([found['nm'], '--no-llvm-bc', '-gU', str(executable)], capture_output=True, text=True, check=True).stdout.split()
    bridge_record = json.loads((Path(args.bridge).resolve() / 'build.json').read_text())
    record = dict(schema=SCHEMA, built_utc=datetime.now(timezone.utc).isoformat(), target=TARGET, sdk_version=sdk_version, plugin=plugin, turnip=turnip,
                  info_plist_sha256=sha256(info_plist()), principal_class_defined=f'_OBJC_CLASS_$_{PRINCIPAL_CLASS}' in symbols, link_warnings=len(re.findall(r'^ld: warning', log.read_text(), re.M)),
                  bridge_sha256=bridge_record['sha256'], translator_commit=bridge_record['translator_commit'], licenses=LICENSES, runtime_tested=False, hardware_qualified=False,
                  apple_content=False, vendor_firmware=False, release=False, **sources)
    (out / 'build.json').write_text(json.dumps(record, indent=2) + '\n')
    validate(record)
    print(f"PASS: {NAME} {plugin['bytes']} bytes CDHash {plugin['cdhash']}; {TURNIP} {turnip['bytes']} bytes CDHash {turnip['cdhash']}; nothing was run")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--plugin-source', default=str(ROOT / 'sources/metal-plugin/macOS-DroidVM'))
    parser.add_argument('--adreno-metal', default=str(ROOT / 'sources/metal-plugin/AdrenoMetal'))
    parser.add_argument('--bridge', default=str(ROOT / 'out/metal-bridge'))
    parser.add_argument('--turnip', default=str(ROOT / 'out/turnip-macos'))
    parser.add_argument('--out', default=str(ROOT / 'out/metal-plugin'))
    args = parser.parse_args()
    build(args, check_sources(args.plugin_source, args.adreno_metal))


if __name__ == '__main__':
    sys.exit(main())
