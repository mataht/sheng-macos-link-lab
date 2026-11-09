// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
typedef int wait_result_t;
typedef struct thread_stub *thread_t;
typedef int kern_return_t;
enum { KERN_SUCCESS = 0 };
extern "C" kern_return_t kernel_thread_start(void (*)(void *, wait_result_t), void *, thread_t *);
extern "C" void thread_deallocate(thread_t);
