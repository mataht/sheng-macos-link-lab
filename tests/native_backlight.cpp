// SPDX-License-Identifier: GPL-3.0-or-later
// Host test of platform/sheng/macos26/power/ktz8866_i2c.h against a simulated GENI I2C engine with a KTZ8866 behind it.
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <map>
#include <vector>
#include "../platform/sheng/macos26/power/ktz8866_i2c.h"

using namespace sheng_backlight;

#define CHECK(x) do { if (!(x)) { std::printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #x); std::exit(1); } } while (0)

struct Sim {
    std::map<uint32_t, uint32_t> regs;
    std::map<uint8_t, uint8_t> chip;
    std::vector<std::pair<uint8_t, uint8_t>> chipWrites;
    uint8_t pointer = 0;
    uint32_t irq = 0, op = 0, txLen = 0, rx = 0;
    bool rxReady = false, nack = false, deaf = false;

    uint32_t read(uint32_t offset)
    {
        if (offset == kIrqStatus) return irq;
        if (offset == kRxFifoStatus) return rxReady ? 1 : 0;
        if (offset == kRxFifo) { rxReady = false; return rx; }
        return regs[offset];
    }
    void write(uint32_t offset, uint32_t value)
    {
        regs[offset] = value;
        if (offset == kIrqClear) { irq &= ~value; return; }
        if (deaf) return;
        if (offset == kCmd0) {
            CHECK(((value >> 9) & 0x7f) == kAddress);
            op = value >> 27;
            if (op == kOpWrite) { irq |= kTxWatermarkIrq; return; }
            CHECK(op == kOpRead);
            if (nack) { irq |= 1u << 10; return; }
            rx = chip[pointer];
            rxReady = true;
            irq |= kCmdDone;
            return;
        }
        if (offset == kTxFifo) {
            CHECK(regs[kTxLen] == 1 || regs[kTxLen] == 2);
            if (nack) { irq |= 1u << 10; return; }
            const uint8_t address = value & 0xff;
            if (regs[kTxLen] == 2) {
                chip[address] = (value >> 8) & 0xff;
                chipWrites.emplace_back(address, static_cast<uint8_t>((value >> 8) & 0xff));
            } else {
                pointer = address;
            }
            irq |= kCmdDone;
        }
    }
};

static uint32_t level(Sim &s) { return levelOf(s.chip[kBrtLsb], s.chip[kBrtMsb]); }

int main()
{
    // The clamp: nothing outside the visible range can come out of it.
    CHECK(clampLevel(-5) == kMinVisibleLevel && clampLevel(0) == kMinVisibleLevel && clampLevel(63) == kMinVisibleLevel);
    CHECK(clampLevel(64) == 64 && clampLevel(2047) == 2047 && clampLevel(4095) == 2047);
    for (uint32_t v = 0; v <= kMaxLevel; ++v)
        CHECK(levelOf(lsbOf(v), msbOf(v)) == v);
    // GCC branch status: running, stopped, and hardware-gated (reads off while idle, not judged).
    CHECK(branchRunning(0) && !branchRunning(0x80000000u) && branchRunning(0x80000002u));

    {   // identify
        Sim s;
        Engine<Sim> e{&s};
        uint32_t summary = 0;
        s.regs[kFwRevision] = kProtoI2c << 8;
        s.regs[kClkCfg] = 0x123 | 1;
        s.regs[kSclCounters] = 7;
        CHECK(e.identify(&summary) == Error::none && summary == (kProtoI2c << 24 | 0x123 | 1));
        s.regs[kFwRevision] = 1 << 8;
        CHECK(e.identify(&summary) == Error::notI2c);
        s.regs[kFwRevision] = kProtoI2c << 8;
        s.regs[kIfDisable] = 1;
        CHECK(e.identify(&summary) == Error::disabled);
        s.regs[kIfDisable] = 0;
        s.regs[kClkCfg] = 0;
        CHECK(e.identify(&summary) == Error::notClocked);
        s.regs[kClkCfg] = 1;
        s.regs[kSclCounters] = 0;
        CHECK(e.identify(&summary) == Error::noTiming);
    }
    {   // read and write of the level: only the two level registers, in LSB then MSB order
        Sim s;
        Engine<Sim> e{&s};
        s.chip[kBrtLsb] = 5;
        s.chip[kBrtMsb] = 0x12;
        uint32_t value = 0;
        CHECK(e.readLevel(&value) == Error::none && value == levelOf(5, 0x12));
        CHECK(s.chipWrites.empty());
        CHECK(e.writeLevel(0x3ff) == Error::none && level(s) == 0x3ff);
        CHECK(s.chipWrites.size() == 2 && s.chipWrites[0].first == kBrtLsb && s.chipWrites[1].first == kBrtMsb);
    }
    {   // both chips, the clamp at the choke point, and a NACK on the second chip restores the first
        Sim a, b;
        Engine<Sim> ea{&a}, eb{&b};
        CHECK(setBoth(ea, eb, 1500, 100) == Error::none && level(a) == 1500 && level(b) == 1500);
        CHECK(setBoth(ea, eb, 0, 1500) == Error::none && level(a) == kMinVisibleLevel && level(b) == kMinVisibleLevel);
        CHECK(setBoth(ea, eb, 9999, 64) == Error::none && level(a) == kMaxLevel);
        a.chip[kBrtLsb] = lsbOf(700);
        a.chip[kBrtMsb] = msbOf(700);
        b.nack = true;
        CHECK(setBoth(ea, eb, 1000, 700) == Error::doneFailed);
        CHECK(level(a) == 700);
        CHECK(eb.lastIrq & (1u << 10));
        // never below the minimum even while undoing
        CHECK(setBoth(ea, eb, 1000, 0) == Error::doneFailed && level(a) == kMinVisibleLevel);
        for (auto w : a.chipWrites)
            CHECK(w.first == kBrtLsb || w.first == kBrtMsb);
    }
    {   // the first chip refusing leaves the second alone
        Sim a, b;
        Engine<Sim> ea{&a}, eb{&b};
        a.nack = true;
        CHECK(setBoth(ea, eb, 1000, 500) == Error::doneFailed && b.chipWrites.empty());
    }
    {   // an engine that never answers times out instead of hanging
        Sim s;
        Engine<Sim> e{&s};
        s.deaf = true;
        uint32_t value = 0;
        CHECK(e.readLevel(&value) == Error::watermarkTimeout);
    }
    {   // prepare programs the FIFO packing and no DMA
        Sim s;
        Engine<Sim> e{&s};
        e.prepare();
        CHECK(s.regs[kDmaMode] == 0 && s.regs[kTxPack0] == 0x7f8fe && s.regs[kRxPack1] == 0xffefe);
    }
    std::printf("PASS\n");
    return 0;
}
