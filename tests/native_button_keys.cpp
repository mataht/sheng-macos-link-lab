// SPDX-License-Identifier: GPL-3.0-or-later
// Host test of platform/sheng/macos26/input/spmi_keys.h against a simulated SPMI arbiter v7.
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <map>
#include <vector>
#include "../platform/sheng/macos26/input/spmi_keys.h"

using namespace sheng_keys;

#define CHECK(x) do { if (!(x)) { std::printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #x); std::exit(1); } } while (0)

struct Sim : Mmio {
    std::vector<uint32_t> coreRegs = std::vector<uint32_t>(0x3000 / 4), cnfgRegs = std::vector<uint32_t>(0x4000 / 4);
    std::map<uint32_t, uint32_t> obs;      // channel register writes / reads
    std::map<uint32_t, uint8_t> pmic;      // (ppid << 8 | reg) -> value
    std::map<int, uint32_t> apidPpid;      // apid -> ppid
    uint32_t statusOverride = 0;           // when nonzero, returned as STATUS
    unsigned busyPolls = 0, delays = 0;
    std::vector<uint32_t> commands;
    uint32_t core(uint32_t o) override { CHECK(o % 4 == 0 && o / 4 < coreRegs.size()); return coreRegs[o / 4]; }
    uint32_t cnfg(uint32_t o) override { CHECK(o % 4 == 0 && o / 4 < cnfgRegs.size()); return cnfgRegs[o / 4]; }
    uint32_t readObs(uint32_t o) override
    {
        const uint32_t reg = o % 0x20;
        if (reg == kStatus) {
            if (busyPolls) { --busyPolls; return 0; }
            return statusOverride ? statusOverride : 1;
        }
        return obs[o];
    }
    void writeObs(uint32_t o, uint32_t v) override
    {
        CHECK(o % 0x20 == kCmd);
        commands.push_back(v);
        const int apid = static_cast<int>((o % 0x8000) / 0x20);
        CHECK(v >> 27 == kOpExtReadLong && (v & 7) == 0);
        const uint32_t ppid = apidPpid.at(apid), reg = (v >> 4) & 0xff;
        obs[o - kCmd + kRdata0] = pmic[ppid << 8 | reg];
    }
    void delayMicroseconds(unsigned) override { ++delays; }
    void map(int apid, uint16_t sid, uint16_t periph, uint32_t ownerEe)
    {
        const uint32_t ppid = static_cast<uint32_t>(sid) << 8 | periph;
        coreRegs[(kApidMapBase + 4 * apid) / 4] = ppid << 8 | 1u << 24;
        cnfgRegs[apid] = ownerEe;
        apidPpid[apid] = ppid;
    }
};

static Arbiter arbiter(Sim &s)
{
    Arbiter a;
    a.io = &s;
    a.coreBytes = 0x3000;
    a.cnfgBytes = 0x4000;
    a.obsBytes = 0x80000;
    a.ee = 0;
    return a;
}

