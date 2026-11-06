// SPDX-License-Identifier: GPL-3.0-or-later
// Remove one named device-mapper device (DM_DEV_REMOVE) on an AArch64 Linux recovery that lacks dmctl/dmsetup.
// Freestanding: no libc, only openat/ioctl/write/exit. The kernel refuses with EBUSY while the device is open,
// so this cannot tear down a mounted or in-use mapping and never touches the underlying partition's data.
// Build: clang --target=aarch64-linux-gnu -nostdlib -static -ffreestanding -fno-stack-protector -O2 -fuse-ld=lld
typedef unsigned long u64;
typedef unsigned int u32;

struct dm_ioctl {                // <linux/dm-ioctl.h>, version 4
    u32 version[3];
    u32 data_size;
    u32 data_start;
    u32 target_count;
    int open_count;
    u32 flags;
    u32 event_nr;
    u32 padding;
    unsigned long long dev;
    char name[128];
    char uuid[129];
    char data[7];
};
_Static_assert(sizeof(struct dm_ioctl) == 312, "dm_ioctl ABI");
#define DM_DEV_REMOVE 0xC138FD04u  // _IOWR(0xfd, 4, struct dm_ioctl)

static long sys(long n, long a, long b, long c, long d) {
    register long x8 __asm__("x8") = n, x0 __asm__("x0") = a, x1 __asm__("x1") = b,
                  x2 __asm__("x2") = c, x3 __asm__("x3") = d;
    __asm__ volatile("svc #0" : "+r"(x0) : "r"(x8), "r"(x1), "r"(x2), "r"(x3) : "memory");
    return x0;
}

static void say(const char *s) {
    long n = 0;
    while (s[n]) n++;
    sys(64, 1, (long)s, n, 0);  // write(1, ...)
}

static void number(long v) {
    char buf[24];
    int i = 23;
    buf[i] = 0;
    unsigned long u = v < 0 ? -v : v;
    do { buf[--i] = '0' + u % 10; u /= 10; } while (u);
    if (v < 0) buf[--i] = '-';
    say(buf + i);
}

__attribute__((noreturn)) void _start_c(long argc, char **argv) {
    if (argc != 2) { say("usage: dm_remove <name>\n"); sys(93, 2, 0, 0, 0); }
    struct dm_ioctl io;
    for (u64 i = 0; i < sizeof(io); i++) ((char *)&io)[i] = 0;
    io.version[0] = 4;
    io.data_size = sizeof(io);
    io.data_start = sizeof(io);
    long n = 0;
    while (argv[1][n] && n < 127) { io.name[n] = argv[1][n]; n++; }
    if (argv[1][n]) { say("name too long\n"); sys(93, 2, 0, 0, 0); }
    long fd = sys(56, -100, (long)"/dev/device-mapper", 2 /* O_RDWR */, 0);  // openat(AT_FDCWD, ...)
    if (fd < 0) { say("open /dev/device-mapper failed: "); number(fd); say("\n"); sys(93, 1, 0, 0, 0); }
    long r = sys(29, fd, DM_DEV_REMOVE, (long)&io, 0);  // ioctl
    if (r < 0) { say("DM_DEV_REMOVE failed: "); number(r); say("\n"); sys(93, 1, 0, 0, 0); }
    say("removed dm device "); say(io.name); say("\n");
    sys(93, 0, 0, 0, 0);  // exit(0)
    __builtin_unreachable();
}

__asm__(".global _start\n_start:\n ldr x0, [sp]\n add x1, sp, #8\n b _start_c\n");
