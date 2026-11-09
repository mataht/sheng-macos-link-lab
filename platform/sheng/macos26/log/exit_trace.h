// SPDX-License-Identifier: GPL-3.0-or-later
/*
 * Default-off, QEMU-only diagnostic (sheng-exit-trace=1): what a dying WindowServer was doing.
 *
 * A process that calls abort() leaves its reason in user memory: the return addresses on its stacks and the text libraries put in their __crash_info
 * sections. Crash reports normally carry that, but under QEMU none get written. So this prints the same facts on the kernel log, as the process exits.
 *
 * Every read of user memory goes through sheng_exit_read() and sheng_exit_read_string(), which the file including this header defines, so a host test
 * drives this same code. All sizes come from the process being traced and are checked before use.
 */
#pragma once

#define SHENG_EXIT_MH_MAGIC_64 0xfeedfacfu
#define SHENG_EXIT_CPU_ARM64 0x0100000cu
#define SHENG_EXIT_MH_EXECUTE 2u
#define SHENG_EXIT_MH_DYLIB 6u
#define SHENG_EXIT_MH_DYLINKER 7u
#define SHENG_EXIT_MH_BUNDLE 8u
#define SHENG_EXIT_LC_SEGMENT_64 0x19u
#define SHENG_EXIT_LC_UUID 0x1bu
#define SHENG_EXIT_HEADER_SIZE 32u
#define SHENG_EXIT_SEGMENT_SIZE 72u
#define SHENG_EXIT_SECTION_SIZE 80u
#define SHENG_EXIT_UUID_COMMAND_SIZE 24u
#define SHENG_EXIT_PAGE 0x4000ull
#define SHENG_EXIT_SCAN_PAGES 0x2000u   /* 128 MiB below a return address */
#define SHENG_EXIT_MAX_COMMANDS 0x8000u
#define SHENG_EXIT_IMAGES 32u
#define SHENG_EXIT_CRASH_INFOS 4u
#define SHENG_EXIT_FRAMES 64u
#define SHENG_EXIT_TEXT 240u
#define SHENG_EXIT_ROW 6u

struct sheng_exit_image {
    uint64_t base;
    uint64_t size;
};

struct sheng_exit_work {
    struct sheng_exit_image images[SHENG_EXIT_IMAGES];
    unsigned image_count;
    uint8_t commands[SHENG_EXIT_MAX_COMMANDS];
    char text[SHENG_EXIT_TEXT + 1];
    uintptr_t frames[SHENG_EXIT_FRAMES];
};

static uint32_t sheng_exit_u32(const uint8_t *bytes)
{
    uint32_t value;
    memcpy(&value, bytes, sizeof(value));
    return value;
}

static uint64_t sheng_exit_u64(const uint8_t *bytes)
{
    uint64_t value;
    memcpy(&value, bytes, sizeof(value));
    return value;
}

// The page holding a Mach-O header at or below a return address: images start page-aligned and their text is contiguous.
static int sheng_exit_find_base(uint64_t return_address, uint64_t *base)
{
    uint64_t candidate = return_address & ~(SHENG_EXIT_PAGE - 1);
    for (unsigned i = 0; i < SHENG_EXIT_SCAN_PAGES && candidate; i++, candidate -= SHENG_EXIT_PAGE) {
        uint8_t header[16];
        if (sheng_exit_read(candidate, header, sizeof(header)))
            return 0;
        uint32_t filetype = sheng_exit_u32(header + 12);
        if (sheng_exit_u32(header) == SHENG_EXIT_MH_MAGIC_64 && sheng_exit_u32(header + 4) == SHENG_EXIT_CPU_ARM64 &&
            (filetype == SHENG_EXIT_MH_EXECUTE || filetype == SHENG_EXIT_MH_DYLIB || filetype == SHENG_EXIT_MH_DYLINKER || filetype == SHENG_EXIT_MH_BUNDLE)) {
            *base = candidate;
            return 1;
        }
    }
    return 0;
}

// Printing a process's own text on the log: a quote or a line break must not be able to imitate another line.
static void sheng_exit_clean_text(char *text)
{
    for (; *text; text++) {
        if (*text < 0x20 || *text == 0x7f || *text == '"')
            *text = '.';
    }
}

static void sheng_exit_crash_info(struct sheng_exit_work *work, uint64_t base, uint64_t address)
{
    static const char *const names[] = { "message", "signature", "backtrace", "message2" };
    uint8_t annotation[40];
    if (sheng_exit_read(address, annotation, sizeof(annotation)))
        return;
    for (unsigned i = 0; i < 4; i++) {
        uint64_t text_address = sheng_exit_u64(annotation + 8 + 8 * i);
        if (!text_address)
            continue;
        memset(work->text, 0, sizeof(work->text));
        if (sheng_exit_read_string(text_address, work->text, sizeof(work->text)))
            continue;
        work->text[SHENG_EXIT_TEXT] = 0;
        sheng_exit_clean_text(work->text);
        SHENG_EXIT_PRINT("SHENG-EXIT: crash-info image 0x%llx %s \"%s\"\n", (unsigned long long)base, names[i], work->text);
    }
}

