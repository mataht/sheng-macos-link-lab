/* SPDX-License-Identifier: BSD-2-Clause-Patent */
/* Copyright (c) 2026, UniXNU contributors. All rights reserved. (the UniXNU-sheng license text is in LICENSES/ and THIRD_PARTY_NOTICES)
 *
 * WCN7850 power and PCIe0 bring-up for sheng (stages 2 and 3 of NativeWifi). The sequence is that of UniXNU-sheng package/WifiProbe.cpp
 * at commit a68ee4ee67cc71792fbf519de7e3e28072c717b5 (pmicPrepare, powerRails, bringUp, endpointProbe up to the MHI register read),
 * rewritten over an abstract register interface so a host test can run it. The firmware download, MHI, DMA, the ath12k stack and the
 * network interface are not here.
 *
 * Evidence: upstream reports this sequence working on the same board under iPadOS. None of it has run under macOS 26.
 *
 * What it changes: PMK8550 gpio3 (sleep clock pin) through the SPMI write channel when the pin is not already driving; six RPMh rails
 * and the PCIe NoC vote through TCS 0 of the apps RSC; TLMM GPIO 80, 94 and 95; GCC clock votes, GDSCs and block resets of PCIe0;
 * TCSR PCIe clock reference; the PCIe0 PARF, DBI, iATU and QMP PHY; configuration space of the endpoint. Nothing is written to storage.
 */
#ifndef SHENG_WCN_BRINGUP_H
#define SHENG_WCN_BRINGUP_H

#include <stddef.h>
#include <stdint.h>
#include "spmi_keys.h"
#include "wcn_pcie_tables.h"
#include "wcn_rpmh.h"
#include "wcn_state.h"

