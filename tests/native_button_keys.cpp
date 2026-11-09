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
    // Chord window: a lone volume key waits kWindowPolls polls; the other joining makes it Tab only.
    {
        auto lone = [](bool up) { Keys k; k.volumeUp = up; k.volumeDown = !up; return k; };
        Keys none, both, power;
        both.volumeUp = both.volumeDown = true;
        power.power = true;
        Keys out[3];
        // Held down: nothing for three polls, Down on the fourth, released when let go.
        Chord c;
        for (unsigned i = 1; i < Chord::kWindowPolls; ++i) CHECK(c.update(lone(false), out) == 0);
        CHECK(c.update(lone(false), out) == 1 && sameKeys(out[0], lone(false)));
        CHECK(c.update(lone(false), out) == 0);
        CHECK(c.update(none, out) == 1 && sameKeys(out[0], none));
        // A quick tap inside the window: press and release in the same poll.
        CHECK(c.update(lone(true), out) == 0);
        CHECK(c.update(none, out) == 2 && sameKeys(out[0], lone(true)) && sameKeys(out[1], none));
        // Volume down, then volume up 40 ms later: Tab only, no arrow; Tab stays until both are up.
        CHECK(c.update(lone(false), out) == 0 && c.update(lone(false), out) == 0);
        CHECK(c.update(both, out) == 1 && sameKeys(out[0], both));
        CHECK(c.update(lone(true), out) == 0);    // one released: still the chord
        CHECK(c.update(none, out) == 1 && sameKeys(out[0], none));
        // Past the window the arrow is already down; the other key joining then gives Tab.
        for (unsigned i = 0; i < Chord::kWindowPolls; ++i) c.update(lone(true), out);
        CHECK(sameKeys(c.shown, lone(true)));
        CHECK(c.update(both, out) == 1 && sameKeys(out[0], both));
        CHECK(c.update(none, out) == 1);
        // Power is immediate, also while a volume key is held back.
        CHECK(c.update(power, out) == 1 && sameKeys(out[0], power));
        Keys powerDown = lone(false);
        powerDown.power = true;
        CHECK(c.update(powerDown, out) == 0);    // volume down held back, power unchanged
        CHECK(c.update(none, out) == 2);         // tap of volume down with power, then all up
        CHECK(sameKeys(out[0], powerDown) && sameKeys(out[1], none));
        // Switching from one lone key to the other inside the window: the first is a tap.
        CHECK(c.update(lone(true), out) == 0);
        CHECK(c.update(lone(false), out) == 2 && sameKeys(out[0], lone(true)) && sameKeys(out[1], none));
        uint8_t r[8];
        report(both, r);
        CHECK(r[2] == kUsageTab && r[3] == 0);
    }
    // Typing experiment: reports, accepted texts and the hold trigger.
    {
        uint8_t r[8];
        CHECK(typeReport('a', r) && r[0] == 0 && r[1] == 0 && r[2] == 0x04 && r[3] == 0);
        CHECK(typeReport('z', r) && r[0] == 0 && r[2] == 0x1d);
        CHECK(typeReport('Q', r) && r[0] == kModifierLeftShift && r[2] == 0x14);
        CHECK(typeReport('1', r) && r[0] == 0 && r[2] == 0x1e);
        CHECK(typeReport('9', r) && r[0] == 0 && r[2] == 0x26);
        CHECK(typeReport('0', r) && r[0] == 0 && r[2] == 0x27);
        CHECK(typeReport(' ', r) && r[0] == 0 && r[2] == 0x2c);
        CHECK(typeReport('!', r) && r[0] == kModifierLeftShift && r[2] == 0x1e);
        CHECK(typeReport('@', r) && r[0] == kModifierLeftShift && r[2] == 0x1f);
        CHECK(typeReport(')', r) && r[0] == kModifierLeftShift && r[2] == 0x27);
        CHECK(typeReport('-', r) && r[0] == 0 && r[2] == 0x2d);
        CHECK(typeReport('_', r) && r[0] == kModifierLeftShift && r[2] == 0x2d);
        CHECK(typeReport('\\', r) && r[0] == 0 && r[2] == 0x31);
        CHECK(typeReport('|', r) && r[0] == kModifierLeftShift && r[2] == 0x31);
        CHECK(typeReport('"', r) && r[0] == kModifierLeftShift && r[2] == 0x34);
        CHECK(typeReport('~', r) && r[0] == kModifierLeftShift && r[2] == 0x35);
        CHECK(typeReport('?', r) && r[0] == kModifierLeftShift && r[2] == 0x38);
        // Every printable ASCII character has a key; nothing else does, and no report carries more than one key.
        for (int c = 0; c < 256; ++c) {
            const char value = static_cast<char>(c);
            const bool printable = c >= 0x20 && c <= 0x7e;
            CHECK(typeReport(value, r) == printable);
            if (printable) CHECK(r[2] && !r[1] && !r[3] && !r[4] && !r[5] && !r[6] && !r[7]);
        }
        CHECK(typeTextLength("s", 1) == 1);
        CHECK(typeTextLength("1 a-B!", 6) == 6);
        CHECK(typeTextLength("abc", 0) == 0);
        CHECK(typeTextLength("a\nb", 3) == 0);
        CHECK(typeTextLength("a\0b", 3) == 0);
        char longest[kTypeMaxCharacters + 1];
        std::memset(longest, 'a', sizeof(longest));
        CHECK(typeTextLength(longest, kTypeMaxCharacters) == kTypeMaxCharacters);
        CHECK(typeTextLength(longest, kTypeMaxCharacters + 1) == 0);
        TypeTrigger t;
        for (unsigned i = 1; i < TypeTrigger::kHoldPolls; ++i) CHECK(!t.update(true));
        CHECK(t.update(true));                                   // exactly once at the end of the hold
        for (unsigned i = 0; i < 300; ++i) CHECK(!t.update(true));   // no repeat while the key stays down
        CHECK(!t.update(false));
        for (unsigned i = 1; i < TypeTrigger::kHoldPolls; ++i) CHECK(!t.update(true));
        CHECK(!t.update(false));                                 // released one poll early: nothing
        for (unsigned i = 1; i < TypeTrigger::kHoldPolls; ++i) CHECK(!t.update(true));
        CHECK(t.update(true));                                   // a new full hold types again
    }
    std::printf("PASS\n");
    return 0;
}
