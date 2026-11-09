// SPDX-License-Identifier: GPL-3.0-or-later
// Host test of platform/sheng/macos26/usb/usb_hw.h against a simulated SM8550 register bank.
#include <cassert>
#include <cstdio>
#include <map>
#include <vector>

#include "../platform/sheng/macos26/usb/usb_hw.h"

using namespace sheng_usb;

struct Sim final : Bus {
    std::map<uint64_t, uint32_t> reg;
    std::vector<std::pair<uint64_t, uint32_t>> writes;
    std::vector<uint64_t> reads;
    bool hang = false;      // the controller must not be read while unpowered
    bool gdscStuck = false;
    unsigned softResets = 0, hostSets = 0;
    uint32_t cap = 0x20, ports = 2;
    bool deviceOnPort = false;

    Sim()
    {
        reg[kCore + kGsnpsid] = 0x5533330a;
        reg[kCore] = cap | 0x01100000;  // CAPLENGTH 0x20, HCIVERSION 1.10
        reg[kCore + 4] = ports << 24 | 8;
        reg[kCore + cap + kUsbStsOffset] = kUsbStsHalted;
    }
    bool powered() const
    {
        auto on = [&](uint64_t offset) { auto it = reg.find(kGcc + offset); return it != reg.end() && (it->second & kCbcrEnable); };
        auto gdsc = reg.find(kGcc + kUsb30Gdsc);
        return gdsc != reg.end() && (gdsc->second & kGdscOn) && on(kClkMaster) && on(kClkCfgNoc) && on(kClkAggr);
    }
    uint32_t read(uint64_t pa) override
    {
        reads.push_back(pa);
        if (pa >= kCore && pa < kCore + kCoreBytes)
            assert(powered() && "the controller was accessed while unpowered");
        return reg[pa];
    }
    void write(uint64_t pa, uint32_t value) override
    {
        writes.push_back({pa, value});
        if (pa >= kCore && pa < kCore + kCoreBytes)
            assert(powered() && "the controller was written while unpowered");
        const uint64_t op = kCore + cap;
        if (pa == kGcc + kUsb30Gdsc || pa == kGcc + kUsb3PhyGdsc) {
            reg[pa] = value;
            if (!gdscStuck && !(value & kGdscCollapse))
                reg[pa] |= kGdscOn;
            return;
        }
        if (pa >= kGcc && pa < kGcc + kGccBytes) {
            reg[pa] = value;
            if (value & kCbcrEnable)
                reg[pa] &= ~kCbcrOff;
            if (pa == kGcc + kRcgMaster || pa == kGcc + kRcgMock)
                reg[pa] &= ~1u;  // the root command takes effect at once
            return;
        }
        if (pa == kCore + kGctl) {
            if (value & kGctlCoreSoftReset)
                ++softResets;
            if ((value & kGctlPrtCapMask) == kGctlPrtCapHost)
                ++hostSets;
        }
        reg[pa] = value;
        if (pa == op + kUsbCmdOffset) {
            if (value & kUsbCmdReset) {
                reg[pa] = value & ~kUsbCmdReset;
                reg[op + kUsbStsOffset] = kUsbStsHalted;
                for (uint32_t i = 0; i < ports; ++i)
                    reg[op + portsc(i)] = 0;
            }
            return;
        }
        for (uint32_t i = 0; i < ports; ++i) {
            if (pa == op + portsc(i)) {
                uint32_t v = value & ~kPortChangeBits;
                if (deviceOnPort && i == 0 && (value & kPortPower))
                    v |= kPortConnected | (3u << 10);  // high speed, connected
                reg[pa] = v;
            }
        }
    }
    void delayMicroseconds(unsigned) override {}
    void sleepMilliseconds(unsigned) override {}
};

static void testReadOnlyStage()
{
    Sim sim;
    const Power power = readPower(sim);
    const Core core = readCore(sim, power);
    assert(!core.accessible);
    assert(sim.writes.empty());
    for (uint64_t pa : sim.reads)
        assert(pa < kCore || pa >= kCore + kCoreBytes);  // no controller reads while unpowered

    // Powered on (GDSC on, clocks running): the registers are read.
    sim.reg[kGcc + kUsb30Gdsc] = kGdscOn;
    for (uint32_t offset : {kClkMaster, kClkCfgNoc, kClkAggr})
        sim.reg[kGcc + offset] = kCbcrEnable;
    sim.reads.clear();
    const Power on = readPower(sim);
    const Core read = readCore(sim, on);
    assert(read.accessible && read.gsnpsid == 0x5533330a);
    assert(!read.host);
    assert(sim.writes.empty());

    // A clock that reports off refuses the read.
    sim.reg[kGcc + kClkCfgNoc] = kCbcrEnable | kCbcrOff;
    assert(!readCore(sim, readPower(sim)).accessible);
    sim.reg[kGcc + kClkCfgNoc] = kCbcrEnable;
    sim.reg[kGcc + kUsb30Gdsc] = 0;
    assert(!readCore(sim, readPower(sim)).accessible);
}

