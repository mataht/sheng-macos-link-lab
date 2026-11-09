/* SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef SHENG_USB_HW_H
#define SHENG_USB_HW_H

/*
 * SM8550 "kalama" DWC3 USB controller: the register work of the NativeUsb bring-up, as pure logic over a physical-address
 * accessor so the host tests can drive a simulated register bank. No IOKit here.
 *
 * Addresses and sequences follow upstream UniXNU QcomDwc3 (QcomDwc3.cpp, BSD-2-Clause-Patent, commit e4efe95c) and, through
 * it, mainline Linux (gcc-sm8550.c, dwc3-qcom.c, phy-qcom-eusb2-repeater.c, xhci.h). Base address and interrupt checked
 * offline against this tablet's Android device tree (vendor_boot_a /soc/ssusb@a600000: reg 0x0a600000, SPI 133).
 *
 * Stages (native-usb=N):
 *   1  read-only: GCC clock/GDSC state, TCSR clock references; the controller's registers only when its power domain
 *      and clocks are on, because an access to an unclocked block can hang the bus.
 *   2  rails, power domains, clocks and resets. Controller registers are then readable.
 * The host-role helpers below (selectUtmi, setHostRole, xhciResetAndPower) are for a later stage; only the host test uses them now.
 */

#include <stddef.h>
#include <stdint.h>

