/* SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef SHENG_SPMI_KEYS_H
#define SHENG_SPMI_KEYS_H

/*
 * Read-only polling of the sheng hardware keys through the Qualcomm SPMI PMIC arbiter v7 (SM8550 "kalama").
 * Pure logic over an MMIO accessor so the host tests can drive a simulated arbiter; no IOKit here.
 *
 * Sources (offline, recorded in hw-build/step17-input-investigation.md):
 *  - SPMI arbiter @ /soc/qcom,spmi@c42d000: cnfg 0xc42d000, core 0xc400000 (0x3000), obsrvr 0xc440000, qcom,ee = 0.
 *  - Linux drivers/spmi/spmi-pmic-arb.c (v7): version core[0x0] in [0x70000000, 0x80000000); APID count core[0x4] & 0x7ff;
 *    APID map core[0x2000 + 4 * apid], ppid = (map >> 8) & 0xfff, empty when zero; owner EE cnfg[4 * apid] & 7;
 *    observer channel obsrvr + 0x8000 * ee + 0x20 * apid; CMD 0x00, STATUS 0x08, RDATA0 0x18;
 *    read command (EXT_READL = 1) << 27 | (addr & 0xff) << 4 | (bytes - 1); STATUS bit0 done, bit1 failure,
 *    bit2 denied, bit3 dropped; up to 1000 polls of 1 us.
 *  - Linux pm8941-pwrkey.c: PON_RT_STS 0x10, gen3 KPDPWR bit 7, RESIN bit 6 (set = pressed); PMK8550 (SID 0) pon_hlos 0x1300.
 *  - Linux pinctrl-spmi-gpio.c: GPIO n at base + (n - 1) * 0x100, input level RT_STS 0x10 bit 0; vol-up is PM8550 (SID 1)
 *    GPIO6 @ 0x8d00, active low with pull-up.
 * Only reads are issued. No arbiter write channel, PMIC register write or interrupt configuration is used.
 */

#include <stddef.h>
#include <stdint.h>

