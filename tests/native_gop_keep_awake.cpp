// SPDX-License-Identifier: GPL-3.0-or-later
// Host checks for the display keep-awake policy in platform/sheng/macos26/display/0001-native-gop-display.patch.
// test_native_gop_keep_awake.py extracts the helper block from the patch into KEEP_AWAKE_HELPERS.
#include <cassert>
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

class IOService;
static uint64_t now;
static uint64_t *suspend_in_clock;
static void (*before_root_return)();
static const uint32_t kSecondScale = 1000000000;
static std::vector<std::string> logs, reasons;

static void IOLog(const char *format, ...)
{
    char line[256];
    va_list arguments;
    va_start(arguments, format);
    vsnprintf(line, sizeof(line), format, arguments);
    va_end(arguments);
    logs.emplace_back(line);
}

struct IOPMrootDomain {
    void requestUserActive(IOService *, const char *reason) { reasons.emplace_back(reason); }
};
static IOPMrootDomain domain, *domain_pointer = &domain;
class IOService {
public:
    static IOPMrootDomain *getPMRootDomain()
    {
        auto hook = before_root_return;
        before_root_return = nullptr;
        if (hook)
            hook();
        return domain_pointer;
    }
};
static uint64_t mach_absolute_time() { return now; }
static void clock_interval_to_deadline(uint32_t interval, uint32_t scale, uint64_t *deadline)
{
    *deadline = now + uint64_t(interval) * scale;
    if (suspend_in_clock) {
        __atomic_store_n(suspend_in_clock, UINT64_MAX, __ATOMIC_RELEASE);
        suspend_in_clock = nullptr;
    }
}

#include KEEP_AWAKE_HELPERS

static const char *const kVM = "sheng-qemu-display-observation";
static const char *const kHardware = "sheng-native-display-keep-awake";

static void release(IOService *owner)
{
    // NativeGOPDisplay::stop: suspend, base stop, then release only its own ownership.
    if (__atomic_load_n(&gVMKeepAwakeOwner, __ATOMIC_ACQUIRE) == owner)
        __atomic_store_n(&gVMKeepAwakeDeadline, UINT64_MAX, __ATOMIC_RELEASE);
    IOService *expected = owner;
    __atomic_compare_exchange_n(&gVMKeepAwakeOwner, &expected, nullptr, false, __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE);
}

static void suspendOwner() { __atomic_store_n(&gVMKeepAwakeDeadline, UINT64_MAX, __ATOMIC_RELEASE); }

int main()
{
    // Boot-argument gate: default off; hardware only on a non-VMM kernel and never together with the VM switch.
    assert(nativeGopHardwareKeepAwakeAllowed(0, 0, 0) && nativeGopHardwareKeepAwakeAllowed(0, 1, 1));
    assert(nativeGopHardwareKeepAwakeAllowed(0, 7, 3));
    assert(nativeGopHardwareKeepAwakeAllowed(1, 0, 0));
    assert(!nativeGopHardwareKeepAwakeAllowed(1, 0, 1) && !nativeGopHardwareKeepAwakeAllowed(1, 1, 0));
    assert(!nativeGopHardwareKeepAwakeAllowed(1, 1, 1) && !nativeGopHardwareKeepAwakeAllowed(2, 0, 0));
    assert(!nativeGopHardwareKeepAwakeAllowed(UINT32_MAX, 0, 0));

    IOService one, two;
    nativeGopVMKeepAwakeTick(&one);
    assert(reasons.empty());

    // The VM switch keeps the Stage18B reason and exact log text.
    domain_pointer = nullptr;
    assert(!nativeGopStartVMKeepAwake(&one, false));
    assert(gVMKeepAwakeOwner == nullptr && logs.back() == "NativeGOPDisplay: VM keep-awake root domain not ready\n");
    domain_pointer = &domain;
    assert(nativeGopStartVMKeepAwake(&one, false));
    assert(reasons.size() == 1 && reasons.back() == kVM);
    assert(logs.back() == "NativeGOPDisplay: VM requestUserActive submitted (start)\n");
    now = 30000000000;
    nativeGopVMKeepAwakeTick(&one);
    assert(reasons.size() == 2 && reasons.back() == kVM);
    assert(logs.back() == "NativeGOPDisplay: VM requestUserActive submitted (periodic)\n");
    release(&one);

    // Hardware switch: same owner/deadline mechanism, its own reason and log label.
    now = 100000000000;
    assert(nativeGopStartVMKeepAwake(&two, true));
    assert(reasons.size() == 3 && reasons.back() == kHardware);
    assert(logs.back() == "NativeGOPDisplay: hardware requestUserActive submitted (start)\n");
    assert(!nativeGopStartVMKeepAwake(&one, false) && gVMKeepAwakeOwner == &two);
    assert(logs.back() == "NativeGOPDisplay: VM keep-awake already has an owner\n" && reasons.size() == 3);
    now = 129999999999;
    nativeGopVMKeepAwakeTick(&two);
    assert(reasons.size() == 3);
    now = 130000000000;
    nativeGopVMKeepAwakeTick(&one);
    assert(reasons.size() == 3);
    nativeGopVMKeepAwakeTick(&two);
    assert(reasons.size() == 4 && reasons.back() == kHardware && gVMKeepAwakeDeadline == 160000000000);
    assert(logs.back() == "NativeGOPDisplay: hardware requestUserActive submitted (periodic)\n");
    nativeGopVMKeepAwakeTick(&two);
    assert(reasons.size() == 4);
    // A long gap sends one request, then re-arms 30 s from now (no burst of missed periods).
    now = 990000000000;
    nativeGopVMKeepAwakeTick(&two);
    assert(reasons.size() == 5 && gVMKeepAwakeDeadline == 1020000000000);
    domain_pointer = nullptr;
    now = 1020000000000;
    nativeGopVMKeepAwakeTick(&two);
    assert(reasons.size() == 5 && logs.back() == "NativeGOPDisplay: hardware keep-awake root domain unavailable; no request\n");
    domain_pointer = &domain;

    // Stop wins before the deadline CAS: no request is submitted.
    now = gVMKeepAwakeDeadline;
    suspend_in_clock = &gVMKeepAwakeDeadline;
    nativeGopVMKeepAwakeTick(&two);
    assert(reasons.size() == 5 && gVMKeepAwakeDeadline == UINT64_MAX);
    release(&two);
    assert(gVMKeepAwakeOwner == nullptr);

    // A later VM owner does not inherit the hardware reason.
    assert(nativeGopStartVMKeepAwake(&one, false));
    assert(reasons.size() == 6 && reasons.back() == kVM);
    // Tick CAS wins first: one in-flight request may still finish after stop starts.
    now = gVMKeepAwakeDeadline;
    before_root_return = suspendOwner;
    nativeGopVMKeepAwakeTick(&one);
    assert(reasons.size() == 7 && reasons.back() == kVM && gVMKeepAwakeDeadline == UINT64_MAX);
    nativeGopVMKeepAwakeTick(&one);
    assert(reasons.size() == 7);
    release(&one);
    nativeGopVMKeepAwakeTick(&one);
    assert(reasons.size() == 7);
    puts("PASS: keep-awake gate, VM text, hardware reason, ownership and stop ordering");
    return 0;
}
