// SPDX-License-Identifier: GPL-3.0-or-later
// Geometry comes from the reviewed layout macros (-DNATIVE_MACOSLOG_*), exactly as in the kernel build.
#define NATIVE_MACOSLOG 1
#define main original_commands_main
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wreturn-type"
#include "../UniXNU/drivers/NativeUfs/tests/RootCommands.cpp"
#pragma GCC diagnostic pop
#undef main

int main() {
    constexpr uint8_t L = NATIVE_MACOSLOG_LUN;
    constexpr uint64_t F = NATIVE_MACOSLOG_FIRST, N = NATIVE_MACOSLOG_BLOCKS;
    reset();
    gMacosLogOpen = false;
    assert(hostMacosLogReady() == kIOReturnSuccess);
    assert(device[kUcdOffset+2] == L && device[kUcdOffset+16] == 0);
    std::array<uint8_t, 21*4096> data{};
    assert(hostWrite4k(L, F+88, 1, data.data()) == kIOReturnBadArgument);
    assert(!hostMacosLogOpen(F, 511));
    assert(!hostMacosLogOpen(F+N-511, 511));
    assert(!hostMacosLogOpen(F+88, 512));
    assert(hostMacosLogOpen(F+88, 511));
    assert(!hostMacosLogOpen(F+88, 511));
    assert(hostWrite4k(L, F+89, 21, data.data()) == kIOReturnSuccess);
    const uint8_t *cmd = device.data()+kUcdOffset;
    assert(cmd[2] == L && cmd[16] == 0x2a);
    assert(loadBe32(cmd+18) == F+89);
    assert(hostWrite4k(L, F+598, 1, data.data()) == kIOReturnSuccess);
    auto previous = writes;
    for (uint8_t lun : {0, 1, 2, 3, 4, 5, 6})
        if (lun != L) assert(hostWrite4k(lun, F+88, 1, data.data()) == kIOReturnBadArgument);
    for (uint64_t at : std::array<uint64_t, 5>{1, F+87, F+599, F+N-1, UINT64_MAX})
        assert(hostWrite4k(L, at, 1, data.data()) == kIOReturnBadArgument);
    assert(hostWrite4k(L, F+598, 2, data.data()) == kIOReturnBadArgument);
    assert(writes == previous);
    assert(hostMacosLogSync() == kIOReturnSuccess);
    assert(cmd[2] == L && cmd[16] == 0x35);
    assert(hostStoreOpen(100, 2));
    assert(hostWrite4k(0, 100, 1, data.data()) == kIOReturnSuccess);
    assert(cmd[2] == 0);
    reset(Fault::CheckCondition);
    assert(hostMacosLogReady() == kIOReturnNotReady && now == 4000 && !gHost.poisoned);
    reset(Fault::Timeout);
    assert(hostWrite4k(L, F+88, 1, data.data()) != kIOReturnSuccess);
    assertQuarantined();
    printf("PASS: macoslog data window on LUN %u at %llu, guard exclusion, sync LUN and existing LUN0 policy\n",
           unsigned(L), (unsigned long long)F);
}
