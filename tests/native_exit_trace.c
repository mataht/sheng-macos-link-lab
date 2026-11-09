// SPDX-License-Identifier: GPL-3.0-or-later
// Host test of the real exit-trace parser against a synthetic process image; run under AddressSanitizer so a bad bound fails.
#include <assert.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#define BASE 0x180000000ull
#define SLIDE 0x2000000ull
#define MEMORY_SIZE 0x40000u

static uint8_t memory[MEMORY_SIZE];
static char output[1 << 16];
static size_t output_used;
static unsigned reads;
static int everything_readable;

static int sheng_exit_read(uint64_t address, void *out, unsigned size)
{
    reads++;
    if (everything_readable) {
        memset(out, 0, size);
        return 0;
    }
    if (address < BASE || address - BASE > MEMORY_SIZE || MEMORY_SIZE - (address - BASE) < size)
        return 14;
    memcpy(out, memory + (address - BASE), size);
    return 0;
}

static int sheng_exit_read_string(uint64_t address, char *out, unsigned size)
{
    if (address < BASE || address - BASE >= MEMORY_SIZE)
        return 14;
    strncpy(out, (const char *)memory + (address - BASE), size);
    return 0;
}

static void print_line(const char *format, ...)
{
    va_list arguments;
    va_start(arguments, format);
    int written = vsnprintf(output + output_used, sizeof(output) - output_used, format, arguments);
    va_end(arguments);
    assert(written >= 0 && (size_t)written < sizeof(output) - output_used);
    output_used += (size_t)written;
}

#define SHENG_EXIT_PRINT(...) print_line(__VA_ARGS__)
#include "../platform/sheng/macos26/log/exit_trace.h"

static void put32(uint32_t offset, uint32_t value) { memcpy(memory + offset, &value, sizeof(value)); }
static void put64(uint32_t offset, uint64_t value) { memcpy(memory + offset, &value, sizeof(value)); }

static void reset(void)
{
    memset(memory, 0, sizeof(memory));
    memset(output, 0, sizeof(output));
    output_used = 0;
    reads = 0;
}

// A dylib at BASE whose text runs to +0x10000 and whose __DATA holds one __crash_info section at +0x20000.
static uint32_t image_with_crash_info(const char *message)
{
    reset();
    put32(0, 0xfeedfacfu);
    put32(4, 0x0100000cu);
    put32(12, 6);
    put32(16, 3);
    uint32_t at = 32;
    put32(at, 0x19);                         // __TEXT, no sections
    put32(at + 4, 72);
    memcpy(memory + at + 8, "__TEXT", 6);
    put64(at + 24, BASE - SLIDE);
    put64(at + 32, 0x10000);
    at += 72;
    put32(at, 0x19);                         // __DATA with __crash_info
    put32(at + 4, 72 + 80);
    memcpy(memory + at + 8, "__DATA", 6);
    put32(at + 64, 1);
    memcpy(memory + at + 72, "__crash_info", 12);
    put64(at + 72 + 32, BASE - SLIDE + 0x20000);
    at += 72 + 80;
    put32(at, 0x1b);
    put32(at + 4, 24);
    for (unsigned i = 0; i < 16; i++)
        memory[at + 8 + i] = (uint8_t)(0xa0 + i);
    at += 24;
    put32(20, at - 32);
    put64(0x20000, 5);                       // version
    put64(0x20008, BASE + 0x21000);          // message
    strcpy((char *)memory + 0x21000, message);
    return at;
}

static struct sheng_exit_work work;

static void frames(uint64_t first, uint64_t second, uint64_t third)
{
    memset(&work, 0, sizeof(work));
    work.frames[0] = (uintptr_t)first;
    work.frames[1] = (uintptr_t)second;
    work.frames[2] = (uintptr_t)third;
}

