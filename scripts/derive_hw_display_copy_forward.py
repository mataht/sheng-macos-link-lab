#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Derive the copy-forward step on top of the reviewed panel-size display patch.

Chain: Stage18B patch -> hw-keep-awake.json -> hw-panel-size.json -> hw-copy-forward.json (this step).
On the real tablet (Step18 boot, 2026-11-07) moving the Language Chooser selection left highlights on alternating rows
(1,3,5 then 2,4,6). That matches WindowServer alternating its root surfaces and redrawing only the damaged area into the
one it submits, as if that surface still held the frame shown last. The surfaces then each miss the other's updates, and a
whole-surface copy shows the stale rows. Pixels alone cannot recover the damage (a row redrawn to the value it had two frames
earlier looks unchanged), so the driver makes the assumption true instead: while it processes a swap, WindowServer does not
compose (it waits for the swap, IOMFBDisplay::can_update_), so every other cached root surface of the same geometry is idle;
before the swap completes, the shown surface is copied into them. If the surfaces were complete already, this is harmless:
WindowServer's own redraw over an up-to-date surface gives the same pixels. Not a boot argument: the copy path always uses it.
"""
import argparse
import difflib
import json
from pathlib import Path

from derive_hw_display_panel_size import HEADER_MARKER, ORIGINAL_SHA, apply, full_patch, sha

ROOT = Path(__file__).resolve().parents[1]
DISPLAY = ROOT / 'platform/sheng/macos26/display'
PATCH = DISPLAY / '0001-native-gop-display.patch'
FORWARD = (
    '    // Copy-forward (derive_hw_display_copy_forward.py): WindowServer alternates root surfaces and redraws only the\n'
    '    // damaged area into the one it submits, as if that surface still held the frame shown last. It does not compose\n'
    '    // while this swap is pending, so bring every other cached root surface of this geometry up to date before the swap\n'
    '    // completes and hands them back.\n'
    '    if (current) {\n'
    '        current->rowBytes = rowBytes;\n'
    '        current->rows = height;\n'
    '        current->rowLength = sourceRowBytes;\n'
    '        current->format = format;\n'
    '        for (SourceMap &other : fSourceMaps) {\n'
    '            if (&other == current || !other.memory || other.forwardRefused || other.span != span ||\n'
    '                other.rowBytes != rowBytes || other.rows != height || other.rowLength != sourceRowBytes ||\n'
    '                other.format != format)\n'
    '                continue;\n'
    '            if (!other.writable)\n'
    '                other.writable = other.memory->createMappingInTask(kernel_task, 0, kIOMapAnywhere, other.base, other.span);\n'
    '            if (!other.writable || other.writable->getLength() < span) {\n'
    '                other.forwardRefused = true;   // not retried for this cached surface\n'
    '                fForwardFailures++;\n'
    '                continue;\n'
    '            }\n'
    '            uint8_t *target = reinterpret_cast<uint8_t *>(other.writable->getVirtualAddress());\n'
    '            for (uint64_t y = 0; y < height; y++)\n'
    '                __builtin_memcpy(target + y * rowBytes, source + y * rowBytes, sourceRowBytes);\n'
    '            fForwardCopies++;\n'
    '        }\n'
    '    }\n')
REPLACEMENTS = (
    ('        uint64_t base, span, used;\n'
     '    };\n',
     '        uint64_t base, span, used;\n'
     '        IOMemoryMap *writable;        // copy-forward: writable kernel mapping of the same range, made on first use\n'
     '        uint64_t rowBytes, rows, rowLength;   // geometry when last shown (zero until then)\n'
     '        uint32_t format;\n'
     '        bool forwardRefused;          // no writable mapping could be made\n'
     '    };\n'),
    ('    uint64_t fRowsWritten = 0, fRowsSkipped = 0;   // diagnostic: scanout rows rewritten / left unchanged\n',
     '    uint64_t fRowsWritten = 0, fRowsSkipped = 0;   // diagnostic: scanout rows rewritten / left unchanged\n'
     '    SourceMap *fMappedSource = nullptr;           // the cache entry mapSource returned last\n'
     '    uint64_t fForwardCopies = 0, fForwardFailures = 0;   // diagnostic: copy-forward surface updates / unmappable\n'),
    ('        IOLog("NativeGOPDisplay: %llu swaps submitted, %llu shown, %llu dropped, %llu ticks stalled (current %p), last frame %llu lit centre %#llx, rows %llu written %llu unchanged\\n",\n'
     '              fSubmitted, fFrames, fDropped, fStalled, at<IOMFBSwapIORequest *>(this, kCurrentSwap), fLastLit, fLastCenter,\n'
     '              fRowsWritten, fRowsSkipped);\n',
     '        IOLog("NativeGOPDisplay: %llu swaps submitted, %llu shown, %llu dropped, %llu ticks stalled (current %p), last frame %llu lit centre %#llx, rows %llu written %llu unchanged, %llu surfaces forwarded %llu failed\\n",\n'
     '              fSubmitted, fFrames, fDropped, fStalled, at<IOMFBSwapIORequest *>(this, kCurrentSwap), fLastLit, fLastCenter,\n'
     '              fRowsWritten, fRowsSkipped, fForwardCopies, fForwardFailures);\n'),
    ('        if (entry.memory == memory && entry.base == base && entry.span == span) {\n'
     '            entry.used = ++fSourceMapClock;\n'
     '            return reinterpret_cast<const uint8_t *>(entry.map->getVirtualAddress());\n',
     '        if (entry.memory == memory && entry.base == base && entry.span == span) {\n'
     '            entry.used = ++fSourceMapClock;\n'
     '            fMappedSource = &entry;\n'
     '            return reinterpret_cast<const uint8_t *>(entry.map->getVirtualAddress());\n'),
    ('    if (victim->memory) {\n'
     '        victim->map->release();\n'
     '        victim->memory->complete();\n'
     '        victim->memory->release();\n'
     '        *victim = {};\n'
     '    }\n',
     '    fMappedSource = nullptr;\n'
     '    if (victim->memory) {\n'
     '        if (victim->writable)\n'
     '            victim->writable->release();\n'
     '        victim->map->release();\n'
     '        victim->memory->complete();\n'
     '        victim->memory->release();\n'
     '        *victim = {};\n'
     '    }\n'),
    ('    memory->retain();\n'
     '    *victim = {memory, map, base, span, ++fSourceMapClock};\n'
     '    return reinterpret_cast<const uint8_t *>(map->getVirtualAddress());\n',
     '    memory->retain();\n'
     '    *victim = {memory, map, base, span, ++fSourceMapClock, nullptr, 0, 0, 0, 0, false};\n'
     '    fMappedSource = victim;\n'
     '    return reinterpret_cast<const uint8_t *>(map->getVirtualAddress());\n'),
    ('        if (!entry.memory)\n'
     '            continue;\n'
     '        entry.map->release();\n',
     '        if (!entry.memory)\n'
     '            continue;\n'
     '        if (entry.writable)\n'
     '            entry.writable->release();\n'
     '        entry.map->release();\n'),
    ('        entry = {};\n'
     '    }\n'
     '}\n',
     '        entry = {};\n'
     '    }\n'
     '    fMappedSource = nullptr;\n'
     '}\n'),
    ('    const uint8_t *source = span <= (512ULL << 20) ? mapSource(memory, base, span) : nullptr;\n',
     '    const uint8_t *source = span <= (512ULL << 20) ? mapSource(memory, base, span) : nullptr;\n'
     '    SourceMap *current = source ? fMappedSource : nullptr;\n'),
    ('    fRowsWritten += blit.written;\n'
     '    fRowsSkipped += blit.skipped;\n',
     FORWARD +
     '    fRowsWritten += blit.written;\n'
     '    fRowsSkipped += blit.skipped;\n'),
)
REMOVED_OK = (
    '        IOLog("NativeGOPDisplay: %llu swaps submitted',
    '              fRowsWritten, fRowsSkipped);',
    '    *victim = {memory, map, base, span, ++fSourceMapClock};',
)


def derive(original_path: Path) -> dict:
    original = original_path.read_bytes().rstrip(b'\n') + b'\n'
    if sha(original) != ORIGINAL_SHA:
        raise ValueError('requires the pinned upstream DroidVMDisplay.cpp')
    panel = json.loads((DISPLAY / 'hw-panel-size.json').read_text())
    base_patch = PATCH.read_bytes()
    if sha(base_patch) != panel['patch_sha256']:
        raise ValueError('the current patch is not the reviewed panel-size patch; refusing to derive twice')
    text = base_patch.decode()
    if text.count(HEADER_MARKER) != 1:
        raise ValueError('panel-size patch has no single framebuffer header hunk')
    cpp_part, header_hunk = text[:text.index(HEADER_MARKER)], text[text.index(HEADER_MARKER):]
    base = apply(original, cpp_part.encode())
    if sha(base.encode()) != panel['source_sha256'] or full_patch(original.decode(), base) != cpp_part:
        raise ValueError('the panel-size patch does not reproduce its recorded source')
    source = base
    for old, new in REPLACEMENTS:
        if source.count(old) != 1:
            raise ValueError('anchor not found exactly once: ' + old.splitlines()[0])
        source = source.replace(old, new)
    changed = [line for line in difflib.unified_diff(base.splitlines(), source.splitlines(), lineterm='', n=0)
               if line.startswith(('+', '-')) and not line.startswith(('+++', '---'))]
    unexpected = [line for line in changed if line.startswith('-') and not line[1:].startswith(REMOVED_OK)]
    if unexpected:
        raise ValueError('unexpected removal outside the copy-forward step: ' + unexpected[0])
    cpp_patch = full_patch(original.decode(), source)
    if apply(original, cpp_patch.encode()) != source:
        raise ValueError('regenerated patch does not reproduce the derived source')
    patch = cpp_patch + header_hunk
    PATCH.write_bytes(patch.encode())
    keep_awake = json.loads((DISPLAY / 'hw-keep-awake.json').read_text())
    if sha((DISPLAY / 'gop_scanout.h').read_bytes()) != keep_awake['scanout_sha256']:
        raise ValueError('the copy-forward step leaves gop_scanout.h unchanged')
    record = dict(schema='sheng-hw-display-copy-forward-v1', status='source_only', compiled=False, ci_submitted=False,
                  hardware_run=False, vm_run=False, base_record='hw-panel-size.json',
                  base_patch_sha256=panel['patch_sha256'], patch_sha256=sha(patch.encode()),
                  source_sha256=sha(source.encode()), original_sha256=ORIGINAL_SHA,
                  scanout_sha256=keep_awake['scanout_sha256'], boot_argument=None, always_on=True,
                  evidence='Step18 hardware screen observations: highlights on alternating rows 1,3,5 / 2,4,6 '
                           'after volume-down (private audit 20261107-step18-display-keys-waiver-boot-01)',
                  assumption='WindowServer does not compose while a swap it submitted is pending (IOMFBDisplay::can_update_ '
                             'asks swap_wait first), so the other root surfaces are idle when they are written',
                  hardware_qualified=False,
                  added_lines=sum(line.startswith('+') for line in changed),
                  removed_lines=sum(line.startswith('-') for line in changed))
    (DISPLAY / 'hw-copy-forward.json').write_text(json.dumps(record, indent=2) + '\n')
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--original', type=Path, required=True, help='pinned upstream DroidVMDisplay.cpp')
    print(json.dumps(derive(parser.parse_args().original), indent=2))


if __name__ == '__main__':
    main()
