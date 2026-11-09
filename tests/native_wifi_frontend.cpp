// SPDX-License-Identifier: GPL-3.0-or-later
// Host run of the real platform/sheng/macos26/net/native_wifi.cpp over stubbed kernel interfaces: the boot-argument and VM gates, the report rows,
// and the stage 2 path up to a missing command database (ASan and UBSan check that the row table and buffers stay in bounds).
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <map>
#include <string>
#include <vector>

#include <IOKit/IOLib.h>
#include <IOKit/IOMemoryDescriptor.h>
#include <IOKit/IOService.h>
#include <kern/thread.h>
#include <pexpert/pexpert.h>

task *kernel_task = nullptr;
int PE_vmm_present = 0;
static unsigned bootArgValue = 0;
static bool bootArgPresent = false;
static unsigned probeCalls = 0;
static std::string logText;

extern "C" {
volatile uint32_t native_wifi_stats[10];
void sheng_wifi_probe(void)
{
    ++probeCalls;
    native_wifi_stats[0] = 2;
    native_wifi_stats[3] = 0x80000000u;
}
int IOLog(const char *fmt, ...)
{
    char line[256];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(line, sizeof line, fmt, ap);
    va_end(ap);
    logText += line;
    return 0;
}
void IOSleep(unsigned) {}
void IODelay(unsigned) {}
void *IOMalloc(size_t n) { return malloc(n); }
void IOFree(void *p, size_t) { free(p); }
int PE_parse_boot_argn(const char *name, void *value, int bytes)
{
    if (strcmp(name, "sheng-wifi") || !bootArgPresent || bytes < 4)
        return 0;
    memcpy(value, &bootArgValue, sizeof bootArgValue);
    return 1;
}
kern_return_t kernel_thread_start(void (*entry)(void *, wait_result_t), void *arg, thread_t *out)
{
    // The real reporter is a kernel thread; here it runs to completion before start() returns, so its rows can be checked.
    entry(arg, 0);
    *out = nullptr;
    return KERN_SUCCESS;
}
void thread_deallocate(thread_t) {}
}

// Device memory: zeroed arrays by physical address, with the SPMI arbiter's version and APID count preset so its reads complete.
static std::map<uint64_t, std::vector<uint32_t>> regions;

static uint32_t *regionFor(uint64_t physical, uint64_t bytes)
{
    auto it = regions.find(physical);
    if (it == regions.end()) {
        std::vector<uint32_t> memory(static_cast<size_t>(bytes / 4 + 1));
        it = regions.emplace(physical, std::move(memory)).first;
        if (physical == 0x0c400000) {
            it->second[0] = 0x70000000;
            it->second[1] = 0x80;
        }
        if (physical == 0x0c440000)
            for (auto &word : it->second)
                word = 1;   // every observer channel reports done, and data 1
    }
    return it->second.data();
}

IOMemoryDescriptor *IOMemoryDescriptor::withPhysicalAddress(uint64_t physical, uint64_t bytes, unsigned)
{
    auto *descriptor = new IOMemoryDescriptor;
    descriptor->physical = physical;
    descriptor->bytes = bytes;
    return descriptor;
}

IOMemoryMap *IOMemoryDescriptor::createMappingInTask(task *, uint64_t, IOOptionBits)
{
    auto *map = new IOMemoryMap;
    map->va = reinterpret_cast<uint64_t>(regionFor(physical, bytes));
    return map;
}

#include "native_wifi.cpp"

#define CHECK(x) do { if (!(x)) { std::printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #x); std::exit(1); } } while (0)

static void reset(bool present, unsigned value, int vm)
{
    regions.clear();
    bootArgPresent = present;
    bootArgValue = value;
    PE_vmm_present = vm;
    probeCalls = 0;
    logText.clear();
    memset(const_cast<uint32_t *>(native_wifi_stats), 0, sizeof native_wifi_stats);
}

static bool start()
{
    auto *wifi = new NativeWifi;
    IOService provider;
    const bool started = wifi->start(&provider);
    return started;
}

int main()
{
    // Default off: no argument, zero, or an unknown stage.
    reset(false, 0, 0);
    CHECK(!start() && probeCalls == 0 && logText.empty());
    reset(true, 0, 0);
    CHECK(!start() && probeCalls == 0);
    reset(true, 4, 0);
    CHECK(!start() && probeCalls == 0);

    // Refused in a VM.
    reset(true, 1, 1);
    CHECK(!start() && probeCalls == 0);
    CHECK(logText.find("not driven in a VM") != std::string::npos);

    // Stage 1: the rows are printed and no bring-up runs.
    reset(true, 1, 0);
    CHECK(start() && probeCalls == 1);
    CHECK(logText.find("NativeWifi t0 R00: st2") != std::string::npos);
    CHECK(logText.find("NativeWifi t0 R11: uart14") != std::string::npos);
    CHECK(logText.find("R12") == std::string::npos);
    CHECK(logText.find("bringup") == std::string::npos);
    CHECK(logText.find("t120 R11") != std::string::npos);

    // Stage 2 with no sleep clock peripheral in the arbiter stops before any power vote and reports it.
    reset(true, 2, 0);
    CHECK(start() && probeCalls == 1);
    CHECK(logText.find("NativeWifi bringup 0: st-1 clk 02") != std::string::npos);

    std::printf("PASS\n");
    return 0;
}