static void test_describes_one_image_once(void)
{
    image_with_crash_info("uncaught exception");
    frames(BASE + 0x8123, BASE + 0x8456, BASE + 0xc000);
    sheng_exit_note_images(&work, 3);
    assert(work.image_count == 1 && work.images[0].base == BASE && work.images[0].size == 0x10000);
    assert(strstr(output, "SHENG-EXIT: image 0x180000000 size 0x10000 slide 0x2000000 uuid a0a1a2a3a4a5a6a7a8a9aaabacadaeaf\n"));
    assert(strstr(output, "SHENG-EXIT: crash-info image 0x180000000 message \"uncaught exception\"\n"));
    const char *first = strstr(output, "SHENG-EXIT: image"), *again = strstr(first + 1, "SHENG-EXIT: image");
    assert(first && !again);
}

static void test_process_text_cannot_imitate_a_line(void)
{
    image_with_crash_info("a\"b\nSHENG-EXIT: forged\x7f");
    frames(BASE + 0x8000, 0, 0);
    sheng_exit_note_images(&work, 1);
    assert(strstr(output, "message \"a.b.SHENG-EXIT: forged.\"\n"));
}

static void test_long_text_is_bounded(void)
{
    image_with_crash_info("");
    memset(memory + 0x21000, 'x', 0x800);
    frames(BASE + 0x8000, 0, 0);
    sheng_exit_note_images(&work, 1);
    const char *start = strstr(output, "message \"");
    assert(start);
    const char *end = strchr(start + 9, '"');
    assert(end && end - (start + 9) == SHENG_EXIT_TEXT);
}

static void test_unmapped_and_absent_headers_are_quiet(void)
{
    image_with_crash_info("x");
    frames(0x7000000000ull, 0, 0);
    sheng_exit_note_images(&work, 1);
    assert(!output_used && !work.image_count);

    reset();                                 // mapped memory holding no header: the scan ends at the mapping's start
    frames(BASE + 0x8000, 0, 0);
    sheng_exit_note_images(&work, 1);
    assert(!output_used && !work.image_count);
}

static void test_bad_load_commands_are_refused(void)
{
    static const struct { uint32_t offset, value; } cases[] = {
        { 20, SHENG_EXIT_MAX_COMMANDS + 1 },     // sizeofcmds too large for the buffer
        { 36, 0 },                               // first cmdsize zero: must not spin
        { 36, 0x7fffffff },                      // first cmdsize past the end
        { 32 + 72 + 64, 0xffffffffu },           // section count past its command
    };
    for (unsigned i = 0; i < sizeof(cases) / sizeof(cases[0]); i++) {
        image_with_crash_info("x");
        put32(cases[i].offset, cases[i].value);
        frames(BASE + 0x8000, 0, 0);
        sheng_exit_note_images(&work, 1);
        assert(!output_used && !work.image_count);
    }
}

static void test_without_text_segment_nothing_is_recorded(void)
{
    image_with_crash_info("x");
    memcpy(memory + 32 + 8, "__NOPE", 6);
    frames(BASE + 0x8000, 0, 0);
    sheng_exit_note_images(&work, 1);
    assert(!output_used && !work.image_count);
}

static void test_frame_rows(void)
{
    reset();
    memset(&work, 0, sizeof(work));
    for (unsigned i = 0; i < 7; i++)
        work.frames[i] = 0x1000 + i;
    sheng_exit_print_frames(&work, 7);
    assert(!strcmp(output, "SHENG-EXIT:   0x1000 0x1001 0x1002 0x1003 0x1004 0x1005\nSHENG-EXIT:   0x1006\n"));
}

static void test_scan_is_bounded(void)
{
    // Every page readable and none a header: the scan stops at its limit.
    reset();
    everything_readable = 1;
    uint64_t base = 0;
    assert(!sheng_exit_find_base(BASE + MEMORY_SIZE - 1, &base));
    assert(reads == SHENG_EXIT_SCAN_PAGES);
    everything_readable = 0;
}

int main(void)
{
    test_describes_one_image_once();
    test_process_text_cannot_imitate_a_line();
    test_long_text_is_bounded();
    test_unmapped_and_absent_headers_are_quiet();
    test_bad_load_commands_are_refused();
    test_without_text_segment_nothing_is_recorded();
    test_frame_rows();
    test_scan_is_bounded();
    return 0;
}