// Reads one image's load commands, prints its identity and any __crash_info text, and remembers its text range.
static void sheng_exit_describe_image(struct sheng_exit_work *work, uint64_t base)
{
    uint8_t header[SHENG_EXIT_HEADER_SIZE];
    if (sheng_exit_read(base, header, sizeof(header)))
        return;
    uint32_t command_bytes = sheng_exit_u32(header + 20);
    if (command_bytes > SHENG_EXIT_MAX_COMMANDS || sheng_exit_read(base + sizeof(header), work->commands, command_bytes))
        return;

    uint64_t text_address = 0, text_size = 0, crash_infos[SHENG_EXIT_CRASH_INFOS];
    unsigned crash_info_count = 0;
    int have_text = 0, have_uuid = 0;
    uint8_t uuid[16];
    uint32_t position = 0;
    while (command_bytes - position >= 8) {
        const uint8_t *command = work->commands + position;
        uint32_t command_size = sheng_exit_u32(command + 4);
        if (command_size < 8 || command_size > command_bytes - position)
            return;
        if (sheng_exit_u32(command) == SHENG_EXIT_LC_UUID && command_size >= SHENG_EXIT_UUID_COMMAND_SIZE) {
            memcpy(uuid, command + 8, sizeof(uuid));
            have_uuid = 1;
        } else if (sheng_exit_u32(command) == SHENG_EXIT_LC_SEGMENT_64 && command_size >= SHENG_EXIT_SEGMENT_SIZE) {
            if (!strncmp((const char *)command + 8, "__TEXT", 16)) {
                text_address = sheng_exit_u64(command + 24);
                text_size = sheng_exit_u64(command + 32);
                have_text = 1;
            }
            uint32_t sections = sheng_exit_u32(command + 64);
            if (sections > (command_size - SHENG_EXIT_SEGMENT_SIZE) / SHENG_EXIT_SECTION_SIZE)
                return;
            for (uint32_t i = 0; i < sections && crash_info_count < SHENG_EXIT_CRASH_INFOS; i++) {
                const uint8_t *section = command + SHENG_EXIT_SEGMENT_SIZE + i * SHENG_EXIT_SECTION_SIZE;
                if (!strncmp((const char *)section, "__crash_info", 16))
                    crash_infos[crash_info_count++] = sheng_exit_u64(section + 32);
            }
        }
        position += command_size;
    }
    if (!have_text)
        return;

    char uuid_text[33] = "";
    if (have_uuid) {
        static const char digits[] = "0123456789abcdef";
        for (unsigned i = 0; i < 16; i++) {
            uuid_text[2 * i] = digits[uuid[i] >> 4];
            uuid_text[2 * i + 1] = digits[uuid[i] & 15];
        }
    }
    uint64_t slide = base - text_address;
    SHENG_EXIT_PRINT("SHENG-EXIT: image 0x%llx size 0x%llx slide 0x%llx uuid %s\n", (unsigned long long)base, (unsigned long long)text_size, (unsigned long long)slide, uuid_text);
    if (work->image_count < SHENG_EXIT_IMAGES) {
        work->images[work->image_count].base = base;
        work->images[work->image_count].size = text_size;
        work->image_count++;
    }
    for (unsigned i = 0; i < crash_info_count; i++)
        sheng_exit_crash_info(work, base, crash_infos[i] + slide);
}

static void sheng_exit_note_images(struct sheng_exit_work *work, unsigned count)
{
    for (unsigned i = 0; i < count && work->image_count < SHENG_EXIT_IMAGES; i++) {
        uint64_t return_address = work->frames[i], base = 0;
        int known = 0;
        for (unsigned j = 0; j < work->image_count && !known; j++)
            known = return_address >= work->images[j].base && return_address - work->images[j].base < work->images[j].size;
        if (!known && sheng_exit_find_base(return_address, &base))
            sheng_exit_describe_image(work, base);
    }
}

static void sheng_exit_print_frames(const struct sheng_exit_work *work, unsigned count)
{
    for (unsigned i = 0; i < count; i += SHENG_EXIT_ROW) {
        SHENG_EXIT_PRINT("SHENG-EXIT:  ");
        for (unsigned j = i; j < count && j < i + SHENG_EXIT_ROW; j++)
            SHENG_EXIT_PRINT(" 0x%llx", (unsigned long long)work->frames[j]);
        SHENG_EXIT_PRINT("\n");
    }
}
