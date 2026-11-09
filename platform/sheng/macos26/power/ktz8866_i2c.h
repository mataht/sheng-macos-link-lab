/* SPDX-License-Identifier: BSD-2-Clause-Patent */
#ifndef SHENG_KTZ8866_I2C_H
#define SHENG_KTZ8866_I2C_H

/*
 * Panel backlight of the sheng tablet: two Kinetic KTZ8866 drivers at I2C address 0x11, one on the QUP wrap1 serial engine 1
 * (i2c@a84000, "ktz8866a") and one on the I2C hub engine 2 (i2c@988000, "ktz8866b"), both given the same 11-bit level.
 * Pure logic over a register accessor so the host test can drive a simulated engine; no IOKit here.
 *
 * Derived from UniXNU-sheng package/AppleARMBacklight.cpp at 2b7a5815 (BSD-2-Clause-Patent), which observed these engines on the
 * same board: GENI I2C firmware left loaded and clocked by the boot loaders, FIFO transfers, no change to the clock or SCL timing.
 * Device tree check (offline, sheng dtbo): ktz,ktz8866 at 0x11 on qupv3_se1_i2c and qupv3_hub_i2c2.
 * Registers: Linux ktz8866 driver and upstream, BL_BRT_LSB 0x04 (bits 2:0) and BL_BRT_MSB 0x05 (bits 7:0).
 * The enable register and the display bias registers are never written, so the level is the only thing this code can change.
 */

#include <stdint.h>

namespace sheng_backlight {

constexpr uint32_t kFwRevision = 0x68, kIfDisable = 0x64, kClkCfg = 0x48, kDmaMode = 0x258, kByteGran = 0x254;
constexpr uint32_t kTxPack0 = 0x260, kTxPack1 = 0x264, kRxPack0 = 0x284, kRxPack1 = 0x288, kSclCounters = 0x278;
constexpr uint32_t kTxLen = 0x26c, kRxLen = 0x270, kCmd0 = 0x600, kIrqStatus = 0x610, kIrqClear = 0x618;
constexpr uint32_t kTxFifo = 0x700, kRxFifo = 0x780, kRxFifoStatus = 0x804, kTxWatermark = 0x80c;
constexpr uint32_t kCmdDone = 1u << 0, kTxWatermarkIrq = 1u << 30, kErrors = (1u << 1) | (1u << 2) | (1u << 3) | (0x3fu << 9);
constexpr uint32_t kProtoI2c = 3, kOpWrite = 1, kOpRead = 2, kStopStretch = 1u << 2;
constexpr uint8_t kAddress = 0x11, kBrtLsb = 0x04, kBrtMsb = 0x05;
constexpr unsigned kSpins = 200000;

constexpr uint32_t kMaxLevel = 2047;
// The panel must stay visible whatever is asked for. 64 of 2047 is about 3 %; Android's own start level for this panel
// (qcom,mdss-brightness-init-level 0x133 of 0xfff) is about 7.5 %, its minimum (0x02) is not meant to be read by.
constexpr uint32_t kMinVisibleLevel = 64;

inline uint32_t clampLevel(int64_t requested)
{
    if (requested < static_cast<int64_t>(kMinVisibleLevel))
        return kMinVisibleLevel;
    if (requested > static_cast<int64_t>(kMaxLevel))
        return kMaxLevel;
    return static_cast<uint32_t>(requested);
}

constexpr uint8_t lsbOf(uint32_t level) { return static_cast<uint8_t>(level & 7); }
constexpr uint8_t msbOf(uint32_t level) { return static_cast<uint8_t>((level >> 3) & 0xff); }
constexpr uint32_t levelOf(uint8_t lsb, uint8_t msb) { return (static_cast<uint32_t>(msb) << 3) | (lsb & 7u); }

enum class Error : int { none = 0, watermarkTimeout = 11, watermarkFailed = 12, doneTimeout = 21, doneFailed = 22,
                         readTimeout = 31, readFailed = 32, readEmpty = 40, notI2c = 50, disabled = 51, notClocked = 52, noTiming = 53 };

// Reg must provide: uint32_t read(uint32_t offset); void write(uint32_t offset, uint32_t value);
template <typename Reg>
struct Engine {
    Reg *reg = nullptr;
    uint32_t lastIrq = 0;

    Error waitIrq(uint32_t bit, Error timeout, Error failed)
    {
        for (unsigned n = 0; n < kSpins; ++n) {
            const uint32_t status = reg->read(kIrqStatus);
            if (status & kErrors) {
                lastIrq = status;
                reg->write(kIrqClear, status);
                return failed;
            }
            if (status & bit) {
                reg->write(kIrqClear, status & bit);
                return Error::none;
            }
        }
        lastIrq = reg->read(kIrqStatus);
        return timeout;
    }

