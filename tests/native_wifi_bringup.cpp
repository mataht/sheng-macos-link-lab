// SPDX-License-Identifier: GPL-3.0-or-later
// Host test of platform/sheng/macos26/net/wcn_bringup.h: the stage 2 and 3 sequence against a simulated SoC.
// It checks the order of the safety relevant steps and that every failure stops before the blocks that are not ready.
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <functional>
#include <map>
#include <string>
#include <vector>
#include "spmi_keys.h"
#include "wcn_state.h"
#include "wcn_bringup.h"

using namespace sheng_wcn;

#define CHECK(x) do { if (!(x)) { std::printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #x); std::exit(1); } } while (0)

struct Event {
    Block block;
    uint32_t offset, value;
};

// The SPMI observer side (reads only).
struct Spmi : sheng_keys::Mmio {
    std::vector<uint32_t> coreRegs = std::vector<uint32_t>(0x3000 / 4), cnfgRegs = std::vector<uint32_t>(0x4000 / 4);
    std::map<uint32_t, uint32_t> obs;
    std::map<uint32_t, uint8_t> pmic;
    std::map<int, uint32_t> apidPpid;
    uint32_t core(uint32_t o) override { return coreRegs[o / 4]; }
    uint32_t cnfg(uint32_t o) override { return cnfgRegs[o / 4]; }
    uint32_t readObs(uint32_t o) override
    {
        if (o % 0x20 == sheng_keys::kStatus) return 1;
        return obs[o];
    }
    void writeObs(uint32_t o, uint32_t v) override
    {
        CHECK(o % 0x20 == sheng_keys::kCmd && v >> 27 == sheng_keys::kOpExtReadLong);
        const int apid = static_cast<int>((o % 0x8000) / 0x20);
        const uint32_t ppid = apidPpid.at(apid), reg = (v >> 4) & 0xff;
        obs[o - sheng_keys::kCmd + sheng_keys::kRdata0] = pmic[ppid << 8 | reg];
    }
    void delayMicroseconds(unsigned) override {}
    void map(int apid, uint16_t sid, uint16_t page, uint32_t owner)
    {
        const uint32_t ppid = static_cast<uint32_t>(sid) << 8 | page;
        coreRegs[(sheng_keys::kApidMapBase + 4 * apid) / 4] = ppid << 8 | 1u << 24;
        cnfgRegs[apid] = owner;
        apidPpid[apid] = ppid;
    }
};

struct Sim : Hw {
    std::map<uint32_t, uint32_t> regs[kBlockCount];
    std::vector<Event> log;
    Spmi *spmi = nullptr;
    unsigned channelOpens = 0, barOpens = 0;
    std::vector<std::pair<uint8_t, uint8_t>> spmiWrites;
    // Failure injection.
    bool gdscStuck = false, branchStuck = false, phyNeverReady = false, linkNeverUp = false, wrongEndpoint = false, deadBar = false;
    bool ltssmStarted = false, phyStarted = false, perstHigh = false;
    unsigned sleeps = 0;

