// SPDX-License-Identifier: GPL-3.0-or-later
// Host stubs of the kernel interfaces native_wifi.cpp uses; tests/native_wifi_frontend.cpp defines them.
#pragma once
#include <stdint.h>
#include <stdio.h>
#include <stdarg.h>
#include <stddef.h>
#include <string.h>
extern "C" int IOLog(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
extern "C" void IOSleep(unsigned ms);
extern "C" void IODelay(unsigned us);
extern "C" void *IOMalloc(size_t n);
extern "C" void IOFree(void *p, size_t n);