namespace sheng_wcn {

enum Block : unsigned { kGcc, kTlmm, kTcsr, kParf, kDbi, kPhy, kAtu, kConfig, kRsc, kSpmiChannel, kBar, kBlockCount };

struct BlockSpec {
    uint64_t physical;
    uint32_t bytes;
};

// sm8550.dtsi and gcc-sm8550.c, as upstream uses them. kSpmiChannel and kBar are mapped per use, by the owner of Hw.
constexpr BlockSpec kBlocks[kBlockCount] = {
    {0x100000, 0x80000}, {0xf100000, 0x64000}, {0x1fd5000, 0x1000},  {0x1c00000, 0x3000}, {0x60000000, 0x1000}, {0x1c06000, 0x2000},
    {0x60001000, 0x1000}, {0x60100000, 0x1000}, {0x17a20000, 0x10000}, {0xc500000, 0},      {0x60300000, 0},
};

constexpr uint64_t kCmdDbPhysical = 0x81c60000;
constexpr uint32_t kCmdDbBytes = 0x20000;

struct Hw {
    virtual uint32_t read32(Block, uint32_t offset) = 0;
    virtual void write32(Block, uint32_t offset, uint32_t value) = 0;
    // Reserves channel 0x1000 * apid of the SPMI arbiter's read/write space for spmi writes. False if it cannot be mapped.
    virtual bool openSpmiChannel(int apid) = 0;
    // Maps the endpoint's BAR0 (stage 3). False if it cannot be mapped.
    virtual bool openBar(uint64_t physical, uint32_t bytes) = 0;
    virtual void sleepMs(unsigned) = 0;
    virtual void delayUs(unsigned) = 0;
    virtual void barrier() = 0;
    virtual ~Hw() = default;
};

// Registers (gcc-sm8550.c, pcie-qcom.c, pcie-designware.h, pinctrl; the names of upstream's WifiProbe.cpp).
constexpr uint32_t kVote8 = 0x52008, kVote0 = 0x52000, kGdscPcie = 0x6b004, kGdscPhy = 0x6c000, kCollapse = 0x52020;
constexpr uint32_t kBranches[] = {0x6b020, 0x6b02c, 0x6b038, 0x6b03c, 0x6b048, 0x6b01c, 0x10030, 0x1003c, 0x1004c, 0x6b054};
constexpr unsigned kBranchCount = sizeof kBranches / sizeof kBranches[0];
constexpr unsigned kPipeBranch = 4;  // runs only once the PHY drives it
constexpr unsigned kPinWlanEn = 80, kPinPerst = 94, kPinClkReq = 95;
constexpr uint32_t kPcieBcr = 0x6b000, kPhyBcr = 0x6c01c, kPipeMux = 0x6b070, kAuxRcg = 0x6b074, kRchngRcg = 0x6b058;
constexpr uint32_t kVote8Bits = 0x7f, kVote0Bits = 1u << 12 | 1u << 19 | 1u << 22;
constexpr uint32_t kParfSysCtrl = 0x0, kParfPhyCtrl = 0x40, kParfPmCtrl = 0x20, kParfMhiClockReset = 0x174, kParfAxiHalt = 0x1a8,
                   kParfLtssm = 0x1b0, kParfDbiBase = 0x350, kParfDbiBaseHi = 0x354, kParfSlvSpace = 0x358, kParfSlvSpaceHi = 0x35c,
                   kParfAtuBase = 0x634, kParfAtuBaseHi = 0x638, kParfDeviceType = 0x1000, kParfBdfTable = 0x2000, kParfBdfBypass = 0x2c00;
constexpr uint32_t kDbiCommand = 0x4, kDbiBusNumbers = 0x18, kDbiMemWindow = 0x20, kDbiLinkControl = 0x80, kPortDebug0 = 0x728, kPortDebug1 = 0x72c;
constexpr uint64_t kDbiBase = 0x60000000, kAtuBase = 0x60001000, kConfigBase = 0x60100000;
constexpr uint32_t kSlvSpaceBytes = 0x80000000;
constexpr uint32_t kRscTcs = 0xd00;
constexpr uint64_t kMemBase = 0x60300000, kMemSize = 0x3d00000;
constexpr uint32_t kWcn7850Id = 0x110717cb;

struct RailVote {
    const char *name;
    uint32_t millivolts;
    int stateIndex;  // into kRails of wcn_state.h (the PMIC peripheral read to see the request take effect)
    bool checkPmic;  // upstream knows the peripheral of every rail except vddio
};

constexpr RailVote kRailVotes[] = {
    {"smpg5", 850, 0, true}, {"ldob15", 1800, 1, false}, {"smpg2", 850, 2, true}, {"smpe4", 950, 3, true}, {"smpg4", 1300, 4, true}, {"smpg6", 1860, 5, true},
};
constexpr unsigned kRailVoteCount = sizeof kRailVotes / sizeof kRailVotes[0];

// Steps: 1 sleep clock ready, 2 rails voted, 10 WLAN_EN high, 11 GDSCs on, 12 clocks on and blocks out of reset, 13 PARF set up,
// 14 PHY ready and root port set up, 15 PERST# released, 16 link up, 17 endpoint ID read, 18 BAR0 mapped, 19 MHI registers read.
// A negative step is the step that failed: -1 sleep clock, -2 rails, -11 GDSC, -12 clocks, -13/-14 PARF or PHY mapping or PHY not ready,
// -16 link timeout, -17 config or BAR mapping.
struct Report {
    int step = 0;
    uint32_t sleepClockResult = 0;  // 0 written, 0xff not needed, 2 no APID, 3 owner is not EE 0, 4 not a LV/MV pin, 0x10+ SPMI status
    uint32_t railsFound = 0, railsEnabled = 0;
    uint32_t noCmdDb = 0;
    uint32_t pinsAfterEnable = 0;
    uint32_t wlanEnPin = 0, perstPin = 0, clkReqPin = 0;
    uint32_t gdsc = 0;       // bit 0 PCIe0, bit 1 PHY, then branch-off bits << 8
    uint32_t phyStatus = 0;  // PCS_STATUS1 | polls << 16
    uint32_t rootId = 0, portDebug0 = 0, portDebug1 = 0, ltssm = 0, linkControl = 0, parfPhyCtrl = 0;
    uint32_t linkMilliseconds = 0;
    uint32_t endpointId = 0;
    uint32_t barMask = 0, barAddress = 0, command = 0;
    uint32_t mhi[7] = {};  // MHIREGLEN MHIVER MHICFG BHIOFF BHIEOFF MHICTRL MHISTATUS
    uint32_t bhiVersion = 0, bhiEe = 0, bhiStatus = 0, bhiError = 0;
};

namespace detail {

constexpr uint32_t kSpmiWriteData = 0x10, kSpmiCommand = 0x00, kSpmiStatus = 0x08;

inline uint32_t spmiCommand(uint8_t op, uint8_t reg) { return static_cast<uint32_t>(op) << 27 | static_cast<uint32_t>(reg) << 4; }

inline Status spmiWrite8(Hw &hw, int apid, uint8_t reg, uint8_t value)
{
    const uint32_t channel = 0x1000u * static_cast<uint32_t>(apid);
    hw.write32(kSpmiChannel, channel + kSpmiWriteData, value);
    hw.write32(kSpmiChannel, channel + kSpmiCommand, spmiCommand(0, reg));
    for (unsigned poll = 0; poll < sheng_keys::kPollLimit; ++poll) {
        const uint32_t status = hw.read32(kSpmiChannel, channel + kSpmiStatus);
        if (status & 1) {
            if (status & 2) return Status::failure;
            if (status & 4) return Status::denied;
            if (status & 8) return Status::dropped;
            return Status::ok;
        }
        hw.delayUs(1);
    }
    return Status::timeout;
}

inline uint32_t pinWord(Hw &hw, unsigned pin) { return (hw.read32(kTlmm, pin * 0x1000) & 0xffff) | hw.read32(kTlmm, pin * 0x1000 + 4) << 16; }

// TLMM pin config: pull in bits 0-1 (1 down, 3 up), function in 2-5, drive (mA / 2 - 1) in 6-8, output enable bit 9; IO bit 1 drives.
inline void setPin(Hw &hw, unsigned pin, uint32_t config, int level)
{
    if (level >= 0) hw.write32(kTlmm, pin * 0x1000 + 4, level ? 2u : 0u);
    hw.write32(kTlmm, pin * 0x1000, config);
    if (level >= 0) hw.write32(kTlmm, pin * 0x1000 + 4, level ? 2u : 0u);
}

inline bool waitClear(Hw &hw, Block block, uint32_t offset, uint32_t mask, unsigned tries, unsigned *used = nullptr)
{
    unsigned i = 0;
    for (; i < tries && (hw.read32(block, offset) & mask); ++i)
        hw.delayUs(100);
    if (used) *used = i;
    return !(hw.read32(block, offset) & mask);
}

inline void pulseReset(Hw &hw, uint32_t bcr)
{
    hw.write32(kGcc, bcr, hw.read32(kGcc, bcr) | 1u);
    hw.delayUs(1000);
    hw.write32(kGcc, bcr, hw.read32(kGcc, bcr) & ~1u);
    hw.delayUs(1000);
}

inline void rmw(Hw &hw, Block block, uint32_t offset, uint32_t clear, uint32_t set)
{
    hw.write32(block, offset, (hw.read32(block, offset) & ~clear) | set);
}

}  // namespace detail

// Upstream pmicPrepare: leave PMK8550 gpio3 (the WCN7850 sleep clock) driving. Returns false when the pin could not be readied.
inline bool prepareSleepClock(Hw &hw, Arbiter &arbiter, Report &report)
{
    SleepClockState before;
    readSleepClock(arbiter, &before);
    if (before.status == Status::notFound) { report.sleepClockResult = 2; return false; }
    if (before.status != Status::ok) { report.sleepClockResult = 0x10u + static_cast<unsigned>(-static_cast<int>(before.status)); return false; }
    if (before.driving()) { report.sleepClockResult = 0xff; return true; }
    if (before.owner != 0) { report.sleepClockResult = 3; return false; }
    if (before.subtype < kPinSubtypeLow || before.subtype > kPinSubtypeHigh) { report.sleepClockResult = 4; return false; }
    if (!hw.openSpmiChannel(before.apid)) { report.sleepClockResult = 0x10u + static_cast<unsigned>(-static_cast<int>(Status::unsupported)); return false; }
    // pull disabled, power source 0, source function, output mode, master enable (the order the pinctrl driver applies them)
    const struct { uint8_t reg, value; } writes[] = {{kPinPull, 5}, {kPinVoltage, 0}, {kPinSource, 2}, {kPinMode, 1}, {kPinEnable, 0x80}};
    for (const auto &w : writes) {
        const Status s = detail::spmiWrite8(hw, before.apid, w.reg, w.value);
        if (s != Status::ok) { report.sleepClockResult = 0x10u + static_cast<unsigned>(-static_cast<int>(s)); return false; }
    }
    report.sleepClockResult = 0;
    return true;
}

// Upstream powerRails(shared=true, pcie=true): one active request per register of a rail (mode HPM 7, voltage, enable), then the PCIe NoC
// bus clock vote SN7. db is a copy of the AOP command database, kCmdDbBytes long.
inline bool voteRails(Hw &hw, Arbiter &arbiter, const uint8_t *db, Report &report)
{
    using namespace sheng_rpmh;
    const Layout layout = rscLayout(hw.read32(kRsc, 0));
    constexpr unsigned kTcs = 0;  // apps_rsc qcom,tcs-config: type 2 (active) x3 first, so TCS 0 is active-only
    hw.write32(kRsc, kRscTcs, 7u);
    hw.barrier();
    const auto send = [&](uint32_t address, uint32_t value) {
        Write writes[16];
        const unsigned n = activeRequest(layout, kTcs, &address, &value, 1, writes, 16);
        for (unsigned i = 0; i < n; ++i)
            hw.write32(kRsc, kRscTcs + writes[i].offset, writes[i].value);
        hw.barrier();
        hw.sleepMs(2);
        const unsigned m = finish(layout, kTcs, writes, 16);
        for (unsigned i = 0; i < m; ++i)
            hw.write32(kRsc, kRscTcs + writes[i].offset, writes[i].value);
    };
    for (unsigned i = 0; i < kRailVoteCount; ++i) {
        const RailVote &rail = kRailVotes[i];
        Resource resource;
        if (cmdDbFind(db, kCmdDbBytes, rail.name, &resource) != Ok)
            continue;
        report.railsFound |= 1u << i;
        send(resource.address + 8, 7);                 // mode: high power
        send(resource.address, rail.millivolts);
        send(resource.address + 4, 1);                 // enable
        const int apid = rail.checkPmic ? sheng_keys::findApid(arbiter, kRails[rail.stateIndex].sid, kRails[rail.stateIndex].page) : -1;
        if (apid < 0) {
            hw.sleepMs(20);
        } else {
            for (unsigned waited = 0; waited < 50; ++waited) {
                uint8_t enable = 0;
                if (sheng_keys::readByte(arbiter, apid, kRegEnable, &enable) == Status::ok && (enable & kRegulatorEnabled)) {
                    report.railsEnabled |= 1u << i;
                    break;
                }
                hw.sleepMs(1);
            }
        }
    }
    // The endpoint's DMA runs over the PCIe NoC, whose bus clock manager (SN7) the interconnect driver votes when the link comes up.
    // vote = ceil(ceil(kBps * width / buswidth) * 1000 / unit), buswidth 16, kBps 2000000; a missing unit keeps the upstream fallback.
    Resource bcm;
    if (cmdDbFind(db, kCmdDbBytes, "SN7", &bcm) == Ok) {
        uint32_t vote = 0x400;
        if (bcm.levelCount >= 3 && (bcm.levels[0] | static_cast<uint32_t>(bcm.levels[1]) << 16) && bcm.levels[2]) {
            const uint64_t unit = bcm.levels[0] | static_cast<uint32_t>(bcm.levels[1]) << 16;
            const uint64_t scaled = (2000000ull * bcm.levels[2] + 15) / 16;
            const uint64_t x = (scaled * 1000 + unit - 1) / unit;
            vote = x < 1 ? 1 : x > 0x3fff ? 0x3fff : static_cast<uint32_t>(x);
        }
        send(bcm.address, 1u << 30 | 1u << 29 | vote << 14 | vote);
    } else {
        report.noCmdDb |= 1;
    }
    return report.railsFound == (1u << kRailVoteCount) - 1;
}

// Upstream bringUp, from WLAN_EN to the configuration read of the endpoint. Every step that fails stops here with PERST# still low
// unless the link had already been released.
inline void bringLink(Hw &hw, Report &report)
{
    using namespace detail;
    using namespace wifi_phy;
    // PERST# held low, CLKREQ# to its PCIe function, WLAN_EN low first so the chip starts from nothing, then high (pwrseq-qcom-wcn.c: 50 ms).
    setPin(hw, kPinPerst, 1u | 1u << 9, 0);
    setPin(hw, kPinClkReq, 3u | 1u << 2, -1);
    setPin(hw, kPinWlanEn, 1u | 3u << 6 | 1u << 9, 0);
    hw.sleepMs(150);
    setPin(hw, kPinWlanEn, 1u | 3u << 6 | 1u << 9, 1);
    hw.sleepMs(60);
    report.wlanEnPin = pinWord(hw, kPinWlanEn);
    report.perstPin = pinWord(hw, kPinPerst);
    report.clkReqPin = pinWord(hw, kPinClkReq);
    report.step = 10;

    // Both GDSCs are votable: a set bit in the collapse register (PCIe0 bit 0, PHY bit 3) keeps them collapsed.
    rmw(hw, kGcc, kCollapse, 1u << 0 | 1u << 3, 0);
    for (unsigned tries = 0; tries < 100 && (!(hw.read32(kGcc, kGdscPcie) >> 31) || !(hw.read32(kGcc, kGdscPhy) >> 31)); ++tries)
        hw.delayUs(10);
    const uint32_t power = (hw.read32(kGcc, kGdscPcie) >> 31) | (hw.read32(kGcc, kGdscPhy) >> 31) << 1;
    report.gdsc = power;
    if (power != 3) { report.step = -11; return; }
    report.step = 11;

    // Sources first (an RCG takes a new CFG on CMD bit 0), the pipe mux parked on TCXO until the PHY runs, then the branch votes.
    hw.write32(kGcc, kAuxRcg + 4, 0);
    rmw(hw, kGcc, kAuxRcg, 0, 1u);
    hw.write32(kGcc, kRchngRcg + 4, 6u << 8 | 5u);
    rmw(hw, kGcc, kRchngRcg, 0, 1u);
    waitClear(hw, kGcc, kAuxRcg, 1u, 100);
    waitClear(hw, kGcc, kRchngRcg, 1u, 100);
    rmw(hw, kGcc, kPipeMux, 3u, 2u);
    rmw(hw, kGcc, kVote8, 0, kVote8Bits);
    rmw(hw, kGcc, kVote0, 0, kVote0Bits);
    rmw(hw, kTcsr, 0x100, 0, 1u);
    hw.delayUs(200);
    uint32_t off = 0;
    for (unsigned i = 0; i < kBranchCount; ++i)
        if (hw.read32(kGcc, kBranches[i]) >> 31) off |= 1u << i;
    report.gdsc = power | off << 8;
    if (off & ~(1u << kPipeBranch)) { report.step = -12; return; }
    pulseReset(hw, kPcieBcr);
    pulseReset(hw, kPhyBcr);
    report.step = 12;

    // qcom_pcie_init_2_7_0 after its resets, before the PHY powers on: PHY_CTRL's test power-down bit holds the PHY.
    hw.write32(kParf, kParfDeviceType, 4u);
    rmw(hw, kParf, kParfPhyCtrl, 1u, 0);
    hw.write32(kParf, kParfDbiBase, static_cast<uint32_t>(kDbiBase));
    hw.write32(kParf, kParfDbiBaseHi, static_cast<uint32_t>(kDbiBase >> 32));
    hw.write32(kParf, kParfAtuBase, static_cast<uint32_t>(kAtuBase));
    hw.write32(kParf, kParfAtuBaseHi, static_cast<uint32_t>(kAtuBase >> 32));
    hw.write32(kParf, kParfSlvSpace, 0);
    hw.write32(kParf, kParfSlvSpaceHi, kSlvSpaceBytes);
    rmw(hw, kParf, kParfSysCtrl, 1u << 29, 0);
    rmw(hw, kParf, kParfMhiClockReset, 0, 1u << 4);
    rmw(hw, kParf, kParfPmCtrl, 1u << 5, 0);
    rmw(hw, kParf, kParfAxiHalt, 0, 1u << 31);
    report.step = 13;

    // qmp_pcie_power_on: power-down control, the tables (both lanes), pipe clock from the PHY, out of reset, start, PHYSTATUS low.
    rmw(hw, kPhy, kPowerDownControl, 0, 3u);
    for (const Init &e : kSerdes) hw.write32(kPhy, e.offset, e.value);
    for (const Init &e : kTx) { hw.write32(kPhy, e.offset, e.value); hw.write32(kPhy, e.offset + kTx2, e.value); }
    for (const Init &e : kRx) { hw.write32(kPhy, e.offset, e.value); hw.write32(kPhy, e.offset + kRx2, e.value); }
    for (const Init &e : kPcs) hw.write32(kPhy, e.offset, e.value);
    for (const Init &e : kPcsMisc) hw.write32(kPhy, e.offset, e.value);
    rmw(hw, kGcc, kPipeMux, 3u, 0);
    rmw(hw, kPhy, kSwReset, 1u, 0);
    rmw(hw, kPhy, kStartControl, 0, 3u);
    hw.delayUs(1000);
    unsigned polls = 0;
    const bool ready = waitClear(hw, kPhy, kPcsStatus1, 1u << 6, 100, &polls);
    report.phyStatus = (hw.read32(kPhy, kPcsStatus1) & 0xffffu) | polls << 16;
    if (!ready) { report.step = -14; return; }

    // qcom_pcie_config_sid_1_9_0: requester 0x100 (bus 1, the endpoint) to stream 0x1401. Kept from upstream's order; no DMA runs at these stages.
    for (unsigned i = 0; i < 256; ++i)
        hw.write32(kParf, kParfBdfTable + 4 * i, 0);
    rmw(hw, kParf, kParfBdfBypass, 1u, 0);
    uint8_t crc = 0;
    const uint8_t requester[2] = {0x01, 0x00};   // bus 1, device 0, function 0: the high byte first
    for (unsigned b = 0; b < 2; ++b) {
        crc ^= requester[b];
        for (int bit = 0; bit < 8; ++bit)
            crc = (crc & 0x80) ? static_cast<uint8_t>((crc << 1) ^ 7) : static_cast<uint8_t>(crc << 1);
    }
    hw.write32(kParf, kParfBdfTable + 4u * crc, 0x100u << 16 | 1u << 8);
    hw.barrier();

    // The root port's bus numbers and command (dw_pcie_setup_rc).
    hw.write32(kDbi, kDbiBusNumbers, 0x00010100);
    rmw(hw, kDbi, kDbiCommand, 0, 0x6u);
    report.rootId = hw.read32(kDbi, 0);
    report.step = 14;

    // qcom_ep_reset_deassert: PERST# low for at least 100 ms, then high, then the LTSSM.
    hw.sleepMs(100);
    setPin(hw, kPinPerst, 1u | 1u << 9, 1);
    hw.delayUs(1000);
    report.step = 15;
    rmw(hw, kParf, kParfLtssm, 0, 1u << 8);
    unsigned ms = 0;
    for (; ms < 1000; ++ms) {
        if ((hw.read32(kDbi, kPortDebug1) & (1u << 4)) && (hw.read32(kDbi, kPortDebug0) & 0x3f) == 0x11)
            break;
        hw.sleepMs(1);
    }
    report.linkMilliseconds = ms;
    report.perstPin = pinWord(hw, kPinPerst);
    report.portDebug0 = hw.read32(kDbi, kPortDebug0);
    report.portDebug1 = hw.read32(kDbi, kPortDebug1);
    report.ltssm = hw.read32(kParf, kParfLtssm);
    report.linkControl = hw.read32(kDbi, kDbiLinkControl);
    report.parfPhyCtrl = hw.read32(kParf, kParfPhyCtrl);
    if (ms == 1000) { report.step = -16; return; }
    report.step = 16;

    // Outbound iATU region 0 turns the 1 MiB config window into type 0 accesses to bus 1 device 0.
    hw.write32(kAtu, 0x08, static_cast<uint32_t>(kConfigBase));
    hw.write32(kAtu, 0x0c, static_cast<uint32_t>(kConfigBase >> 32));
    hw.write32(kAtu, 0x10, static_cast<uint32_t>(kConfigBase + 0xfffff));
    hw.write32(kAtu, 0x20, static_cast<uint32_t>((kConfigBase + 0xfffff) >> 32));
    hw.write32(kAtu, 0x14, 1u << 24);
    hw.write32(kAtu, 0x18, 0);
    hw.write32(kAtu, 0x00, 0x4);
    hw.write32(kAtu, 0x04, 1u << 31);
    for (unsigned i = 0; i < 5 && !(hw.read32(kAtu, 0x04) >> 31); ++i)
        hw.delayUs(10);
    report.endpointId = hw.read32(kConfig, 0);
    report.step = 17;
}

// Upstream endpointProbe up to the MHI register read: the outbound memory window, BAR0 at the start of it, memory decoding and bus mastering,
// then reads of the MHI block. No firmware is loaded and no DMA is set up here.
inline void probeEndpoint(Hw &hw, Report &report)
{
    // region 1: memory, CPU window to the same PCI addresses
    const uint32_t r1 = 0x200;
    hw.write32(kAtu, r1 + 0x08, static_cast<uint32_t>(kMemBase));
    hw.write32(kAtu, r1 + 0x0c, static_cast<uint32_t>(kMemBase >> 32));
    hw.write32(kAtu, r1 + 0x10, static_cast<uint32_t>(kMemBase + kMemSize - 1));
    hw.write32(kAtu, r1 + 0x20, static_cast<uint32_t>((kMemBase + kMemSize - 1) >> 32));
    hw.write32(kAtu, r1 + 0x14, static_cast<uint32_t>(kMemBase));
    hw.write32(kAtu, r1 + 0x18, static_cast<uint32_t>(kMemBase >> 32));
    hw.write32(kAtu, r1 + 0x00, 0);
    hw.write32(kAtu, r1 + 0x04, 1u << 31);
    for (unsigned i = 0; i < 5 && !(hw.read32(kAtu, r1 + 0x04) >> 31); ++i)
        hw.delayUs(10);
    // type 1 header memory base (bits 15:4 = address bits 31:20) and limit (31:20)
    hw.write32(kDbi, kDbiMemWindow,
               static_cast<uint32_t>(((kMemBase + kMemSize - 1) & 0xfff00000u) | ((kMemBase >> 16) & 0xfff0u)));
    // BAR0: size it, put it at the start of the window
    const uint32_t saved = hw.read32(kConfig, 0x10);
    hw.write32(kConfig, 0x10, 0xffffffffu);
    const uint32_t mask = hw.read32(kConfig, 0x10);
    hw.write32(kConfig, 0x10, saved);
    report.barMask = mask;
    const uint32_t size = ~(mask & ~0xfu) + 1;
    if (!(mask & ~0xfu) || !size || size > kMemSize) { report.step = -17; return; }
    const uint32_t address = static_cast<uint32_t>((kMemBase + size - 1) & ~static_cast<uint64_t>(size - 1));
    hw.write32(kConfig, 0x10, address | (mask & 0xfu));
    if ((mask & 6u) == 4u)
        hw.write32(kConfig, 0x14, 0);
    hw.write32(kConfig, 0x04, (hw.read32(kConfig, 0x04) & 0xffffu) | 0x6u);
    report.barAddress = hw.read32(kConfig, 0x10);
    report.command = hw.read32(kConfig, 0x04);
    const uint32_t mapped = size < 0x20000 ? size : 0x20000;
    if (!hw.openBar(address, mapped)) { report.step = -17; return; }
    report.step = 18;
    static const uint32_t regs[7] = {0x00, 0x08, 0x10, 0x28, 0x2c, 0x38, 0x48};
    for (unsigned i = 0; i < 7; ++i)
        report.mhi[i] = hw.read32(kBar, regs[i]);
    report.step = 19;
    const uint32_t bhi = report.mhi[3];
    if (bhi && bhi < 0x20000 - 0x40) {
        report.bhiVersion = hw.read32(kBar, bhi + 4) | hw.read32(kBar, bhi) << 16;
        report.bhiEe = hw.read32(kBar, bhi + 0x28);
        report.bhiStatus = hw.read32(kBar, bhi + 0x2c);
        report.bhiError = hw.read32(kBar, bhi + 0x30);
    }
}

// Stage 2 and 3 entry. stage is the sheng-wifi value (>= 2). db is a kCmdDbBytes copy of the AOP command database.
inline void bringUp(Hw &hw, Arbiter &arbiter, const uint8_t *db, unsigned stage, Report &report)
{
    report = Report();
    if (!prepareSleepClock(hw, arbiter, report)) { report.step = -1; return; }
    report.step = 1;
    if (!voteRails(hw, arbiter, db, report)) { report.step = -2; return; }
    report.step = 2;
    hw.sleepMs(25);
    bringLink(hw, report);
    if (report.step == 17 && (report.endpointId & 0xffff) == (kWcn7850Id & 0xffff) && stage >= 3)
        probeEndpoint(hw, report);
}

inline void formatBringup(char *out, size_t bytes, unsigned row, const Report &r)
{
    switch (row) {
    case 0:
        snprintf(out, bytes, "st%d clk %02x rails %02x/%02x ep %08x", r.step, r.sleepClockResult, r.railsFound, r.railsEnabled, r.endpointId);
        break;
    case 1:
        snprintf(out, bytes, "gdsc %04x phy %08x pd0 %08x pd1 %08x ms %u", r.gdsc, r.phyStatus, r.portDebug0, r.portDebug1, r.linkMilliseconds);
        break;
    case 2:
        snprintf(out, bytes, "rc %08x lnk %08x ltssm %08x perst %08x wlan %08x", r.rootId, r.linkControl, r.ltssm, r.perstPin, r.wlanEnPin);
        break;
    case 3:
        snprintf(out, bytes, "bar %08x mask %08x cmd %08x mhi %08x ver %08x", r.barAddress, r.barMask, r.command, r.mhi[0], r.mhi[1]);
        break;
    default:
        snprintf(out, bytes, "bhi off %x ver %08x ee %x st %x err %x ctrl %08x sts %08x", r.mhi[3], r.bhiVersion, r.bhiEe, r.bhiStatus, r.bhiError, r.mhi[5], r.mhi[6]);
        break;
    }
}

constexpr unsigned kBringupRows = 5;

}  // namespace sheng_wcn

#endif
