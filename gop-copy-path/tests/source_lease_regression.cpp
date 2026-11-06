// SPDX-License-Identifier: MIT
// Host-only ordering test. This is not an XNU VM or framebuffer simulation.
// A lease protects the complete (two-page) source mapping with PROT_NONE. Every
// source read must therefore have finished before begin(), including identity BGRA.
#include <cassert>
#include <cerrno>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>
#include <signal.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

#ifndef GOP_HEADER
#error GOP_HEADER must name the exact original or proposed header
#endif
#include GOP_HEADER

struct ProtectSourceLease {
    void *source;
    size_t bytes;
    unsigned begins = 0, ends = 0;
    bool open = false;
    bool begin() {
        assert(!open);
        assert(mprotect(source, bytes, PROT_NONE) == 0);
        open = true;
        begins++;
        return true;
    }
    void end() {
        assert(open);
        assert(mprotect(source, bytes, PROT_READ | PROT_WRITE) == 0);
        open = false;
        ends++;
    }
};

static void exercise()
{
    const long rawPage = sysconf(_SC_PAGESIZE);
    assert(rawPage > 0 && rawPage % 4 == 0);
    const size_t page = static_cast<size_t>(rawPage), allocation = page * 2;
    auto *source = static_cast<uint8_t *>(mmap(nullptr, allocation, PROT_READ | PROT_WRITE,
                                              MAP_PRIVATE | MAP_ANONYMOUS, -1, 0));
    assert(source != MAP_FAILED);
    // The copied row crosses a real host page boundary. Touching just one byte
    // or only the first page before begin() cannot satisfy this test.
    const uint64_t width = page / 4 + 16, rowBytes = width * 4;
    assert(rowBytes > page && rowBytes < allocation);
    for (uint64_t x = 0; x < width; x++)
        reinterpret_cast<uint32_t *>(source)[x] = 0xff000000u | static_cast<uint32_t>(x * 977);
    std::vector<uint32_t> destination(width, 0), converted(width), shadow(width, 0x12345678);
    std::vector<uint16_t> scratch(width * 2);
    auto sample = [](uint64_t, const uint16_t *) { assert(false && "BGRA identity must not invoke half-float sampling"); };
    ProtectSourceLease lease{source, allocation};
    auto result = nativeGopBlitRows(reinterpret_cast<uint8_t *>(destination.data()), rowBytes,
        width, 1, source, rowBytes, width, 1, 4, nullptr,
        scratch.data(), converted.data(), shadow.data(), false, lease, sample);
    assert(result.complete && result.written == 1 && result.skipped == 0);
    assert(lease.begins == 1 && lease.ends == 1 && !lease.open);
    assert(memcmp(destination.data(), source, rowBytes) == 0);
    assert(memcmp(shadow.data(), source, rowBytes) == 0);

    // Unchanged rows still skip the lease; the staging fix preserves dirty-row behavior.
    ProtectSourceLease unchanged{source, allocation};
    result = nativeGopBlitRows(reinterpret_cast<uint8_t *>(destination.data()), rowBytes,
        width, 1, source, rowBytes, width, 1, 4, nullptr,
        scratch.data(), converted.data(), shadow.data(), true, unchanged, sample);
    assert(result.complete && result.written == 0 && result.skipped == 1);
    assert(unchanged.begins == 0 && unchanged.ends == 0);
    assert(munmap(source, allocation) == 0);
}

int main(int argc, char **argv)
{
    assert(argc == 2 && (!strcmp(argv[1], "expect-source-fault") || !strcmp(argv[1], "expect-pass")));
    const bool expectFault = !strcmp(argv[1], "expect-source-fault");
    // XNU bsd/uxkern/ux_exception.c maps a protection failure in an existing mapping
    // to SIGBUS; Linux reports SIGSEGV. Neither path accepts an unrelated signal.
#if defined(__APPLE__)
    const int expectedSignal = SIGBUS;
#else
    const int expectedSignal = SIGSEGV;
#endif
    const pid_t child = fork();
    assert(child >= 0);
    if (child == 0) {
        // Keep the intentionally faulting original test isolated and do not create a core dump.
        const rlimit limit{0, 0};
        assert(setrlimit(RLIMIT_CORE, &limit) == 0);
        exercise();
        _exit(0);
    }
    int status = 0;
    while (waitpid(child, &status, 0) < 0) assert(errno == EINTR);
    if (expectFault) {
        if (!WIFSIGNALED(status) || WTERMSIG(status) != expectedSignal) {
            fprintf(stderr, "original did not receive expected protection signal %d: status=%d\n", expectedSignal, status);
            return 1;
        }
        printf("PASS: original identity path reads protected source inside lease (isolated child signal %d)\n", expectedSignal);
    } else {
        if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) {
            fprintf(stderr, "proposed staging failed: status=%d\n", status);
            return 1;
        }
        puts("PASS: proposed identity path stages complete cross-page row before lease; pixels/shadow/skip preserved");
    }
    return 0;
}
