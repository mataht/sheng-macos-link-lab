// SPDX-License-Identifier: GPL-3.0-or-later
// Host test of platform/sheng/macos26/power/rpmh_readout.h against synthetic command database and register images.
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <map>
#include <vector>
#include "../platform/sheng/macos26/power/rpmh_readout.h"

using namespace sheng_memperf;

#define CHECK(x) do { if (!(x)) { std::printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #x); std::exit(1); } } while (0)

static void put16(std::vector<uint8_t> &d, size_t at, uint16_t v) { d[at] = v & 0xff; d[at + 1] = v >> 8; }
static void put32(std::vector<uint8_t> &d, size_t at, uint32_t v) { put16(d, at, v & 0xffff); put16(d, at + 2, v >> 16); }

// One slave (BCM, id 5) with entries at offset 0 and data at offset 0x100, both counted from byte 144.
static std::vector<uint8_t> database()
{
    std::vector<uint8_t> d(0x400);
    put32(d, 0, 1);
    put32(d, 4, kCmdDbMagic);
    put16(d, 8, 5);       // slave id
    put16(d, 8 + 2, 0);   // header offset
    put16(d, 8 + 4, 0x100);
    put16(d, 8 + 6, 2);   // entries
    const size_t first = 144;
    std::memcpy(&d[first], "MC0", 3);
    put32(d, first + 16, 0x50010);
    put16(d, first + 20, 8);
    put16(d, first + 22, 0);
    std::memcpy(&d[first + 24], "CN0", 3);
    put32(d, first + 24 + 16, 0x50030);
    put16(d, first + 24 + 20, 0);
    put16(d, first + 24 + 22, 0);
    put32(d, 144 + 0x100, 1000);    // unit
    put16(d, 144 + 0x100 + 4, 4);   // width
    d[144 + 0x100 + 6] = 2;         // vcd
    return d;
}

struct Regs {
    std::map<uint32_t, uint32_t> values;
    uint32_t read(uint32_t offset) { return values[offset]; }
};

int main()
{
    {   // the command database
        std::vector<uint8_t> db = database();
        Resource r;
        CHECK(cmdDbFind(db.data(), db.size(), "MC0", &r) == DbStatus::ok);
        CHECK(r.address == 0x50010 && r.hasAux && r.unit == 1000 && r.width == 4 && r.vcd == 2);
        Resource n;
        CHECK(cmdDbFind(db.data(), db.size(), "CN0", &n) == DbStatus::ok && n.address == 0x50030 && !n.hasAux);
        CHECK(cmdDbFind(db.data(), db.size(), "ACV", &r) == DbStatus::missing);
        std::vector<uint8_t> broken = db;
        put32(broken, 4, 0);
        CHECK(cmdDbFind(broken.data(), broken.size(), "MC0", &r) == DbStatus::noDb);
        CHECK(cmdDbFind(db.data(), 100, "MC0", &r) == DbStatus::noDb);
        broken = db;
        put16(broken, 8 + 6, 0xffff);   // entries past the end
        CHECK(cmdDbFind(broken.data(), broken.size(), "MC0", &r) == DbStatus::corrupt);
    }
    {   // the two RSC layouts
        const RscLayout v2 = rscLayout(0x00020000), v3 = rscLayout(0x00030000);
        CHECK(v2.control == 0x14 && v2.cmdEnable == 0x1c && v2.cmdAddr == 0x34 && v2.cmdStride == 20);
        CHECK(v3.control == 0x24 && v3.cmdEnable == 0x2c && v3.cmdAddr == 0x38 && v3.cmdStride == 24);
    }
    {   // BCM command decode
        const BcmVote v = bcmVote(1u << 30 | 1u << 29 | 0x123u << 14 | 0x456u);
        CHECK(v.commit && v.valid && v.x == 0x123 && v.y == 0x456);
        CHECK(!bcmVote(0).valid);
    }
    {   // L3 table: source 1 rows are multiples of 19.2 MHz, source 0 is GPLL0/2, two equal rows end it
        Regs r;
        r.values[kL3Lut + 0] = 0u << 30 | 1;
        r.values[kL3Lut + 4] = 1u << 30 | 52;
        r.values[kL3Lut + 8] = 1u << 30 | 100;
        r.values[kL3Lut + 12] = 1u << 30 | 100;
        uint32_t top = 0;
        CHECK(lutLevels(r, &top) == 3 && top == 100 * kXoKHz);
        CHECK(lutKHz(0) == kGpllHalfKHz);
    }
    std::printf("PASS\n");
    return 0;
}