    uint32_t read32(Block b, uint32_t o) override
    {
        if (b == kGcc) {
            if (o == kGdscPcie || o == kGdscPhy) {
                const bool off = regs[kGcc][kCollapse] & (o == kGdscPcie ? 1u : 8u);
                return (!off && !gdscStuck) ? 1u << 31 : 0;
            }
            for (unsigned i = 0; i < kBranchCount; ++i)
                if (o == kBranches[i]) {
                    if (branchStuck) return 1u << 31;
                    if (i == kPipeBranch) return phyStarted ? 0 : 1u << 31;
                    return (regs[kGcc][kVote8] | regs[kGcc][kVote0]) ? 0 : 1u << 31;
                }
            if (o == kAuxRcg || o == kRchngRcg) return 0;
        }
        if (b == kPhy && o == wifi_phy::kPcsStatus1)
            return (phyStarted && !phyNeverReady) ? 0 : 1u << 6;
        if (b == kDbi && o == kPortDebug1 && !linkNeverUp && ltssmStarted) return 1u << 4;
        if (b == kDbi && o == kPortDebug0 && !linkNeverUp && ltssmStarted) return 0x11;
        if (b == kConfig && o == 0 && ltssmStarted) return wrongEndpoint ? 0xffffffffu : kWcn7850Id;
        if (b == kSpmiChannel) {
            if ((o & 0xfff) == 0x08) return 1;
            return 0;
        }
        if (b == kRsc && o == 0) return 0x030000;
        if (b == kConfig && o == 0x10 && regs[kConfig][0x10] == 0xffffffffu) return 0xffe00004u;
        if (b == kBar && !deadBar) {
            static const std::map<uint32_t, uint32_t> mhi = {{0x00, 0x100}, {0x08, 0x10000}, {0x10, 0x02000000}, {0x28, 0x200}, {0x2c, 0x300}, {0x38, 0x1}, {0x48, 0x2},
                                                             {0x200, 0x0100}, {0x204, 0x0002}, {0x228, 0x03}, {0x22c, 0x0}, {0x230, 0x0}};
            auto it = mhi.find(o);
            return it == mhi.end() ? 0 : it->second;
        }
        return regs[b][o];
    }
    void write32(Block b, uint32_t o, uint32_t v) override
    {
        log.push_back(Event{b, o, v});
        regs[b][o] = v;
        if (b == kPhy && o == wifi_phy::kStartControl && (v & 3) == 3) phyStarted = true;
        if (b == kParf && o == kParfLtssm && (v & (1u << 8))) ltssmStarted = true;
        if (b == kTlmm && o == kPinPerst * 0x1000u + 4 && (v & 2)) perstHigh = true;
        if (b == kSpmiChannel && (o & 0xfff) == 0x10) lastWrite = static_cast<uint8_t>(v);
        if (b == kSpmiChannel && (o & 0xfff) == 0x00) spmiWrites.emplace_back(static_cast<uint8_t>((v >> 4) & 0xff), lastWrite);
    }
    bool openSpmiChannel(int) override { ++channelOpens; return true; }
    bool openBar(uint64_t, uint32_t) override { ++barOpens; return true; }
    void sleepMs(unsigned) override { ++sleeps; }
    void delayUs(unsigned) override {}
    void barrier() override {}
    uint8_t lastWrite = 0;

    unsigned countWrites(Block block) const
    {
        unsigned n = 0;
        for (const Event &e : log) if (e.block == block) ++n;
        return n;
    }
    long firstWrite(Block block, uint32_t offset, std::function<bool(uint32_t)> value) const
    {
        for (size_t i = 0; i < log.size(); ++i)
            if (log[i].block == block && log[i].offset == offset && value(log[i].value)) return static_cast<long>(i);
        return -1;
    }
};

static std::vector<uint8_t> makeDb(const std::vector<std::string> &names)
{
    std::vector<uint8_t> db(kCmdDbBytes, 0);
    const auto put16 = [&](size_t at, uint32_t v) { db[at] = v & 0xff; db[at + 1] = (v >> 8) & 0xff; };
    const auto put32 = [&](size_t at, uint32_t v) { put16(at, v & 0xffff); put16(at + 2, v >> 16); };
    put32(4, sheng_rpmh::kCmdDbMagic);
    put16(8, 1);                       // slave 0 id
    put16(8 + 2, 0);                   // header offset from byte 144
    put16(8 + 4, 0x800);               // data offset
    put16(8 + 6, static_cast<uint32_t>(names.size()));
    for (size_t i = 0; i < names.size(); ++i) {
        const size_t entry = 144 + 24 * i;
        std::memcpy(&db[entry], names[i].c_str(), names[i].size());
        put32(entry + 16, 0x30000u + 0x100u * static_cast<uint32_t>(i));
    }
    return db;
}

static const std::vector<std::string> kAllNames = {"smpg5", "ldob15", "smpg2", "smpe4", "smpg4", "smpg6", "SN7"};

struct Rig {
    Spmi spmi;
    Arbiter arbiter;
    Sim sim;
    std::vector<uint8_t> db;
    Report report;

