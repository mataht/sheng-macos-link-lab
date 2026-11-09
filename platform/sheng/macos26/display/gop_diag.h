// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
// native-gop-diag=1 (default off): a strip of hexadecimal fields drawn over the top-left of the panel, for filming during a
// hardware test. Fields, separated by a wider gap: swaps/s, frames/s, swap_wait Busy/s, layer mask of the last swap, swaps
// carrying another surface besides the one scanned out (a cursor plane), touch reports/s, finger-down count, finger-up count,
// last X, last Y (0..7fff), HID user clients, handleReport failures, heartbeat. Pure formatting; the display driver owns the pixels.

enum : uint32_t {
    kNativeGopDiagHeight = 48,       // strip rows
    kNativeGopDiagScale = 8,         // each cell of a 3x5 glyph is 8x8 pixels
    kNativeGopDiagAdvance = 32,      // pixels from one digit to the next (3 cells plus a gap)
    kNativeGopDiagFieldGap = 16,     // extra gap between fields
    kNativeGopDiagMaxWidth = 1728,   // strip width on the panel
    kNativeGopDiagBackground = 0xff300000u,
    kNativeGopDiagForeground = 0xffffffffu,
};

struct NativeGopDiagField {
    uint32_t value;
    uint8_t digits;   // hexadecimal digits, most significant first
};

static inline uint32_t nativeGopDiagClampWidth(uint32_t panelWidth)
{
    return panelWidth < kNativeGopDiagMaxWidth ? panelWidth : kNativeGopDiagMaxWidth;
}

// 15-bit glyphs for 0-9 and a-f: five rows of three bits, top row in the high bits, bit 2 of a row is its left cell.
static inline uint16_t nativeGopDiagGlyph(unsigned digit)
{
    static const uint16_t glyphs[16] = {
        075557, 026227, 071747, 071717, 055711, 074717, 074757, 071222,
        075757, 075717, 075755, 065656, 074447, 065556, 074747, 074744,
    };
    return glyphs[digit & 15];
}

// Paints the fields into a width-by-kNativeGopDiagHeight BGRA strip (row stride = width pixels), clipping at the right edge.
// Returns the x position after the last digit drawn.
static inline uint32_t nativeGopDiagRender(uint32_t *strip, uint32_t width, const NativeGopDiagField *fields, unsigned count)
{
    for (uint64_t i = 0; i < static_cast<uint64_t>(width) * kNativeGopDiagHeight; i++)
        strip[i] = kNativeGopDiagBackground;
    uint32_t x = 8;
    for (unsigned field = 0; field < count; field++) {
        for (unsigned digit = fields[field].digits; digit > 0; digit--) {
            const uint16_t glyph = nativeGopDiagGlyph(fields[field].value >> ((digit - 1) * 4));
            for (unsigned row = 0; row < 5; row++) {
                for (unsigned column = 0; column < 3; column++) {
                    if (!(glyph >> (((4 - row) * 3) + (2 - column)) & 1))
                        continue;
                    for (uint32_t dy = 0; dy < kNativeGopDiagScale; dy++) {
                        const uint32_t py = 4 + row * kNativeGopDiagScale + dy;
                        for (uint32_t dx = 0; dx < kNativeGopDiagScale; dx++) {
                            const uint32_t px = x + column * kNativeGopDiagScale + dx;
                            if (px < width && py < kNativeGopDiagHeight)
                                strip[static_cast<uint64_t>(py) * width + px] = kNativeGopDiagForeground;
                        }
                    }
                }
            }
            x += kNativeGopDiagAdvance;
        }
        x += kNativeGopDiagFieldGap;
    }
    return x;
}
