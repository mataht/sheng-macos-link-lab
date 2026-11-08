# SPDX-License-Identifier: GPL-3.0-or-later
"""The boot log panel must wrap in place: scrolling through the Device mapping dominated the boot time."""
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / 'platform/sheng/macos26/log'


def function_body(text, name):
    start = text.index('static void ' + name + '(void)')
    opening = text.index('{', start)
    depth = 0
    for index in range(opening, len(text)):
        depth += {'{': 1, '}': -1}.get(text[index], 0)
        if not depth:
            return text[opening:index + 1]
    raise ValueError(name)


class NativePanelWrap(unittest.TestCase):
    def test_newline_never_reads_the_framebuffer(self):
        body = function_body((LOG / 'panel_console.h').read_text(encoding='utf-8'), 'sheng_panel_newline')
        self.assertNotIn('src', body)
        self.assertIsNone(re.search(r'=\s*\*?\(?volatile', body))
        self.assertIn('sheng_panel_clear_row', body)

    def test_wrapping_on_the_host(self):
        clang = shutil.which('clang')
        if not clang:
            self.skipTest('requires clang')
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            shutil.copy2(LOG / 'framebuffer_owner.h', work / 'framebuffer_owner.h')
            # The barrier is the only AArch64 instruction left once framebuffer_owner.h compiles for the host.
            source = (LOG / 'panel_console.h').read_text(encoding='utf-8').replace('"dsb sy"', '""')
            (work / 'panel_console.h').write_text(source, encoding='utf-8')
            # The XNU font is not needed: only the glyph for X has pixels here.
            glyphs = ', '.join('0xff' if ord('X') * 16 <= n < (ord('X') + 1) * 16 else '0' for n in range(256 * 16))
            (work / 'pe_sheng_font.h').write_text(f'static const unsigned char iso_font[4096] = {{ {glyphs} }};\n')
            program = work / 'wrap'
            subprocess.run([clang, '-std=gnu11', '-O1', '-Wall', '-Wextra', '-Werror', '-Wno-unused-function', '-Wno-unused-variable',
                            '-I' + str(work), str(ROOT / 'tests/native_panel_wrap.c'), '-o', str(program)], check=True, timeout=60)
            result = subprocess.run([str(program)], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertEqual(result.stdout.strip(), 'ok')


if __name__ == '__main__':
    unittest.main()