    explicit Rig(const std::vector<std::string> &names = kAllNames, uint8_t sleepEnable = 0x80, uint8_t sleepMode = 1, uint8_t sleepOwner = 0, uint8_t sleepSource = 2, uint8_t sleepSubtype = 0x11)
    {
        arbiter.io = &spmi;
        arbiter.coreBytes = 0x3000;
        arbiter.cnfgBytes = 0x4000;
        arbiter.obsBytes = 0x80000;
        spmi.coreRegs[0] = 0x70000000;
        spmi.coreRegs[1] = 0x80;
        CHECK(sheng_keys::probe(arbiter) == Status::ok);
        spmi.map(5, kSleepClockSid, kSleepClockPage, sleepOwner);
        const uint32_t ppid = kSleepClockPage;
        spmi.pmic[ppid << 8 | kRegType] = 0x10;
        spmi.pmic[ppid << 8 | kRegSubtype] = sleepSubtype;
        spmi.pmic[ppid << 8 | kPinMode] = sleepMode;
        spmi.pmic[ppid << 8 | kPinSource] = sleepSource;
        spmi.pmic[ppid << 8 | kPinEnable] = sleepEnable;
        for (unsigned i = 0; i < kRailCount; ++i) {
            spmi.map(static_cast<int>(10 + i), kRails[i].sid, kRails[i].page, 0);
            const uint32_t railPpid = static_cast<uint32_t>(kRails[i].sid) << 8 | kRails[i].page;
            spmi.pmic[railPpid << 8 | kRegEnable] = 0x80;
        }
        sim.spmi = &spmi;
        db = makeDb(names);
    }
    void run(unsigned stage = 2) { bringUp(sim, arbiter, db.data(), stage, report); }
};

static long perstRelease(const Sim &s)
{
    for (size_t i = 0; i < s.log.size(); ++i)
        if (s.log[i].block == kTlmm && s.log[i].offset == kPinPerst * 0x1000u + 4 && (s.log[i].value & 2))
            return static_cast<long>(i);
    return -1;
}

static void testHappyPath()
{
    Rig r;
    r.run();
    CHECK(r.report.step == 17);
    CHECK(r.report.railsFound == 0x3f);
    CHECK(r.report.endpointId == kWcn7850Id);
    CHECK(r.sim.barOpens == 0);   // stage 2 stops before BAR0
    CHECK(r.sim.perstHigh);

    // Order: rails, then WLAN_EN high, then GDSC, then PHY, then PERST#, then the LTSSM.
    const long rsc = r.sim.firstWrite(kRsc, kRscTcs, [](uint32_t v) { return v == 7; });
    const long wlanHigh = r.sim.firstWrite(kTlmm, kPinWlanEn * 0x1000u + 4, [](uint32_t v) { return v & 2; });
    const long collapse = r.sim.firstWrite(kGcc, kCollapse, [](uint32_t) { return true; });
    const long parf = r.sim.firstWrite(kParf, kParfDeviceType, [](uint32_t) { return true; });
    const long phyStart = r.sim.firstWrite(kPhy, wifi_phy::kStartControl, [](uint32_t v) { return (v & 3) == 3; });
    const long perst = perstRelease(r.sim);
    const long ltssm = r.sim.firstWrite(kParf, kParfLtssm, [](uint32_t v) { return v & (1u << 8); });
    const long atu = r.sim.firstWrite(kAtu, 0x04, [](uint32_t v) { return v >> 31; });
    CHECK(rsc >= 0 && wlanHigh > rsc && collapse > wlanHigh && parf > collapse && phyStart > parf && perst > phyStart && ltssm > perst && atu > ltssm);

    // PERST# is low before WLAN_EN rises, and the WLAN_EN falling edge comes first.
    const long perstLow = r.sim.firstWrite(kTlmm, kPinPerst * 0x1000u, [](uint32_t) { return true; });
    const long wlanLow = r.sim.firstWrite(kTlmm, kPinWlanEn * 0x1000u, [](uint32_t) { return true; });
    CHECK(perstLow >= 0 && perstLow < wlanHigh && wlanLow < wlanHigh);

    // The PHY tables are written for both lanes.
    CHECK(r.sim.regs[kPhy][wifi_phy::kTx[0].offset] == wifi_phy::kTx[0].value);
    CHECK(r.sim.regs[kPhy][wifi_phy::kTx[0].offset + wifi_phy::kTx2] == wifi_phy::kTx[0].value);
    CHECK(r.sim.regs[kPhy][wifi_phy::kRx[0].offset + wifi_phy::kRx2] == wifi_phy::kRx[0].value);

    // The sleep clock pin was already driving: no write channel use.
    CHECK(r.report.sleepClockResult == 0xff);
    CHECK(r.sim.channelOpens == 0 && r.sim.spmiWrites.empty());

    char row[kRowBytes];
    for (unsigned i = 0; i < kBringupRows; ++i) {
        formatBringup(row, sizeof row, i, r.report);
        CHECK(strlen(row) < kRowBytes - 1);
    }
}