namespace sheng_usb {

constexpr uint64_t kGcc = 0x00100000, kTcsr = 0x01fd5000, kCore = 0x0a600000, kPhy = 0x088e3000;
constexpr uint64_t kScratch = kCore + 0xf8800;
constexpr uint32_t kGccBytes = 0x1f0000, kTcsrBytes = 0x1000, kCoreBytes = 0xf9000, kPhyBytes = 0x1000;

// GCC (gcc-sm8550.c).
constexpr uint32_t kUsb30Bcr = 0x39000, kUsb30Gdsc = 0x39004, kUsb3PhyGdsc = 0x50018, kQusb2PhyBcr = 0x12000;
constexpr uint32_t kClkMaster = 0x39018, kClkSleep = 0x39024, kClkMockUtmi = 0x39028, kClkCfgNoc = 0x39088, kClkAggr = 0x3908c;
constexpr uint32_t kClkPhyAux = 0x39060, kClkPhyCom = 0x39064, kClkPhyPipe = 0x39068;
constexpr uint32_t kRcgMaster = 0x3902c, kRcgMock = 0x39044;  // CMD_RCGR; CFG_RCGR is +4
constexpr uint32_t kRcgCfgMask = 0x71f;
constexpr uint32_t kRcgMasterCfg = 1u << 8 | 5u, kRcgMockCfg = 0;  // GPLL0 / 3 = 200 MHz; XO 19.2 MHz
constexpr uint32_t kCbcrEnable = 1u << 0, kCbcrOff = 1u << 31, kGdscOn = 1u << 31, kGdscCollapse = 1u << 0, kGdscRetain = 1u << 11;

// TCSR reference clock enables (tcsrcc-sm8550.c: TCSR_USB3_CLKREF_EN, TCSR_USB2_CLKREF_EN).
constexpr uint32_t kTcsrUsb3Ref = 0x108, kTcsrUsb2Ref = 0x118;

// DWC3 global registers, from the controller base (the host registers occupy 0 to 0x7fff).
constexpr uint32_t kGctl = 0xc110, kGsnpsid = 0xc120, kGhwParams0 = 0xc140, kGhwParams3 = 0xc14c;
constexpr uint32_t kGusb2PhyCfg = 0xc200, kGusb3PipeCtl = 0xc2c0, kDctl = 0xc704, kDsts = 0xc70c;
constexpr uint32_t kGctlCoreSoftReset = 1u << 11, kGctlPrtCapMask = 3u << 12, kGctlPrtCapHost = 1u << 12;
constexpr uint32_t kGusb2SusPhy = 1u << 6, kGusb2EnblSlpM = 1u << 8, kGusb3SusPhy = 1u << 17;

// QSCRATCH (dwc3-qcom.c), from kScratch.
constexpr uint32_t kScratchHsCtl = 0x10, kScratchGeneral = 0x08, kScratchSsCtl = 0x30;
constexpr uint32_t kGeneralPipeUtmiSel = 1u << 0, kGeneralPipe3PhyStatusSw = 1u << 3, kGeneralPipeHsSel = 1u << 8;
constexpr uint32_t kHsVbusOverride = 1u << 20 | 1u << 28, kSsVbusOverride = 1u << 24;

// xHCI capability and operational registers (from the controller base).
constexpr uint32_t kUsbCmdOffset = 0, kUsbStsOffset = 4, kPortOffset = 0x400, kPortStride = 0x10;
constexpr uint32_t kUsbCmdRun = 1u << 0, kUsbCmdReset = 1u << 1, kUsbStsHalted = 1u << 0, kUsbStsNotReady = 1u << 11;
constexpr uint32_t kPortConnected = 1u << 0, kPortEnabled = 1u << 1, kPortPower = 1u << 9;
constexpr uint32_t kPortChangeBits = 0x7fu << 17;
// xhci.h: XHCI_PORT_RO and XHCI_PORT_RWS; everything else in PORTSC is RW1C or read as zero and must be written as zero.
constexpr uint32_t kPortPreserved = (1u << 0 | 1u << 3 | 0xfu << 10 | 1u << 30) | (0xfu << 5 | 1u << 9 | 0x3u << 14 | 0x7u << 25);

constexpr unsigned kPortsReported = 2;

// PMIC SPMI arbiter v7 (sm8550.dtsi); eUSB2 repeater of the PM8550B at SID 7, 0xfd00.
constexpr uint64_t kSpmiCore = 0x0c400000, kSpmiCnfg = 0x0c42d000, kSpmiObs = 0x0c440000, kSpmiChn = 0x0c500000;
constexpr uint32_t kSpmiCoreBytes = 0x3000, kSpmiCnfgBytes = 0x4000, kSpmiObsBytes = 0x8000, kSpmiChnStride = 0x1000;
constexpr uint16_t kRepeaterSid = 7, kRepeaterPeripheral = 0xfd;

struct Bus {
    virtual uint32_t read(uint64_t pa) = 0;
    virtual void write(uint64_t pa, uint32_t value) = 0;
    virtual void delayMicroseconds(unsigned us) = 0;
    virtual void sleepMilliseconds(unsigned ms) = 0;
    virtual ~Bus() = default;
};

constexpr uint32_t portsc(unsigned port) { return kPortOffset + port * kPortStride; }

inline uint32_t portNeutral(uint32_t value) { return value & kPortPreserved; }
inline uint32_t portPowerOn(uint32_t value) { return portNeutral(value) | kPortPower; }
inline unsigned portSpeed(uint32_t value) { return (value >> 10) & 0xf; }
inline unsigned portLinkState(uint32_t value) { return (value >> 5) & 0xf; }
inline const char *speedName(unsigned speed)
{
    switch (speed) {
    case 1: return "FS";
    case 2: return "LS";
    case 3: return "HS";
    case 4: return "SS";
    default: return "?";
    }
}

struct Power {
    uint32_t gdscUsb30 = 0, gdscPhy = 0;
    uint32_t master = 0, sleep = 0, mockUtmi = 0, cfgNoc = 0, aggr = 0, phyAux = 0, phyCom = 0, phyPipe = 0;
    uint32_t rcgMaster = 0, rcgMasterCfg = 0, rcgMock = 0, tcsrUsb2 = 0, tcsrUsb3 = 0;
};

inline Power readPower(Bus &bus)
{
    Power p;
    p.gdscUsb30 = bus.read(kGcc + kUsb30Gdsc);
    p.gdscPhy = bus.read(kGcc + kUsb3PhyGdsc);
    p.master = bus.read(kGcc + kClkMaster);
    p.sleep = bus.read(kGcc + kClkSleep);
    p.mockUtmi = bus.read(kGcc + kClkMockUtmi);
    p.cfgNoc = bus.read(kGcc + kClkCfgNoc);
    p.aggr = bus.read(kGcc + kClkAggr);
    p.phyAux = bus.read(kGcc + kClkPhyAux);
    p.phyCom = bus.read(kGcc + kClkPhyCom);
    p.phyPipe = bus.read(kGcc + kClkPhyPipe);
    p.rcgMaster = bus.read(kGcc + kRcgMaster);
    p.rcgMasterCfg = bus.read(kGcc + kRcgMaster + 4);
    p.rcgMock = bus.read(kGcc + kRcgMock);
    p.tcsrUsb2 = bus.read(kTcsr + kTcsrUsb2Ref);
    p.tcsrUsb3 = bus.read(kTcsr + kTcsrUsb3Ref);
    return p;
}

inline bool clockRunning(uint32_t cbcr) { return (cbcr & kCbcrEnable) && !(cbcr & kCbcrOff); }

// An access to the controller needs the power domain up and the clocks of its register path running. The strict test may
// refuse a clock that is hardware gated while idle; the state word is printed either way, so a refusal is itself a result.
inline bool coreAccessible(const Power &p)
{
    return (p.gdscUsb30 & kGdscOn) && clockRunning(p.master) && clockRunning(p.cfgNoc) && clockRunning(p.aggr);
}

struct Core {
    bool accessible = false;
    uint32_t gsnpsid = 0, gctl = 0, usb2 = 0, usb3 = 0, hw0 = 0, hw3 = 0, dctl = 0, dsts = 0;
    uint32_t scratchHs = 0, scratchSs = 0, scratchGeneral = 0;
    bool host = false;
    uint32_t capLength = 0, hcs1 = 0, usbCmd = 0, usbSts = 0;
    uint32_t ports[kPortsReported] = {};
};

inline Core readCore(Bus &bus, const Power &p)
{
    Core c;
    if (!coreAccessible(p))
        return c;
    c.accessible = true;
    c.gsnpsid = bus.read(kCore + kGsnpsid);
    c.gctl = bus.read(kCore + kGctl);
    c.usb2 = bus.read(kCore + kGusb2PhyCfg);
    c.usb3 = bus.read(kCore + kGusb3PipeCtl);
    c.hw0 = bus.read(kCore + kGhwParams0);
    c.hw3 = bus.read(kCore + kGhwParams3);
    c.dctl = bus.read(kCore + kDctl);
    c.dsts = bus.read(kCore + kDsts);
    c.scratchHs = bus.read(kScratch + kScratchHsCtl);
    c.scratchSs = bus.read(kScratch + kScratchSsCtl);
    c.scratchGeneral = bus.read(kScratch + kScratchGeneral);
    c.host = (c.gctl & kGctlPrtCapMask) == kGctlPrtCapHost;
    if (c.host) {
        c.capLength = bus.read(kCore) & 0xff;
        c.hcs1 = bus.read(kCore + 4);
        c.usbCmd = bus.read(kCore + c.capLength + kUsbCmdOffset);
        c.usbSts = bus.read(kCore + c.capLength + kUsbStsOffset);
        for (unsigned i = 0; i < kPortsReported; ++i)
            c.ports[i] = bus.read(kCore + c.capLength + portsc(i));
    }
    return c;
}

// A controller that answers with the Synopsys identifiers (dwc3: "U3"/"31"/"32" in the top half of GSNPSID).
inline bool validCoreId(uint32_t gsnpsid)
{
    const uint32_t tag = gsnpsid & 0xffff0000u;
    return tag == 0x55330000u || tag == 0x33310000u || tag == 0x33320000u;
}

inline bool waitBits(Bus &bus, uint64_t pa, uint32_t mask, uint32_t want, unsigned ms)
{
    for (unsigned i = 0; i < ms * 10; ++i) {
        if ((bus.read(pa) & mask) == want)
            return true;
        bus.delayMicroseconds(100);
    }
    return (bus.read(pa) & mask) == want;
}

inline void setBits(Bus &bus, uint64_t pa, uint32_t clear, uint32_t set)
{
    bus.write(pa, (bus.read(pa) & ~clear) | set);
}

enum class Step : int { none = 0, rails = 1, gdscPhy = 2, gdscUsb30 = 3, rcg = 4, clock = 5, coreId = 6, repeater = 7, phy = 8, role = 9, reset = 10, ports = 11 };

inline const char *stepName(Step step)
{
    switch (step) {
    case Step::none: return "none";
    case Step::rails: return "rails";
    case Step::gdscPhy: return "gdsc-phy";
    case Step::gdscUsb30: return "gdsc-usb30";
    case Step::rcg: return "rcg";
    case Step::clock: return "branch-clock";
    case Step::coreId: return "core-id";
    case Step::repeater: return "repeater";
    case Step::phy: return "phy";
    case Step::role: return "host-role";
    case Step::reset: return "xhci-reset";
    case Step::ports: return "port-power";
    }
    return "?";
}

inline bool gdscOn(Bus &bus, uint32_t offset)
{
    setBits(bus, kGcc + offset, kGdscCollapse, kGdscRetain);
    return waitBits(bus, kGcc + offset, kGdscOn, kGdscOn, 100);
}

inline bool branchOn(Bus &bus, uint32_t offset)
{
    setBits(bus, kGcc + offset, 0, kCbcrEnable);
    return waitBits(bus, kGcc + offset, kCbcrOff, 0, 100);
}

inline bool rcgSet(Bus &bus, uint32_t cmd, uint32_t cfg)
{
    if ((bus.read(kGcc + cmd + 4) & kRcgCfgMask) == cfg)
        return true;
    setBits(bus, kGcc + cmd + 4, kRcgCfgMask, cfg);
    setBits(bus, kGcc + cmd, 0, 1u);
    return waitBits(bus, kGcc + cmd, 1u, 0, 10);
}

// Stage 2: the power domains, clocks and resets, after the rails. Returns the step that failed, or Step::none.
inline Step powerUp(Bus &bus)
{
    if (!gdscOn(bus, kUsb3PhyGdsc))
        return Step::gdscPhy;
    if (!gdscOn(bus, kUsb30Gdsc))
        return Step::gdscUsb30;
    setBits(bus, kGcc + kUsb30Bcr, 0, 1u);
    bus.delayMicroseconds(20);
    setBits(bus, kGcc + kUsb30Bcr, 1u, 0);
    if (!rcgSet(bus, kRcgMaster, kRcgMasterCfg) || !rcgSet(bus, kRcgMock, kRcgMockCfg))
        return Step::rcg;
    static const uint32_t branches[] = {kClkCfgNoc, kClkMaster, kClkAggr, kClkSleep, kClkMockUtmi};
    for (uint32_t offset : branches)
        if (!branchOn(bus, offset))
            return Step::clock;
    setBits(bus, kTcsr + kTcsrUsb2Ref, 0, 1u);
    setBits(bus, kTcsr + kTcsrUsb3Ref, 0, 1u);
    setBits(bus, kGcc + kQusb2PhyBcr, 0, 1u);
    bus.delayMicroseconds(150);
    setBits(bus, kGcc + kQusb2PhyBcr, 1u, 0);
    if (!validCoreId(bus.read(kCore + kGsnpsid)))
        return Step::coreId;
    return Step::none;
}

struct MaskedWrite {
    uint16_t offset;
    uint8_t mask, value;
};

// dwc3_qcom_select_utmi_clk: no PIPE clock without the QMP PHY, so the USB 3 side runs from the UTMI clock.
inline void selectUtmi(Bus &bus)
{
    setBits(bus, kScratch + kScratchGeneral, 0, kGeneralPipeHsSel);
    bus.sleepMilliseconds(1);
    setBits(bus, kScratch + kScratchGeneral, 0, kGeneralPipeUtmiSel | kGeneralPipe3PhyStatusSw);
    bus.sleepMilliseconds(1);
    setBits(bus, kScratch + kScratchGeneral, kGeneralPipeHsSel, 0);
}

inline void applyPhy(Bus &bus, const MaskedWrite *table, size_t count)
{
    for (size_t i = 0; i < count; ++i)
        setBits(bus, kPhy + table[i].offset, table[i].mask, table[i].value);
}

// __dwc3_set_mode: core soft reset, then the host port capability; snps,dis_u2_susphy_quirk and snps,dis_enblslpm_quirk.
inline void setHostRole(Bus &bus)
{
    setBits(bus, kCore + kGctl, 0, kGctlCoreSoftReset);
    bus.sleepMilliseconds(100);
    setBits(bus, kCore + kGctl, kGctlCoreSoftReset, 0);
    setBits(bus, kCore + kGctl, kGctlPrtCapMask, kGctlPrtCapHost);
    setBits(bus, kCore + kGusb2PhyCfg, kGusb2SusPhy | kGusb2EnblSlpM, 0);
    setBits(bus, kCore + kGusb3PipeCtl, kGusb3SusPhy, 0);
    // VBUS override is for the device role: the controller must not report a host on the cable while it is one.
    setBits(bus, kScratch + kScratchHsCtl, kHsVbusOverride, 0);
    setBits(bus, kScratch + kScratchSsCtl, kSsVbusOverride, 0);
}

// xHCI halt and reset, then power on every root hub port. No ring is set up: nothing is ever started, so no DMA.
inline Step xhciResetAndPower(Bus &bus, uint32_t *capOut)
{
    const uint32_t cap = bus.read(kCore) & 0xff;
    if (capOut)
        *capOut = cap;
    if (cap == 0)
        return Step::reset;
    const uint64_t op = kCore + cap;
    setBits(bus, op + kUsbCmdOffset, kUsbCmdRun, 0);
    if (!waitBits(bus, op + kUsbStsOffset, kUsbStsHalted, kUsbStsHalted, 500))
        return Step::reset;
    setBits(bus, op + kUsbCmdOffset, 0, kUsbCmdReset);
    if (!waitBits(bus, op + kUsbCmdOffset, kUsbCmdReset, 0, 1000) || !waitBits(bus, op + kUsbStsOffset, kUsbStsNotReady, 0, 1000))
        return Step::reset;
    const uint32_t ports = (bus.read(kCore + 4) >> 24) & 0xff;
    if (ports == 0 || ports > 15)
        return Step::ports;
    for (uint32_t i = 0; i < ports; ++i)
        bus.write(op + portsc(i), portPowerOn(bus.read(op + portsc(i))));
    return Step::none;
}

}  // namespace sheng_usb

#endif