int main()
{
    // Version gating.
    {
        Sim s;
        s.coreRegs[0] = 0x50000000;
        s.coreRegs[1] = 40;
        Arbiter a = arbiter(s);
        CHECK(probe(a) == Status::unsupported);
        s.coreRegs[0] = 0x80000000;
        CHECK(probe(a) == Status::unsupported);
        s.coreRegs[0] = 0x70010000;
        CHECK(probe(a) == Status::ok && a.apids == 40);
        s.coreRegs[1] = 0x7ff;
        CHECK(probe(a) == Status::ok && a.apids == (0x3000 - kApidMapBase) / 4);
    }
    // APID selection, reads, command encoding and key sampling.
    {
        Sim s;
        s.coreRegs[0] = 0x70010000;
        s.coreRegs[1] = 64;
        s.map(5, 0, 0x13, 2);      // PON, owned by another EE
        s.map(9, 0, 0x13, 0);      // PON again, owned by EE 0: preferred
        s.map(12, 1, 0x8d, 3);     // PM8550 GPIO6, only entry
        s.map(13, 1, 0x8c, 0);     // GPIO5: must not be picked
        Arbiter a = arbiter(s);
        CHECK(probe(a) == Status::ok);
        Channels c = resolve(a);
        CHECK(c.pon == 9 && c.gpio == 12);
        CHECK(findApid(a, 7, 0x42) == -1);
        s.pmic[0x13 << 8 | 0x10] = 0xc0;      // power and resin
        s.pmic[(1u << 8 | 0x8d) << 8 | 0x10] = 0x00;   // vol-up pulled low = pressed
        Keys k;
        Status ps, gs;
        CHECK(sample(a, c, &k, &ps, &gs) && ps == Status::ok && gs == Status::ok);
        CHECK(k.power && k.volumeDown && k.volumeUp);
        CHECK(s.commands.size() == 2 && s.commands[0] == (1u << 27 | 0x10u << 4));
        s.pmic[0x13 << 8 | 0x10] = 0x00;
        s.pmic[(1u << 8 | 0x8d) << 8 | 0x10] = 0x01;
        CHECK(sample(a, c, &k, &ps, &gs) && !k.power && !k.volumeDown && !k.volumeUp);
        // Busy polls then done; failure, denied, dropped and timeout.
        uint8_t v = 0;
        s.busyPolls = 7;
        CHECK(readByte(a, c.pon, 0x1310, &v) == Status::ok && s.delays == 7);
        s.statusOverride = 1 | 2;
        CHECK(readByte(a, c.pon, 0x1310, &v) == Status::failure);
        s.statusOverride = 1 | 4;
        CHECK(readByte(a, c.pon, 0x1310, &v) == Status::denied);
        s.statusOverride = 1 | 8;
        CHECK(readByte(a, c.pon, 0x1310, &v) == Status::dropped);
        s.statusOverride = 0;
        s.busyPolls = kPollLimit + 5;
        CHECK(readByte(a, c.pon, 0x1310, &v) == Status::timeout);
        s.busyPolls = 0;
        CHECK(readByte(a, -1, 0x1310, &v) == Status::notFound);
        // A failed PON read reports no keys; a missing GPIO channel leaves volume-up released.
        s.statusOverride = 1 | 4;
        CHECK(!sample(a, c, &k, &ps, &gs) && ps == Status::denied);
        s.statusOverride = 0;
        Channels noGpio = c;
        noGpio.gpio = -1;
        s.pmic[0x13 << 8 | 0x10] = 0x80;
        CHECK(sample(a, noGpio, &k, &ps, &gs) && gs == Status::notFound && k.power && !k.volumeUp);
    }
    // Observer bound: a channel outside the mapped observer window is refused.
    {
        Sim s;
        s.coreRegs[0] = 0x70010000;
        s.coreRegs[1] = 64;
        s.map(60, 0, 0x13, 0);
        Arbiter a = arbiter(s);
        a.obsBytes = 0x20 * 10;
        CHECK(probe(a) == Status::ok && findApid(a, 0, 0x13) == -1);
    }
    // Debounce and reports.
    {
        Debounce d;
        Keys up;
        up.volumeUp = true;
        CHECK(!d.update(up));            // first sight
        CHECK(d.update(up));             // confirmed
        CHECK(!d.update(up));            // unchanged
        Keys none;
        CHECK(!d.update(none) && d.update(none));
        uint8_t r[8];
        report(up, r);
        CHECK(r[0] == 0 && r[1] == 0 && r[2] == kUsageUp && r[3] == 0);
        Keys both = up;
        both.volumeDown = true;
        both.power = true;
        report(both, r);
        CHECK(r[2] == kUsageTab && r[3] == kUsageReturn && r[4] == 0);
        Keys down;
        down.volumeDown = true;
        report(down, r);
        CHECK(r[2] == kUsageDown);
        report(none, r);
        for (unsigned i = 0; i < 8; ++i) CHECK(r[i] == 0);
    }
    std::printf("PASS\n");
    return 0;
}
