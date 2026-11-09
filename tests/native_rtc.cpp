// SPDX-License-Identifier: GPL-3.0-or-later
// Host test of platform/sheng/macos26/power/native_rtc.h against a simulated SPMI arbiter v7.
#include <cstdio>
#include <cstdlib>
#include <map>
#include <vector>
#include "../platform/sheng/macos26/power/native_rtc.h"

using namespace sheng_keys;
using sheng_rtc::Source;

#define CHECK(x) do { if (!(x)) { std::printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #x); std::exit(1); } } while (0)

struct Sim : Mmio {
    std::vector<uint32_t> coreRegs = std::vector<uint32_t>(0x3000 / 4), cnfgRegs = std::vector<uint32_t>(0x4000 / 4);
    std::map<uint32_t, uint32_t> obs;
    std::map<uint32_t, uint8_t> pmic;
    std::map<int, uint32_t> apidPpid;
    uint32_t statusOverride = 0;
    unsigned reads = 0, tickAtRead = 0;   // when nonzero, the counter moves by one just before that data read
    uint32_t counter = 0;
    uint32_t core(uint32_t o) override { return coreRegs[o / 4]; }
    uint32_t cnfg(uint32_t o) override { return cnfgRegs[o / 4]; }
    uint32_t readObs(uint32_t o) override
    {
        if (o % 0x20 == kStatus)
            return statusOverride ? statusOverride : 1;
        return obs[o];
    }
    void writeObs(uint32_t o, uint32_t v) override
    {
        CHECK(o % 0x20 == kCmd);
        CHECK(v >> 27 == kOpExtReadLong && (v & 7) == 0);   // only single-byte reads, never a write opcode
        const int apid = static_cast<int>((o % 0x8000) / 0x20);
        const uint32_t ppid = apidPpid.at(apid), reg = (v >> 4) & 0xff;
        CHECK(ppid == (0u << 8 | 0x61));
        uint8_t value = pmic[reg];
        if (reg >= 0x48 && reg <= 0x4b) {
            if (tickAtRead && ++reads == tickAtRead)
                ++counter;
            value = static_cast<uint8_t>(counter >> (8 * (reg - 0x48)));
        }
        obs[o - kCmd + kRdata0] = value;
    }
    void delayMicroseconds(unsigned) override {}
};

static void setUp(Sim &s)
{
    s.coreRegs[0] = 0x70000000;
    s.coreRegs[kFeatures / 4] = 16;
    // APID 3 is SID 0 peripheral 0x13 (the keys), APID 5 the RTC.
    s.coreRegs[(kApidMapBase + 4 * 3) / 4] = 0x13u << 8 | 1u << 24;
    s.coreRegs[(kApidMapBase + 4 * 5) / 4] = 0x61u << 8 | 1u << 24;
    s.apidPpid[3] = 0x13;
    s.apidPpid[5] = 0x61;
    s.pmic[0x46] = 0x80;
}

static Arbiter arbiter(Sim &s)
{
    Arbiter a;
    a.io = &s;
    a.coreBytes = 0x3000;
    a.cnfgBytes = 0x4000;
    a.obsBytes = 0x8000;
    a.ee = 0;
    CHECK(probe(a) == Status::ok);
    return a;
}

int main()
{
    {   // channel lookup and a plain little-endian read
        Sim s;
        setUp(s);
        Arbiter a = arbiter(s);
        const int apid = sheng_rtc::channel(a);
        CHECK(apid == 5);
        s.counter = 0x12345678;
        const sheng_rtc::Reading r = sheng_rtc::read(a, apid);
        CHECK(r.status == Status::ok && r.enabled && r.seconds == 0x12345678 && r.attempts == 2);
    }
    {   // a tick inside the first complete read needs one more pass; the value is the later one
        Sim s;
        setUp(s);
        Arbiter a = arbiter(s);
        s.counter = 0x000000ff;
        s.tickAtRead = 2;
        const sheng_rtc::Reading r = sheng_rtc::read(a, sheng_rtc::channel(a));
        CHECK(r.status == Status::ok && r.seconds == 0x100 && r.attempts == 3);
    }
    {   // the enable bit is reported and a disabled RTC does not produce a date
        Sim s;
        setUp(s);
        Arbiter a = arbiter(s);
        s.pmic[0x46] = 0;
        s.counter = 1000;
        const sheng_rtc::Reading r = sheng_rtc::read(a, sheng_rtc::channel(a));
        CHECK(r.status == Status::ok && !r.enabled);
        sheng_rtc::Policy p;
        p.haveOffset = true;
        p.offset = 5;
        CHECK(sheng_rtc::wallClock(r, p).source == Source::none);
        p.floor = 77;
        CHECK(sheng_rtc::wallClock(r, p).source == Source::floor);
    }
    {   // an arbiter error stops at once and is reported; missing channel reports not found
        Sim s;
        setUp(s);
        Arbiter a = arbiter(s);
        s.statusOverride = 1 | 4;
        CHECK(sheng_rtc::read(a, sheng_rtc::channel(a)).status == Status::denied);
        CHECK(sheng_rtc::read(a, -1).status == Status::notFound);
    }
    {   // the wall clock policy
        sheng_rtc::Reading r;
        r.status = Status::ok;
        r.enabled = true;
        r.seconds = 100000;
        sheng_rtc::Policy p;
        // no offset, no floor: a raw count is not a date
        CHECK(sheng_rtc::wallClock(r, p).source == Source::none && sheng_rtc::wallClock(r, p).seconds == 0);
        p.floor = 1794180000;
        sheng_rtc::Time t = sheng_rtc::wallClock(r, p);
        CHECK(t.source == Source::floor && t.seconds == 1794180000);
        p.haveOffset = true;
        p.offset = 1794000000;
        t = sheng_rtc::wallClock(r, p);
        CHECK(t.source == Source::floor && t.seconds == 1794180000);   // RTC + offset earlier than the floor
        p.offset = 1794100000 + 200000;
        t = sheng_rtc::wallClock(r, p);
        CHECK(t.source == Source::rtcPlusOffset && t.seconds == 1794400000);
        p.floor = 0;
        p.offset = -200000;   // negative result is not a date
        CHECK(sheng_rtc::wallClock(r, p).source == Source::none);
        r.status = Status::timeout;
        p.offset = 1794100000;
        CHECK(sheng_rtc::wallClock(r, p).source == Source::none);
    }
    std::printf("PASS\n");
    return 0;
}