    // Read-only: the protocol the engine's firmware implements and whether the boot loader left it usable.
    Error identify(uint32_t *summary)
    {
        const uint32_t proto = (reg->read(kFwRevision) >> 8) & 0xff, clock = reg->read(kClkCfg);
        if (summary)
            *summary = (proto << 24) | (clock & 0xffffff);
        if (proto != kProtoI2c)
            return Error::notI2c;
        if (reg->read(kIfDisable) & 1)
            return Error::disabled;
        if (!(clock & 1))
            return Error::notClocked;
        if (!reg->read(kSclCounters))
            return Error::noTiming;
        return Error::none;
    }

    // Packing for 8-bit words, 4 per FIFO word, most significant bit first (the values i2c-qcom-geni programs).
    void prepare()
    {
        reg->write(kDmaMode, 0);
        reg->write(kTxPack0, 0x7f8fe);
        reg->write(kTxPack1, 0xffefe);
        reg->write(kRxPack0, 0x7f8fe);
        reg->write(kRxPack1, 0xffefe);
        reg->write(kByteGran, 0);
    }

    // One write of up to 4 bytes to the slave; stop is held when a read follows.
    Error write(const uint8_t *bytes, unsigned length, bool holdStop)
    {
        reg->write(kIrqClear, 0xffffffffu);
        reg->write(kTxLen, length);
        reg->write(kCmd0, (kOpWrite << 27) | (static_cast<uint32_t>(kAddress) << 9) | (holdStop ? kStopStretch : 0));
        reg->write(kTxWatermark, 1);
        Error error = waitIrq(kTxWatermarkIrq, Error::watermarkTimeout, Error::watermarkFailed);
        if (error != Error::none)
            return error;
        uint32_t word = 0;
        for (unsigned i = 0; i < length; ++i)
            word |= static_cast<uint32_t>(bytes[i]) << (8 * i);
        reg->write(kTxFifo, word);
        reg->write(kTxWatermark, 0);
        return waitIrq(kCmdDone, Error::doneTimeout, Error::doneFailed);
    }

    Error readRegister(uint8_t address, uint8_t *value)
    {
        Error error = write(&address, 1, true);
        if (error != Error::none)
            return error;
        reg->write(kIrqClear, 0xffffffffu);
        reg->write(kRxLen, 1);
        reg->write(kCmd0, (kOpRead << 27) | (static_cast<uint32_t>(kAddress) << 9));
        error = waitIrq(kCmdDone, Error::readTimeout, Error::readFailed);
        if (error != Error::none)
            return error;
        if (!(reg->read(kRxFifoStatus) & 0x1ffffffu))
            return Error::readEmpty;
        *value = static_cast<uint8_t>(reg->read(kRxFifo));
        return Error::none;
    }

    Error readLevel(uint32_t *level)
    {
        uint8_t lsb = 0, msb = 0;
        Error error = readRegister(kBrtLsb, &lsb);
        if (error == Error::none)
            error = readRegister(kBrtMsb, &msb);
        if (error == Error::none)
            *level = levelOf(lsb, msb);
        return error;
    }

    Error writeLevel(uint32_t level)
    {
        const uint8_t lsb[2] = {kBrtLsb, lsbOf(level)}, msb[2] = {kBrtMsb, msbOf(level)};
        Error error = write(lsb, 2, false);
        if (error == Error::none)
            error = write(msb, 2, false);
        return error;
    }
};

// Both chips together. A chip that refuses leaves the other as it was: the second chip's failure undoes the first.
template <typename Reg>
inline Error setBoth(Engine<Reg> &a, Engine<Reg> &b, uint32_t level, uint32_t previous)
{
    level = clampLevel(level);
    Error error = a.writeLevel(level);
    if (error != Error::none)
        return error;
    error = b.writeLevel(level);
    if (error != Error::none)
        a.writeLevel(clampLevel(previous));
    return error;
}

// Stage 1 gate: an engine's registers may only be touched when its branch clocks run. GCC branch status: bit 31 CLK_OFF,
// bit 1 HW_CTL; a hardware-controlled branch reads off while idle, so it is not judged (as Linux clk-branch.c).
constexpr bool branchRunning(uint32_t status) { return (status & (1u << 1)) || !(status & (1u << 31)); }

}  // namespace sheng_backlight

#endif
