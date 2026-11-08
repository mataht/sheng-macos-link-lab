/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Host fixture: the boot log panel wraps in place and never moves earlier rows. */
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include "framebuffer_owner.h"

struct sheng_framebuffer_owner sheng_framebuffer;
static uint64_t sheng_bootstrap_vm_stride;
static unsigned sheng_qr_extent(unsigned width, unsigned height, unsigned *scale)
{
    (void)width; (void)height; *scale = 0;
    return 0;
}
static void sheng_qr_draw(uintptr_t base, unsigned stride, unsigned width, unsigned height)
{
    (void)base; (void)stride; (void)width; (void)height;
}
#include "panel_console.h"

#define ROWS 24u
#define COLUMNS 88u
#define SCALE 2u
#define WIDTH (COLUMNS * 8 * SCALE)
#define ROW_LINES (16 * SCALE)
#define GUARD_ROWS 2u
#define SENTINEL 0x12345678u
#define BLACK 0xff000000u
#define MARK 0xff808080u

static uint32_t pixels[(ROWS + GUARD_ROWS) * ROW_LINES * WIDTH];
static int failures;

static void check(int condition, const char *name)
{
    if (!condition) {
        printf("FAILED: %s\n", name);
        failures++;
    }
}

static void setup(void)
{
    for (size_t n = 0; n < sizeof(pixels) / sizeof(pixels[0]); n++) pixels[n] = SENTINEL;
    sheng_panel.base = (uintptr_t)pixels;
    sheng_panel.stride = WIDTH * 4;
    sheng_panel.columns = COLUMNS;
    sheng_panel.rows = ROWS;
    sheng_panel.scale = SCALE;
    sheng_panel.x = sheng_panel.y = sheng_panel.pinned = 0;
    sheng_panel_ready = 1;
    sheng_framebuffer.owner = SHENG_FB_BOOT;
    sheng_framebuffer.writer = 0;
}

static uint32_t pixel(unsigned row, unsigned line, unsigned x)
{
    return pixels[(row * ROW_LINES + line) * WIDTH + x];
}

static int row_is(unsigned row, uint32_t value)
{
    for (unsigned line = 0; line < ROW_LINES; line++)
        for (unsigned x = 0; x < WIDTH; x++)
            if (pixel(row, line, x) != value) return 0;
    return 1;
}

static int row_is_marked(unsigned row)
{
    const unsigned first = 8 * SCALE;
    for (unsigned line = 0; line < ROW_LINES; line++)
        for (unsigned x = 0; x < WIDTH; x++) {
            const uint32_t expected = line >= first && line < first + SCALE ? MARK : BLACK;
            if (pixel(row, line, x) != expected) return 0;
        }
    return 1;
}

static int guard_intact(void)
{
    for (unsigned row = ROWS; row < ROWS + GUARD_ROWS; row++)
        if (!row_is(row, SENTINEL)) return 0;
    return 1;
}

int main(void)
{
    setup();
    sheng_panel_putc('\n');
    check(sheng_panel.y == 1 && sheng_panel.x == 0, "newline advances the cursor");
    check(row_is(1, BLACK), "the new row is cleared");
    check(row_is_marked(2), "the following row carries the line");
    check(row_is(0, SENTINEL) && row_is(3, SENTINEL), "other rows are untouched before the wrap");

    setup();
    sheng_panel.y = ROWS - 1;
    sheng_panel_putc('\n');
    check(sheng_panel.y == 0, "the last row wraps to the first");
    check(row_is(0, BLACK), "the wrapped row is cleared");
    check(row_is_marked(1), "the line follows the wrapped row");
    check(row_is(10, SENTINEL) && row_is(ROWS - 1, SENTINEL), "wrapping moves no earlier rows");
    check(guard_intact(), "wrapping stays inside the panel");

    setup();
    sheng_panel.y = ROWS - 2;
    sheng_panel_putc('\n');
    check(sheng_panel.y == ROWS - 1, "the last row is used");
    check(row_is_marked(0), "the line wraps ahead of the cursor");
    check(row_is(5, SENTINEL) && guard_intact(), "the line wrap writes only its own row");

    setup();
    sheng_panel.pinned = 5;
    sheng_panel.y = ROWS - 1;
    sheng_panel_putc('\n');
    check(sheng_panel.y == 5, "wrapping respects pinned rows");
    check(row_is_marked(6), "the line stays below pinned rows");
    for (unsigned row = 0; row < 5; row++) check(row_is(row, SENTINEL), "pinned rows are preserved");

    setup();
    sheng_framebuffer.writer = 1;
    sheng_panel.y = ROWS - 1;
    sheng_panel_putc('\n');
    check(sheng_panel.y == 0, "a busy lease still commits a valid cursor");
    for (unsigned row = 0; row < ROWS; row++) check(row_is(row, SENTINEL), "a busy lease writes nothing");

    setup();
    sheng_framebuffer.owner = SHENG_FB_GUI;
    sheng_panel.y = 3;
    sheng_panel_putc('\n');
    check(sheng_panel.y == 3 && row_is(4, SENTINEL), "the panel stops once the GUI owns the screen");

    setup();
    sheng_panel.y = ROWS - 1;
    for (unsigned n = 0; n < COLUMNS; n++) sheng_panel_putc('X');
    check(sheng_panel.y == 0 && sheng_panel.x == 0, "a full row wraps the cursor");
    check(pixel(ROWS - 1, 0, 0) == 0xffffffffu, "the glyphs stay on the finished row");
    check(row_is(0, BLACK) && row_is_marked(1), "the wrap after a full row is clean");

    setup();
    for (unsigned n = 0; n < 3 * ROWS; n++) {
        sheng_panel_putc('\n');
        check(sheng_panel.y < ROWS, "the cursor stays in range");
    }
    check(guard_intact(), "repeated wraps stay inside the panel");

    if (!failures) puts("ok");
    return failures != 0;
}
