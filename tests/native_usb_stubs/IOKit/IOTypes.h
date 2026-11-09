// Minimal stand-ins for the host syntax check of the USB companion; not an IOKit implementation.
#pragma once
#include <stdint.h>
#include <stddef.h>
#include <stdarg.h>
#include <string.h>
#include <stdio.h>
typedef int IOReturn;
typedef uint32_t IOOptionBits;
typedef int kern_return_t;
#define KERN_SUCCESS 0
#define kIOReturnSuccess 0
#define kIOMapAnywhere 1u
#define kIOMapInhibitCache 2u
#define kIODirectionInOut 3u
typedef uint64_t IOPhysicalAddress64;
typedef uint64_t mach_vm_address_t;
typedef uintptr_t task_t;
extern task_t kernel_task;
typedef uint32_t IODirection;
typedef int wait_result_t;
typedef struct thread *thread_t;
typedef void (*thread_continue_t)(void *, wait_result_t);
extern "C" {
void IOLog(const char *format, ...) __attribute__((format(printf, 1, 2)));
void IOSleep(unsigned ms);
void IODelay(unsigned us);
void *IOMalloc(size_t);
void IOFree(void *, size_t);
kern_return_t kernel_thread_start(thread_continue_t, void *, thread_t *);
void thread_deallocate(thread_t);
int PE_parse_boot_argn(const char *, void *, int);
extern uint32_t PE_vmm_present;
}
