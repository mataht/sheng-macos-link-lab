// SPDX-License-Identifier: GPL-3.0-or-later
// Host test of platform/sheng/macos26/net/wcn_state.h against a simulated SPMI arbiter v7, and of its report rows.
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <map>
#include <string>
#include <vector>
#include "wcn_state.h"

using namespace sheng_wcn;
using sheng_keys::kApidMapBase;
using sheng_keys::kOpExtReadLong;
using sheng_keys::kRdata0;
using sheng_keys::kCmd;
using sheng_keys::kStatus;

#define CHECK(x) do { if (!(x)) { std::printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #x); std::exit(1); } } while (0)

struct Sim : sheng_keys::Mmio {
    std::vector<uint32_t> coreRegs = std::vector<uint32_t>(0x3000 / 4), cnfgRegs = std::vector<uint32_t>(0x4000 / 4);
    std::map<uint32_t, uint32_t> obs;
    std::map<uint32_t, uint8_t> pmic;
    std::map<int, uint32_t> apidPpid;
    uint32_t statusOverride = 0;
    std::vector<uint32_t> commands;
    uint32_t core(uint32_t o) override { CHECK(o % 4 == 0 && o / 4 < coreRegs.size()); return coreRegs[o / 4]; }
    uint32_t cnfg(uint32_t o) override { CHECK(o % 4 == 0 && o / 4 < cnfgRegs.size()); return cnfgRegs[o / 4]; }
    uint32_t readObs(uint32_t o) override
    {
        if (o % 0x20 == kStatus) return statusOverride ? statusOverride : 1;
        return obs[o];
    }
    void writeObs(uint32_t o, uint32_t v) override
    {
        // The only register ever written is a channel's command register, and only with the read opcode.
        CHECK(o % 0x20 == kCmd);
        CHECK(v >> 27 == kOpExtReadLong);
        commands.push_back(v);
        const int apid = static_cast<int>((o % 0x8000) / 0x20);
        const uint32_t ppid = apidPpid.at(apid), reg = (v >> 4) & 0xff;
        obs[o - kCmd + kRdata0] = pmic[ppid << 8 | reg];
    }
    void delayMicroseconds(unsigned) override {}
    void map(int apid, uint16_t sid, uint16_t page, uint32_t owner)
    {
        const uint32_t ppid = static_cast<uint32_t>(sid) << 8 | page;
        coreRegs[(kApidMapBase + 4 * apid) / 4] = ppid << 8 | 1u << 24;
        cnfgRegs[apid] = owner;
        apidPpid[apid] = ppid;
    }
    void setRail(int apid, const RailSpec &spec, uint8_t type, uint8_t subtype, uint8_t status, uint8_t enable, uint16_t mv)
    {
        map(apid, spec.sid, spec.page, 0);
        const uint32_t ppid = static_cast<uint32_t>(spec.sid) << 8 | spec.page;
        pmic[ppid << 8 | kRegType] = type;
        pmic[ppid << 8 | kRegSubtype] = subtype;
        pmic[ppid << 8 | kRegStatus] = status;
        pmic[ppid << 8 | kRegEnable] = enable;
        pmic[ppid << 8 | kRegVoltageLow] = mv & 0xff;
        pmic[ppid << 8 | kRegVoltageHigh] = mv >> 8;
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
    s.coreRegs[0] = 0x70000000;
    s.coreRegs[4 / 4] = 0x80;
    CHECK(sheng_keys::probe(a) == Status::ok);
    return a;
}

static void testRails()
{
    Sim sim;
    Arbiter a = arbiter(sim);
    for (unsigned i = 0; i < kRailCount; ++i)
        sim.setRail(static_cast<int>(10 + i), kRails[i], kTypeFts, 0x0d, 0x80, i == 1 ? 0x00 : 0x80, static_cast<uint16_t>(kRails[i].nominalMillivolts + 2));
    RailState state;
    for (unsigned i = 0; i < kRailCount; ++i) {
        CHECK(readRail(a, kRails[i], &state) == Status::ok);
        CHECK(state.apid == static_cast<int>(10 + i));
        CHECK(state.millivolts == kRails[i].nominalMillivolts + 2);
        CHECK(state.on() == (i != 1));
    }
    char row[kRowBytes];
    readRail(a, kRails[0], &state);
    formatRail(row, sizeof row, kRails[0], state);
    CHECK(strstr(row, "vdd-s5g") && strstr(row, "852mV") && strstr(row, "en80"));
    CHECK(strlen(row) < 69);

    // A wrong address shows as a row that says so; it does not stop the report.
    Sim bare;
    Arbiter b = arbiter(bare);
    CHECK(readRail(b, kRails[1], &state) == Status::notFound);
    formatRail(row, sizeof row, kRails[1], state);
    CHECK(strstr(row, "absent"));
    CHECK(!state.on());

    // A peripheral of another type is reported, and is not treated as an enabled regulator.
    Sim other;
    Arbiter c = arbiter(other);
    other.setRail(20, kRails[1], 0x51, 0x01, 0x80, 0x80, 1800);
    CHECK(readRail(c, kRails[1], &state) == Status::ok);
    CHECK(!state.knownType() && !state.on());

    // An arbiter failure reports the failing read and stops reading that rail.
    Sim denied;
    Arbiter d = arbiter(denied);
    denied.setRail(21, kRails[0], kTypeFts, 0x0d, 0x80, 0x80, 850);
    denied.statusOverride = 1 | 4;
    CHECK(readRail(d, kRails[0], &state) == Status::denied);
    CHECK(denied.commands.size() == 1);
    formatRail(row, sizeof row, kRails[0], state);
    CHECK(strstr(row, "denied"));
}

static void testSleepClock()
{
    Sim sim;
    Arbiter a = arbiter(sim);
    sim.map(5, kSleepClockSid, kSleepClockPage, 0);
    const uint32_t ppid = kSleepClockPage;
    const auto set = [&](uint8_t reg, uint8_t value) { sim.pmic[ppid << 8 | reg] = value; };
    set(kRegType, 0x10); set(kRegSubtype, 0x11); set(kPinMode, 0x01); set(kPinVoltage, 0x00); set(kPinPull, 0x05); set(kPinSource, 0x02); set(kPinEnable, 0x80);
    SleepClockState state;
    CHECK(readSleepClock(a, &state) == Status::ok);
    CHECK(state.owner == 0 && state.driving());
    char row[kRowBytes];
    formatSleepClock(row, sizeof row, state);
    CHECK(strstr(row, "driving"));

    set(kPinMode, 0x00);
    CHECK(readSleepClock(a, &state) == Status::ok);
    CHECK(!state.driving());
    set(kPinMode, 0x01); set(kPinSource, 0x01);
    readSleepClock(a, &state);
    CHECK(!state.driving());
    set(kPinSource, 0x02); set(kPinEnable, 0x00);
    readSleepClock(a, &state);
    CHECK(!state.driving());
    set(kPinEnable, 0x80); set(kRegSubtype, 0x05);
    readSleepClock(a, &state);
    CHECK(!state.driving());
    sim.cnfgRegs[5] = 3;
    readSleepClock(a, &state);
    CHECK(state.owner == 3);

    Sim bare;
    Arbiter b = arbiter(bare);
    CHECK(readSleepClock(b, &state) == Status::notFound);
    formatSleepClock(row, sizeof row, state);
    CHECK(strstr(row, "absent"));
}

static void testRows()
{
    uint32_t st[kWifiStatsWords] = {2, 0x1, 0x40000, 0x80000000, 0x3ff, 0x030201, 0x11, 0x110717cb, 0x10, 0x8};
    char row[kRowBytes];
    formatClocks(row, sizeof row, st);
    CHECK(!strcmp(row, "st2 v8 00000001 v0 00040000 gdsc 80000000 off 3ff"));
    formatLink(row, sizeof row, st);
    CHECK(!strcmp(row, "pin 030201 ltssm 00000011 rc 110717cb lnk 00000010 phy 00000008"));
    st[0] = static_cast<uint32_t>(-1);
    formatClocks(row, sizeof row, st);
    CHECK(strstr(row, "st-1 ") == row);

    PinState pins[kPinCount];
    for (unsigned i = 0; i < kPinCount; ++i) { pins[i].config = 0x201u + i; pins[i].io = i & 3; }
    formatPins(row, sizeof row, 0, pins);
    CHECK(!strcmp(row, "76:0201/0 77:0202/1 78:0203/2 79:0204/3 81:0205/0"));
    formatPins(row, sizeof row, kPinsPerRow, pins);
    CHECK(!strcmp(row, "80:0206/1 82:0207/2 94:0208/3 95:0209/0 96:020a/1"));
    // The pin list stays clear of the TLMM reserved ranges of sheng (gpio-reserved-ranges = <36 4>).
    for (unsigned i = 0; i < kPinCount; ++i)
        CHECK(kPins[i] < 36 || kPins[i] >= 40);
    // The worst case rows fit the 88 columns of the boot log after the longest prefix "NativeWifi t120 R10: ".
    CHECK(kRowBytes - 1 + std::strlen("NativeWifi t120 R10: ") <= 92);
}

int main()
{
    testRails();
    testSleepClock();
    testRows();
    std::printf("PASS\n");
    return 0;
}
