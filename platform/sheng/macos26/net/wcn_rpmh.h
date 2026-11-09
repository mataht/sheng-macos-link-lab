/* SPDX-License-Identifier: BSD-2-Clause-Patent */
/* Copyright (c) 2026, UniXNU contributors. All rights reserved. (the UniXNU-sheng license text is in LICENSES/ and THIRD_PARTY_NOTICES)
 *
 * The cmd-db lookup and the TCS register writes of one RPMh active request, as C++ inline functions.
 * Imported unchanged in substance from UniXNU-sheng package/QcomRpmh.c and QcomRpmh.h at commit a68ee4ee67cc71792fbf519de7e3e28072c717b5.
 * Only the form differs: inline functions in a namespace, so the storage companion builds one more C++ source and no C source.
 * Nothing here touches hardware: the caller applies the writes. */
#ifndef SHENG_WCN_RPMH_H
#define SHENG_WCN_RPMH_H

#include <stddef.h>
#include <stdint.h>

namespace sheng_rpmh {

constexpr uint32_t kCmdDbMagic = 0x0c0330dbu;
constexpr unsigned kMaxLevels = 16;

struct Resource {
    uint32_t address = 0;
    uint16_t levels[kMaxLevels] = {};
    unsigned levelCount = 0;
};

enum { Ok = 0, NoDb = -1, Missing = -2, Corrupt = -3, Argument = -5 };

inline uint16_t le16(const uint8_t *p) { return static_cast<uint16_t>(p[0] | static_cast<uint16_t>(p[1]) << 8); }
inline uint32_t le32(const uint8_t *p) { return static_cast<uint32_t>(le16(p)) | static_cast<uint32_t>(le16(p + 2)) << 16; }
inline bool fits(size_t at, size_t length, size_t size) { return at <= size && length <= size - at; }

/* Header: u32 version, u32 magic, 8 slave records of 16 bytes (u16 id, header offset, data offset, entry count, version, reserved[3]),
 * u32 checksum, u32 reserved, then the data area the offsets count from (byte 144). Entries are 24 bytes: an 8-byte id, two u32
 * priorities, u32 address, u16 aux length, u16 aux offset. */
inline int cmdDbFind(const uint8_t *db, size_t bytes, const char *name, Resource *out)
{
    if (!db || !name || !out)
        return Argument;
    if (bytes < 144 || le32(db + 4) != kCmdDbMagic)
        return NoDb;
    char id[8] = {0};
    for (unsigned i = 0; i < 8 && name[i]; ++i)
        id[i] = name[i];
    for (unsigned s = 0; s < 8; ++s) {
        const uint8_t *slave = db + 8 + 16 * s;
        if (!le16(slave))
            break;
        const size_t heads = 144u + le16(slave + 2), data = 144u + le16(slave + 4), count = le16(slave + 6);
        if (!fits(heads, count * 24, bytes))
            return Corrupt;
        for (size_t e = 0; e < count; ++e) {
            const uint8_t *entry = db + heads + 24 * e;
            bool same = true;
            for (unsigned i = 0; i < 8 && same; ++i)
                same = entry[i] == static_cast<uint8_t>(id[i]);
            if (!same)
                continue;
            Resource r;
            r.address = le32(entry + 16);
            const size_t length = le16(entry + 20), at = data + le16(entry + 22);
            if (!r.address || !fits(at, length, bytes))
                return Corrupt;
            for (size_t l = 0; l + 1 < length && r.levelCount < kMaxLevels; l += 2) {
                const uint16_t level = le16(db + at + l);
                if (r.levelCount && !level)
                    break; /* the list is zero padded */
                r.levels[r.levelCount++] = level;
            }
            *out = r;
            return Ok;
        }
    }
    return Missing;
}

struct Write {
    uint32_t offset, value;
};

/* The register layout of an RSC DRV; offsets are relative to the TCS block (DRV base + tcs-offset). */
struct Layout {
    uint32_t tcsStride, cmdStride, irqStatus, irqClear, waitForCmpl, control, status, cmdEnable, cmdMsgid, cmdAddr, cmdData;
};

/* The layout for a DRV whose id register reads drvId (major version in bits 23:16). */
inline Layout rscLayout(uint32_t drvId)
{
    if (((drvId >> 16) & 0xff) >= 3)
        return Layout{672, 24, 0x04, 0x08, 0x20, 0x24, 0x28, 0x2c, 0x34, 0x38, 0x3c};
    return Layout{672, 20, 0x04, 0x08, 0x10, 0x14, 0x18, 0x1c, 0x30, 0x34, 0x38};
}

constexpr uint32_t kMsgidWriteResponse = 8u | 1u << 8 | 1u << 16; /* 4-byte payload code, response requested, write */
constexpr uint32_t kControlActiveMode = 1u << 16, kControlTrigger = 1u << 24;

/* The writes of an active request of count (<= 4) data commands in TCS tcs, in order, ending with the trigger. Returns the number of
 * writes, 0 on bad arguments. */
inline unsigned activeRequest(const Layout &l, unsigned tcs, const uint32_t *addresses, const uint32_t *values, unsigned count, Write *w,
                              unsigned capacity)
{
    if (!count || count > 4 || capacity < 3 * count + 6)
        return 0;
    const uint32_t base = l.tcsStride * tcs;
    unsigned n = 0;
    /* The control register is cleared before the TCS is reprogrammed, then set to active mode and only after that triggered. */
    w[n++] = Write{base + l.control, 0};
    w[n++] = Write{l.irqClear, 1u << tcs};
    for (unsigned c = 0; c < count; ++c) {
        const uint32_t cmd = base + l.cmdStride * c;
        w[n++] = Write{cmd + l.cmdMsgid, kMsgidWriteResponse};
        w[n++] = Write{cmd + l.cmdAddr, addresses[c]};
        w[n++] = Write{cmd + l.cmdData, values[c]};
    }
    const uint32_t mask = (1u << count) - 1;
    w[n++] = Write{base + l.waitForCmpl, mask};
    w[n++] = Write{base + l.cmdEnable, mask};
    w[n++] = Write{base + l.control, kControlActiveMode};
    w[n++] = Write{base + l.control, kControlActiveMode | kControlTrigger};
    return n;
}

/* The writes that return the TCS to idle after its done interrupt. */
inline unsigned finish(const Layout &l, unsigned tcs, Write *w, unsigned capacity)
{
    if (capacity < 3)
        return 0;
    const uint32_t base = l.tcsStride * tcs;
    w[0] = Write{l.irqClear, 1u << tcs};
    w[1] = Write{base + l.control, 0};
    w[2] = Write{base + l.cmdEnable, 0};
    return 3;
}

}  // namespace sheng_rpmh

#endif