static void testStageThree()
{
    Rig r;
    r.run(3);
    CHECK(r.report.step == 19);
    CHECK(r.sim.barOpens == 1);
    CHECK(r.report.mhi[3] == 0x200 && r.report.bhiVersion == 0x01000002);   // minor word | major word << 16, as upstream prints it
    CHECK(r.report.barMask == 0xffe00004u);
    // BAR0 sits at the first address of the window aligned to its size.
    CHECK((r.report.barAddress & ~0xfu) == 0x60400000u);
}

static void testSleepClockWrites()
{
    Rig r(kAllNames, 0x00, 0x00);
    r.run();
    CHECK(r.report.sleepClockResult == 0);
    CHECK(r.sim.channelOpens == 1);
    const std::vector<std::pair<uint8_t, uint8_t>> want = {{kPinPull, 5}, {kPinVoltage, 0}, {kPinSource, 2}, {kPinMode, 1}, {kPinEnable, 0x80}};
    CHECK(r.sim.spmiWrites == want);
    CHECK(r.report.step == 17);
}

static void testSleepClockRefusals()
{
    {   // Not ours to write: nothing else happens.
        Rig r(kAllNames, 0x00, 0x00, 3);
        r.run();
        CHECK(r.report.step == -1 && r.report.sleepClockResult == 3);
        CHECK(r.sim.spmiWrites.empty() && r.sim.log.empty());
    }
    {   // Not the pin layout the writes are for.
        Rig r(kAllNames, 0x00, 0x00, 0, 2, 0x05);
        r.run();
        CHECK(r.report.step == -1 && r.report.sleepClockResult == 4);
        CHECK(r.sim.log.empty());
    }
}

static void testFailures()
{
    {   // A rail missing from the command database: no pin or clock is touched.
        Rig r({"smpg5", "ldob15", "smpg2", "smpe4", "smpg4"});
        r.run();
        CHECK(r.report.step == -2 && r.report.railsFound == 0x1f);
        CHECK(r.sim.countWrites(kTlmm) == 0 && r.sim.countWrites(kGcc) == 0 && r.sim.countWrites(kParf) == 0);
    }
    {   // No command database at all.
        Rig r;
        std::fill(r.db.begin(), r.db.end(), 0);
        r.run();
        CHECK(r.report.step == -2 && r.report.railsFound == 0);
        CHECK(r.sim.countWrites(kTlmm) == 0);
    }
    {   // GDSCs never power up: PARF, PHY and DBI are never touched.
        Rig r;
        r.sim.gdscStuck = true;
        r.run();
        CHECK(r.report.step == -11);
        CHECK(r.sim.countWrites(kParf) == 0 && r.sim.countWrites(kPhy) == 0 && r.sim.countWrites(kDbi) == 0 && r.sim.countWrites(kAtu) == 0);
        CHECK(!r.sim.perstHigh);
    }
    {   // A branch stays off: the controller is left alone, PERST# stays low.
        Rig r;
        r.sim.branchStuck = true;
        r.run();
        CHECK(r.report.step == -12);
        CHECK(r.sim.countWrites(kParf) == 0 && r.sim.countWrites(kPhy) == 0 && r.sim.countWrites(kDbi) == 0);
        CHECK(!r.sim.perstHigh);
    }
    {   // The PHY never reports ready: PERST# stays low and the LTSSM is not started.
        Rig r;
        r.sim.phyNeverReady = true;
        r.run();
        CHECK(r.report.step == -14);
        CHECK(!r.sim.perstHigh && !r.sim.ltssmStarted);
    }
    {   // The link does not train: no iATU programming and no configuration read.
        Rig r;
        r.sim.linkNeverUp = true;
        r.run();
        CHECK(r.report.step == -16 && r.report.linkMilliseconds == 1000);
        CHECK(r.sim.countWrites(kAtu) == 0);
    }
    {   // The endpoint is not the WCN7850: stage 3 does not assign a BAR.
        Rig r;
        r.sim.wrongEndpoint = true;
        r.run(3);
        CHECK(r.report.step == 17 && r.sim.barOpens == 0);
    }
}

int main()
{
    testHappyPath();
    testStageThree();
    testSleepClockWrites();
    testSleepClockRefusals();
    testFailures();
    std::printf("PASS\n");
    return 0;
}
