#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Derive the default-off physical panel size step on top of the reviewed keep-awake display patch.

Chain: Stage18B patch -> hw-keep-awake.json (keep-awake switches) -> hw-panel-size.json (this step). The GOP has no EDID, so the
driver reports a 96-dpi fallback and WindowServer draws the 3048x2032 sheng panel at 1x. native-gop-width-mm and
native-gop-height-mm (both required, 50..1000 mm, aspect within 2 percent of the framebuffer) report the real size instead.
Only the new helper, the two DisplayAttributes image-size entries and get_dot_pitch change; everything else is byte-identical.
"""
import argparse
import difflib
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
DISPLAY = ROOT / 'platform/sheng/macos26/display'
PATCH = DISPLAY / '0001-native-gop-display.patch'
ORIGINAL_SHA = '050ae69eec6f0340c4f3659285dfb16fce13058648a044f9149b89d40344bcac'
HELPER = (
    '// native-gop-width-mm / native-gop-height-mm (default off) give the panel\'s physical size so the system derives its real density\n'
    '// (about 295 dpi for the 12.4-inch 3048x2032 sheng panel, 262x175 mm) instead of the 96-dpi fallback that draws the UI at 1x.\n'
    '// Both are required, each 50..1000 mm, and their aspect must match the framebuffer within 2 percent; otherwise the fallback stays.\n'
    'static bool nativeGopPanelSize(uint32_t width, uint32_t height, uint32_t *widthMM, uint32_t *heightMM)\n'
    '{\n'
    '    unsigned w = 0, h = 0;\n'
    '    if (!PE_parse_boot_argn("native-gop-width-mm", &w, sizeof(w)) || !PE_parse_boot_argn("native-gop-height-mm", &h, sizeof(h)))\n'
    '        return false;\n'
    '    if (w < 50 || w > 1000 || h < 50 || h > 1000 || !width || !height)\n'
    '        return false;\n'
    '    const uint64_t a = static_cast<uint64_t>(w) * height, b = static_cast<uint64_t>(h) * width;\n'
    '    if (a * 50 < b * 49 || a * 50 > b * 51)\n'
    '        return false;\n'
    '    *widthMM = w;\n'
    '    *heightMM = h;\n'
    '    return true;\n'
    '}\n'
    '\n')
# Placed just before createDisplayAttributes, away from the keep-awake helper block that tests compile on their own.
ANCHOR = '// AppleParavirtDisplay::createDisplayAttributes, with the display of the boot framebuffer:'
REPLACEMENTS = (
    (ANCHOR, HELPER + ANCHOR),
    ('              appendNumber(attributes, "MaxHorizontalImageSize", (width * 254 + 4800) / 9600) &&\n'
     '              appendNumber(attributes, "MaxVerticalImageSize", (height * 254 + 4800) / 9600) &&\n',
     '              appendNumber(attributes, "MaxHorizontalImageSize", panel ? (widthMM + 5) / 10 : (width * 254 + 4800) / 9600) &&\n'
     '              appendNumber(attributes, "MaxVerticalImageSize", panel ? (heightMM + 5) / 10 : (height * 254 + 4800) / 9600) &&\n'),
    ('    if (static_cast<uint64_t>(width + 160) * (height + 23) * kRefreshHz > UINT32_MAX)\n'
     '        return false;\n',
     '    if (static_cast<uint64_t>(width + 160) * (height + 23) * kRefreshHz > UINT32_MAX)\n'
     '        return false;\n'
     '    uint32_t widthMM = 0, heightMM = 0;\n'
     '    const bool panel = nativeGopPanelSize(width, height, &widthMM, &heightMM);\n'
     '    if (panel)\n'
     '        IOLog("NativeGOPDisplay: panel %ux%u mm, %u dpi\\n", widthMM, heightMM, (width * 254 + widthMM * 5) / (widthMM * 10));\n'),
    ('// GOP supplies no EDID here; use a 96-dpi logical fallback.\n'
     'IOReturn NativeGOPDisplay::get_dot_pitch(unsigned int *pitch)\n'
     '{\n'
     '    *pitch = 96;\n',
     '// GOP supplies no EDID here: the explicit panel size when given (see nativeGopPanelSize), else a 96-dpi logical fallback.\n'
     'IOReturn NativeGOPDisplay::get_dot_pitch(unsigned int *pitch)\n'
     '{\n'
     '    const uint32_t width = static_cast<uint32_t>(fVideo.v_width), height = static_cast<uint32_t>(fVideo.v_height);\n'
     '    uint32_t widthMM = 0, heightMM = 0;\n'
     '    *pitch = nativeGopPanelSize(width, height, &widthMM, &heightMM) ? (width * 254 + widthMM * 5) / (widthMM * 10) : 96;\n'),
)


HEADER_MARKER = '--- a/IOMobileFramebufferABI.h\n'


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def full_patch(original: str, source: str) -> str:
    # Same generator as derive_hw_display_keep_awake.full_patch, without the header hunk.
    return ''.join(difflib.unified_diff(original.splitlines(True), source.splitlines(True),
                                        fromfile='a/DroidVMDisplay.cpp', tofile='b/DroidVMDisplay.cpp'))


def apply(original: bytes, patch: bytes) -> str:
    with tempfile.TemporaryDirectory() as temporary:
        work = Path(temporary)
        (work / 'DroidVMDisplay.cpp').write_bytes(original)
        (work / 'p.patch').write_bytes(patch)
        result = subprocess.run(['patch', '--batch', '--fuzz=0', '-p1', '-i', 'p.patch'], cwd=work, capture_output=True, text=True)
        if result.returncode:
            raise ValueError('patch failed: ' + result.stdout[-400:] + result.stderr[-400:])
        return (work / 'DroidVMDisplay.cpp').read_text()


def derive(original_path: Path) -> dict:
    original = original_path.read_bytes().rstrip(b'\n') + b'\n'
    if sha(original) != ORIGINAL_SHA:
        raise ValueError('requires the pinned upstream DroidVMDisplay.cpp')
    keep_awake = json.loads((DISPLAY / 'hw-keep-awake.json').read_text())
    base_patch = PATCH.read_bytes()
    if sha(base_patch) != keep_awake['patch_sha256']:
        raise ValueError('the current patch is not the reviewed keep-awake patch; refusing to derive twice')
    text = base_patch.decode()
    if text.count(HEADER_MARKER) != 1:
        raise ValueError('keep-awake patch has no single framebuffer header hunk')
    # The IOMobileFramebufferABI.h hunk is carried over verbatim; only the DroidVMDisplay.cpp part is re-derived.
    cpp_part, header_hunk = text[:text.index(HEADER_MARKER)], text[text.index(HEADER_MARKER):]
    base = apply(original, cpp_part.encode())
    if full_patch(original.decode(), base) != cpp_part:
        raise ValueError('the keep-awake patch is not the unified diff of its source (derivation method mismatch)')
    source = base
    for old, new in REPLACEMENTS:
        if source.count(old) != 1:
            raise ValueError('anchor not found exactly once: ' + old.splitlines()[0])
        source = source.replace(old, new)
    changed = [line for line in difflib.unified_diff(base.splitlines(), source.splitlines(), lineterm='', n=0)
               if line.startswith(('+', '-')) and not line.startswith(('+++', '---'))]
    if any(line.startswith('-') for line in changed if 'ImageSize' not in line and 'pitch' not in line and 'EDID' not in line):
        raise ValueError('unexpected removal outside the panel-size step')
    cpp_patch = full_patch(original.decode(), source)
    if apply(original, cpp_patch.encode()) != source:
        raise ValueError('regenerated patch does not reproduce the derived source')
    patch = cpp_patch + header_hunk
    if patch[len(cpp_patch):] != base_patch.decode()[base_patch.decode().index(HEADER_MARKER):]:
        raise ValueError('framebuffer header hunk changed')
    PATCH.write_bytes(patch.encode())
    record = dict(schema='sheng-hw-display-panel-size-v1', status='source_only', compiled=False, ci_submitted=False,
                  hardware_run=False, vm_run=False, base_record='hw-keep-awake.json',
                  base_patch_sha256=keep_awake['patch_sha256'], patch_sha256=sha(patch.encode()),
                  source_sha256=sha(source.encode()), original_sha256=ORIGINAL_SHA,
                  boot_arguments=['native-gop-width-mm', 'native-gop-height-mm'], default_off=True,
                  bounds=dict(millimetres=[50, 1000], aspect_tolerance_percent=2),
                  sheng_panel=dict(width_mm=262, height_mm=175, pixels=[3048, 2032], dpi=295,
                                   basis='12.4-inch 3:2 panel; inference from the marketed diagonal, not a measured EDID'),
                  added_lines=sum(line.startswith('+') for line in changed),
                  removed_lines=sum(line.startswith('-') for line in changed),
                  note='Reports DisplayAttributes MaxHorizontal/VerticalImageSize and get_dot_pitch from the panel size; '
                       'whether WindowServer then selects a 2x mode is a runtime question.')
    (DISPLAY / 'hw-panel-size.json').write_text(json.dumps(record, indent=2) + '\n')
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--original', type=Path, required=True, help='pinned upstream DroidVMDisplay.cpp')
    print(json.dumps(derive(parser.parse_args().original), indent=2))


if __name__ == '__main__':
    main()