static void testPowerUp()
{
    Sim sim;
    assert(powerUp(sim) == Step::none);
    assert(sim.reg[kGcc + kUsb30Gdsc] & kGdscRetain);
    assert(sim.reg[kGcc + kUsb30Gdsc] & kGdscOn);
    assert(clockRunning(sim.reg[kGcc + kClkMaster]));
    assert((sim.reg[kGcc + kRcgMaster + 4] & kRcgCfgMask) == kRcgMasterCfg);
    assert(sim.reg[kTcsr + kTcsrUsb2Ref] & 1u);
    assert(!(sim.reg[kGcc + kUsb30Bcr] & 1u) && !(sim.reg[kGcc + kQusb2PhyBcr] & 1u));
    assert(coreAccessible(readPower(sim)));

    Sim failing;
    failing.gdscStuck = true;
    assert(powerUp(failing) == Step::gdscPhy);

    Sim badId;
    badId.reg[kCore + kGsnpsid] = 0xdead0000;
    assert(powerUp(badId) == Step::coreId);
}

static void testHostAndPorts()
{
    Sim sim;
    assert(powerUp(sim) == Step::none);
    selectUtmi(sim);
    assert(sim.reg[kScratch + kScratchGeneral] & (kGeneralPipeUtmiSel | kGeneralPipe3PhyStatusSw));
    assert(!(sim.reg[kScratch + kScratchGeneral] & kGeneralPipeHsSel));
    sim.reg[kScratch + kScratchHsCtl] = kHsVbusOverride;
    sim.reg[kScratch + kScratchSsCtl] = kSsVbusOverride;
    sim.reg[kCore + kGusb2PhyCfg] = kGusb2SusPhy | kGusb2EnblSlpM;
    sim.reg[kCore + kGusb3PipeCtl] = kGusb3SusPhy;
    setHostRole(sim);
    assert(sim.softResets == 1 && sim.hostSets >= 1);
    assert((sim.reg[kCore + kGctl] & kGctlPrtCapMask) == kGctlPrtCapHost);
    assert(!(sim.reg[kCore + kGctl] & kGctlCoreSoftReset));
    assert(!(sim.reg[kCore + kGusb2PhyCfg] & (kGusb2SusPhy | kGusb2EnblSlpM)));
    assert(!(sim.reg[kCore + kGusb3PipeCtl] & kGusb3SusPhy));
    assert(!(sim.reg[kScratch + kScratchHsCtl] & kHsVbusOverride) && !(sim.reg[kScratch + kScratchSsCtl] & kSsVbusOverride));
    uint32_t cap = 0;
    sim.deviceOnPort = true;
    assert(xhciResetAndPower(sim, &cap) == Step::none);
    assert(cap == 0x20);
    const uint64_t op = kCore + cap;
    assert(sim.reg[op + portsc(0)] & kPortPower);
    assert(sim.reg[op + portsc(0)] & kPortConnected);
    assert(portSpeed(sim.reg[op + portsc(0)]) == 3);
    assert(sim.reg[op + portsc(1)] & kPortPower);
    assert(!(sim.reg[op + portsc(1)] & kPortConnected));
    assert(!(sim.reg[op + kUsbCmdOffset] & kUsbCmdRun));  // never started: no rings, no DMA
    // Core id and registers read in host mode decode the capability length.
    const Core host = readCore(sim, readPower(sim));
    assert(host.host && host.capLength == 0x20 && host.hcs1 >> 24 == 2);
    assert(host.ports[0] & kPortConnected);
}

static void testPortscBits()
{
    // Writing back a PORTSC with all status change bits set must clear none of them unintentionally, keep the writable
    // bits, and must never write the RW1C port-enable bit as one (which would disable the port).
    const uint32_t raw = 0xffffffffu;
    const uint32_t out = portPowerOn(raw);
    assert(!(out & kPortEnabled));
    assert(!(out & kPortChangeBits));
    assert(out & kPortPower);
    assert(out & (1u << 0));   // CCS is RO, carried
    assert(portSpeed(0x00000c00) == 3);
    assert(portLinkState(0x000000a0) == 5);
    assert(validCoreId(0x5533290a) && validCoreId(0x33310123) && validCoreId(0x33320123));
    assert(!validCoreId(0xdead0000) && !validCoreId(0));
}

int main()
{
    testReadOnlyStage();
    testPowerUp();
    testHostAndPorts();
    testPortscBits();
    puts("PASS");
}