namespace sheng_keys {

constexpr uint32_t kVersionMin = 0x70000000, kVersionMax = 0x80000000;
constexpr uint32_t kFeatures = 0x4, kApidMapBase = 0x2000, kChannelStride = 0x20, kEeStride = 0x8000;
constexpr uint32_t kCmd = 0x00, kStatus = 0x08, kRdata0 = 0x18;
constexpr uint32_t kOpExtReadLong = 1;
constexpr unsigned kPollLimit = 1000;

constexpr uint16_t kPonSid = 0, kPonBase = 0x1300, kPonRtSts = 0x10;
constexpr uint8_t kPonPowerBit = 0x80, kPonResinBit = 0x40;
constexpr uint16_t kGpioSid = 1, kVolUpGpio = 6, kGpioBase = 0x8800, kGpioRtSts = 0x10;

struct Mmio {
    // Accessors over device mappings; offsets are byte offsets from each region's base.
    virtual uint32_t core(uint32_t offset) = 0;
    virtual uint32_t cnfg(uint32_t offset) = 0;
    virtual uint32_t readObs(uint32_t offset) = 0;
    virtual void writeObs(uint32_t offset, uint32_t value) = 0;
    virtual void delayMicroseconds(unsigned) = 0;
    virtual ~Mmio() = default;
};

struct Arbiter {
    Mmio *io = nullptr;
    uint32_t coreBytes = 0, cnfgBytes = 0, obsBytes = 0, ee = 0;
    uint32_t version = 0, apids = 0;
};

enum class Status : int { ok = 0, unsupported = -1, notFound = -2, timeout = -3, failure = -4, denied = -5, dropped = -6 };

inline Status probe(Arbiter &a)
{
    if (!a.io || a.coreBytes < kApidMapBase + 4 || a.ee > 5)
        return Status::unsupported;
    a.version = a.io->core(0);
    if (a.version < kVersionMin || a.version >= kVersionMax)
        return Status::unsupported;
    a.apids = a.io->core(kFeatures) & 0x7ff;
    const uint32_t mapped = (a.coreBytes - kApidMapBase) / 4;
    if (a.apids > mapped)
        a.apids = mapped;
    return a.apids ? Status::ok : Status::unsupported;
}

// Linux _pmic_arb_read_apid_map: the first entry with the PPID wins unless a later one is owned (write EE) by our EE.
inline int findApid(Arbiter &a, uint16_t sid, uint16_t peripheral)
{
    const uint32_t ppid = (static_cast<uint32_t>(sid) << 8) | peripheral;
    int chosen = -1;
    for (uint32_t apid = 0; apid < a.apids; ++apid) {
        const uint32_t map = a.io->core(kApidMapBase + 4 * apid);
        if (!map || ((map >> 8) & 0xfff) != ppid)
            continue;
        const bool owned = 4 * apid + 4 <= a.cnfgBytes && (a.io->cnfg(4 * apid) & 7) == a.ee;
        if (chosen < 0 || owned)
            chosen = static_cast<int>(apid);
        if (owned)
            break;
    }
    if (chosen >= 0 && kEeStride * a.ee + kChannelStride * static_cast<uint32_t>(chosen) + kRdata0 + 4 > a.obsBytes)
        return -1;
    return chosen;
}

constexpr uint32_t readCommand(uint16_t address) { return kOpExtReadLong << 27 | (address & 0xffu) << 4; }

inline Status readByte(Arbiter &a, int apid, uint16_t address, uint8_t *value)
{
    if (apid < 0)
        return Status::notFound;
    const uint32_t channel = kEeStride * a.ee + kChannelStride * static_cast<uint32_t>(apid);
    a.io->writeObs(channel + kCmd, readCommand(address));
    for (unsigned poll = 0; poll < kPollLimit; ++poll) {
        const uint32_t status = a.io->readObs(channel + kStatus);
        if (status & 1) {
            if (status & 2) return Status::failure;
            if (status & 4) return Status::denied;
            if (status & 8) return Status::dropped;
            *value = static_cast<uint8_t>(a.io->readObs(channel + kRdata0) & 0xff);
            return Status::ok;
        }
        a.io->delayMicroseconds(1);
    }
    return Status::timeout;
}

struct Keys {
    bool power = false, volumeDown = false, volumeUp = false;
};

struct Channels {
    int pon = -1, gpio = -1;
};

inline Channels resolve(Arbiter &a)
{
    Channels c;
    c.pon = findApid(a, kPonSid, kPonBase >> 8);
    c.gpio = findApid(a, kGpioSid, (kGpioBase + (kVolUpGpio - 1) * 0x100) >> 8);
    return c;
}

// Returns false when the PON (power / volume-down) read fails; volume-up stays released when its GPIO cannot be read.
inline bool sample(Arbiter &a, const Channels &c, Keys *keys, Status *ponStatus, Status *gpioStatus)
{
    uint8_t pon = 0, gpio = 1;
    *ponStatus = readByte(a, c.pon, kPonBase + kPonRtSts, &pon);
    *gpioStatus = c.gpio >= 0 ? readByte(a, c.gpio, kGpioBase + (kVolUpGpio - 1) * 0x100 + kGpioRtSts, &gpio) : Status::notFound;
    if (*ponStatus != Status::ok)
        return false;
    keys->power = pon & kPonPowerBit;
    keys->volumeDown = pon & kPonResinBit;
    keys->volumeUp = *gpioStatus == Status::ok && !(gpio & 1);
    return true;
}

// HID usages of the boot keyboard report.
constexpr uint8_t kUsageReturn = 0x28, kUsageTab = 0x2b, kUsageDown = 0x51, kUsageUp = 0x52;

// Two equal consecutive samples (one poll period apart) are required before a change is reported.
struct Debounce {
    Keys stable, pending;
    unsigned count = 0;
    bool update(const Keys &now)
    {
        const bool same = now.power == pending.power && now.volumeDown == pending.volumeDown && now.volumeUp == pending.volumeUp;
        pending = now;
        count = same ? count + 1 : 0;
        if (count < 1)
            return false;
        const bool changed = now.power != stable.power || now.volumeDown != stable.volumeDown || now.volumeUp != stable.volumeUp;
        stable = now;
        return changed;
    }
};

inline bool sameKeys(const Keys &a, const Keys &b)
{
    return a.power == b.power && a.volumeDown == b.volumeDown && a.volumeUp == b.volumeUp;
}

// Both volume keys are Tab, but two keys never go down in the same poll. Reporting the first key at once would send an
// arrow before Tab (on hardware this moved the list selection). A lone volume key is therefore held back for kWindowPolls
// polls: if the other volume key joins within that window only Tab is reported, held until both keys are up; if the lone
// key is released inside the window it is reported as a tap (press, then release). Power is never delayed.
// update() takes the debounced state once per poll and returns how many reports (at most 3) to send, in order.
struct Chord {
    static constexpr unsigned kWindowPolls = 4;   // 80 ms at the 20 ms poll
    Keys shown;               // what the last report described
    unsigned waiting = 0;     // polls a lone volume key has been held back (0: none)
    bool pendingUp = false;   // which volume key is held back
    bool chord = false;       // both volume keys reported as Tab until both are up
    unsigned update(const Keys &now, Keys out[3])
    {
        unsigned n = 0;
        auto emit = [&](const Keys &k) {
            if (!sameKeys(k, shown)) {
                out[n++] = k;
                shown = k;
            }
        };
        auto tap = [&]() {   // the held-back key goes down with the power state already shown, then the caller releases it
            Keys k = shown;
            k.volumeUp = pendingUp;
            k.volumeDown = !pendingUp;
            emit(k);
            waiting = 0;
        };
        Keys next = shown;
        next.power = now.power;
        if (chord) {
            if (!now.volumeUp && !now.volumeDown) {
                chord = false;
                next.volumeUp = next.volumeDown = false;
            }
        } else if (now.volumeUp && now.volumeDown) {
            chord = true;
            waiting = 0;
            next.volumeUp = next.volumeDown = true;
        } else if (now.volumeUp || now.volumeDown) {
            const bool up = now.volumeUp;
            if (!(up ? shown.volumeUp : shown.volumeDown)) {
                if (waiting && pendingUp != up)   // the other key alone: the first one was a tap
                    tap();
                pendingUp = up;
                if (++waiting >= kWindowPolls) {
                    waiting = 0;
                    next.volumeUp = up;
                    next.volumeDown = !up;
                } else {
                    next.volumeUp = next.volumeDown = false;
                }
            }
        } else {
            if (waiting)   // released inside the window
                tap();
            next.volumeUp = next.volumeDown = false;
        }
        emit(next);
        return n;
    }
};

// Boot keyboard input report: modifiers, reserved, six key slots. Both volume keys together are Tab (move focus).
inline void report(const Keys &k, uint8_t out[8])
{
    for (unsigned i = 0; i < 8; ++i)
        out[i] = 0;
    unsigned slot = 2;
    if (k.volumeUp && k.volumeDown) {
        out[slot++] = kUsageTab;
    } else {
        if (k.volumeUp) out[slot++] = kUsageUp;
        if (k.volumeDown) out[slot++] = kUsageDown;
    }
    if (k.power) out[slot++] = kUsageReturn;
}

}  // namespace sheng_keys

#endif
