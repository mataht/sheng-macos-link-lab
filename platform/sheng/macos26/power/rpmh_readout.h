/* SPDX-License-Identifier: BSD-2-Clause-Patent */
#ifndef SHENG_RPMH_READOUT_H
#define SHENG_RPMH_READOUT_H

/*
 * Read-only views of SM8550 performance state for NativeMemPerf: the AOP command database (which RPMh address is which bus
 * resource and how its votes scale), the application processor's RSC TCS registers (the last commands programmed there) and the
 * EPSS L3 frequency table. Pure logic over byte and register accessors so the host test can drive synthetic data; no IOKit here.
 *
 * Sources: UniXNU-sheng package/QcomRpmh.c (BSD-2-Clause-Patent, upstream at 2b7a5815 or later) for the database layout and the two
 * RSC register layouts; Linux drivers/interconnect/qcom (bcm-voter.c, icc-rpmh.h) for the BCM aux record and command encoding;
 * Linux drivers/interconnect/qcom/osm-l3.c ("qcom,epss-l3": enable 0x0, vote 0x90, 40-row LUT at 0x100); upstream
 * package/CpuFreqProbe.cpp for the LUT row format. Nothing here writes a register.
 */

#include <stddef.h>
#include <stdint.h>

namespace sheng_memperf {

constexpr uint32_t kCmdDbMagic = 0x0c0330db;

inline uint16_t le16(const uint8_t *p) { return static_cast<uint16_t>(p[0] | static_cast<uint16_t>(p[1]) << 8); }
inline uint32_t le32(const uint8_t *p) { return static_cast<uint32_t>(le16(p)) | static_cast<uint32_t>(le16(p + 2)) << 16; }
inline bool fits(size_t at, size_t length, size_t size) { return at <= size && length <= size - at; }

enum class DbStatus : int { ok = 0, noDb = -1, missing = -2, corrupt = -3 };

struct Resource {
    uint32_t address = 0;
    uint32_t unit = 0;    // BCM aux record: vote unit
    uint16_t width = 0;   // BCM aux record: bus width
    uint8_t vcd = 0;      // BCM aux record: voltage corner domain
    bool hasAux = false;
};

// Header: u32 version, u32 magic, 8 slave records of 16 bytes (u16 id, header offset, data offset, entry count, ...), u32 checksum,
// u32 reserved; offsets count from byte 144. Entries are 24 bytes: 8-byte id, two u32 priorities, u32 address, u16 aux length, u16 aux offset.
inline DbStatus cmdDbFind(const uint8_t *db, size_t bytes, const char *name, Resource *out)
{
    if (!db || bytes < 144 || le32(db + 4) != kCmdDbMagic)
        return DbStatus::noDb;
    char id[8] = {0};
    for (unsigned i = 0; i < 8 && name[i]; ++i)
        id[i] = name[i];
    for (unsigned s = 0; s < 8; ++s) {
        const uint8_t *slave = db + 8 + 16 * s;
        if (!le16(slave))
            break;
        const size_t heads = 144u + le16(slave + 2), data = 144u + le16(slave + 4), count = le16(slave + 6);
        if (!fits(heads, count * 24, bytes))
            return DbStatus::corrupt;
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
                return DbStatus::corrupt;
            if (length >= 8) {
                r.unit = le32(db + at);
                r.width = le16(db + at + 4);
                r.vcd = db[at + 6];
                r.hasAux = true;
            }
            *out = r;
            return DbStatus::ok;
        }
    }
    return DbStatus::missing;
}

struct RscLayout {
    uint32_t tcsStride, cmdStride, control, status, cmdEnable, cmdAddr, cmdData;
};

// Major version in bits 23:16 of the DRV id register; version 3 moved the TCS registers.
constexpr RscLayout rscLayout(uint32_t drvId)
{
    return ((drvId >> 16) & 0xff) >= 3 ? RscLayout{672, 24, 0x24, 0x28, 0x2c, 0x38, 0x3c}
                                       : RscLayout{672, 20, 0x14, 0x18, 0x1c, 0x34, 0x38};
}

struct BcmVote {
    bool commit, valid;
    uint16_t x, y;
};

constexpr BcmVote bcmVote(uint32_t data)
{
    return BcmVote{(data & (1u << 30)) != 0, (data & (1u << 29)) != 0, static_cast<uint16_t>((data >> 14) & 0x3fff),
                   static_cast<uint16_t>(data & 0x3fff)};
}

constexpr uint32_t kL3Enable = 0x0, kL3Vote = 0x90, kL3Lut = 0x100;
constexpr unsigned kLutEntries = 40;
constexpr uint32_t kXoKHz = 19200, kGpllHalfKHz = 300000;

constexpr uint32_t lutKHz(uint32_t row) { return (row >> 30) ? kXoKHz * (row & 0xff) : kGpllHalfKHz; }

// Reg must provide: uint32_t read(uint32_t offset). The table ends at two equal frequencies.
template <typename Reg>
inline unsigned lutLevels(Reg &r, uint32_t *topKHz)
{
    unsigned levels = 0;
    uint32_t previous = 0;
    *topKHz = 0;
    for (unsigned i = 0; i < kLutEntries; ++i) {
        const uint32_t khz = lutKHz(r.read(kL3Lut + 4 * i));
        if (i > 0 && khz == previous)
            break;
        previous = khz;
        levels = i + 1;
        if (khz > *topKHz)
            *topKHz = khz;
    }
    return levels;
}

}  // namespace sheng_memperf

#endif
