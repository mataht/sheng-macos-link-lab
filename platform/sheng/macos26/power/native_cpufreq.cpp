// SPDX-License-Identifier: GPL-3.0-or-later
// NativeCpuFreq: macOS 26 front end for the SM8550 EPSS CPU frequency probe of UniXNU-sheng (package/CpuFreqProbe.cpp).
//
// Default off: sheng-cpufreq=<1..3> as defined by the probe (1 read-only report of each domain's enable bit, current
// and requested level and highest LUT entry; 2 and 3 request higher levels); hardware only (PE_vmm_present == 0).
// The probe keeps its state in native_cpufreq_row; this file polls it (the probe votes a domain only the first time it
// reads enabled, so the poll continues while the secondary clusters come up) and prints the row on the kernel console
// every 5 s and on every change for the first 120 s, so a photo of the boot text records it.
#include <IOKit/IOService.h>
#include <IOKit/IOLib.h>
#include <kern/thread.h>
#include <pexpert/pexpert.h>
#include <string.h>

extern "C" {
void sheng_cpufreq_probe(void);
void sheng_cpufreq_poll(void);
extern char native_cpufreq_row[120];
}

class NativeCpuFreq : public IOService {
  OSDeclareDefaultStructors(NativeCpuFreq)

public:
  bool start(IOService *provider) override;
};

OSDefineMetaClassAndStructors(NativeCpuFreq, IOService)

namespace {
void cpufreqReportThread(void *, wait_result_t)
{
  char row[sizeof native_cpufreq_row];
  char last[sizeof native_cpufreq_row] = {};
  for (unsigned second = 0; second <= 120; ++second) {
    sheng_cpufreq_poll();
    memcpy(row, native_cpufreq_row, sizeof row);
    row[sizeof row - 1] = 0;
    if (second % 5 == 0 || strncmp(row, last, sizeof row) != 0) {
      IOLog("NativeCpuFreq t%us: %s\n", second, row);
      memcpy(last, row, sizeof row);
    }
    IOSleep(1000);
  }
}
}

bool NativeCpuFreq::start(IOService *provider)
{
  uint32_t mode = 0;
  if (!PE_parse_boot_argn("sheng-cpufreq", &mode, sizeof(mode)) || mode < 1 || mode > 3)
    return false;
  if (PE_vmm_present != 0) {
    IOLog("NativeCpuFreq: the CPU frequency domains are not driven in a VM\n");
    return false;
  }
  if (!IOService::start(provider))
    return false;
  sheng_cpufreq_probe();
  registerService();
  IOLog("NativeCpuFreq: mode %u started\n", mode);
  thread_t reporter = nullptr;
  if (kernel_thread_start(cpufreqReportThread, nullptr, &reporter) == KERN_SUCCESS)
    thread_deallocate(reporter);
  else
    IOLog("NativeCpuFreq: cannot start the reporter\n");
  return true;
}
