// SPDX-License-Identifier: GPL-3.0-or-later
// Host checks for the native-gop-diag counter strip formatting.
#include <assert.h>
#include <stdint.h>
#include <string.h>
#include <vector>
#include "../platform/sheng/macos26/display/gop_diag.h"

static uint32_t pixel(const std::vector<uint32_t> &strip, uint32_t width, uint32_t x, uint32_t y)
{
    return strip[static_cast<uint64_t>(y) * width + x];
}

int main()
{
    assert(nativeGopDiagClampWidth(3048) == kNativeGopDiagMaxWidth);
    assert(nativeGopDiagClampWidth(800) == 800);
    // Glyph 1 is the shape 010/110/010/010/111.
    assert(nativeGopDiagGlyph(1) == 026227);
    assert(nativeGopDiagGlyph(0xf) == 074744);
    assert(nativeGopDiagGlyph(0x13) == nativeGopDiagGlyph(3));
    const uint32_t width = 800;
    std::vector<uint32_t> strip(static_cast<uint64_t>(width) * kNativeGopDiagHeight);
    NativeGopDiagField fields[] = {{0x1f, 2}, {0x8, 1}};
    const uint32_t end = nativeGopDiagRender(strip.data(), width, fields, 2);
    // Two digits of the first field, gap, one digit of the second.
    assert(end == 8 + 2 * kNativeGopDiagAdvance + kNativeGopDiagFieldGap + kNativeGopDiagAdvance + kNativeGopDiagFieldGap);
    // Digit '1' top row is .X. : the middle cell (x 8+8..8+16) is painted, the left cell is not.
    assert(pixel(strip, width, 8 + 8 + 4, 4 + 4) == kNativeGopDiagForeground);
    assert(pixel(strip, width, 8 + 4, 4 + 4) == kNativeGopDiagBackground);
    // Digit 'f' bottom-left cell is painted (rows 40..47 belong to the fifth glyph row at py 4+4*8).
    assert(pixel(strip, width, 8 + kNativeGopDiagAdvance + 2, 4 + 4 * kNativeGopDiagScale + 2) == kNativeGopDiagForeground);
    // Digit '8' first row is XXX: its first cell after the field gap.
    const uint32_t eightX = 8 + 2 * kNativeGopDiagAdvance + kNativeGopDiagFieldGap;
    assert(pixel(strip, width, eightX + 2, 6) == kNativeGopDiagForeground);
    // Narrow strips clip instead of writing past the buffer: the sanitizer run of this test checks it.
    std::vector<uint32_t> narrow(static_cast<uint64_t>(20) * kNativeGopDiagHeight);
    nativeGopDiagRender(narrow.data(), 20, fields, 2);
    return 0;
}
