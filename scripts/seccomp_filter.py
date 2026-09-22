#!/usr/bin/env python3
"""Build the seccomp filter bubblewrap loads into every confined phase.

The systemd unit used to supply a few syscall-level restrictions that a mount
namespace cannot express: no ``AF_UNIX`` sockets (``RestrictAddressFamilies``),
no personality changes (``LockPersonality``) and no creation of setuid or
setgid files (``RestrictSUIDSGID``). This filter restores them, and adds the
one that matters most while the standalone comparator still builds candidate
code in the same sandbox as its own process: no ``ptrace`` and no
``process_vm_readv``/``process_vm_writev``, so a candidate build cannot reach
into the comparator holding the challenge export, whatever the kernel's Yama
setting and whether or not Landlock exists on the runner.

Everything is expressed as classic BPF for x86-64, written by hand rather than
through libseccomp so the trusted phase needs no extra dependency. Denied calls
fail with ``EPERM``; a foreign architecture or the x32 ABI is refused outright.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

# <linux/bpf_common.h>
BPF_LD, BPF_JMP, BPF_RET = 0x00, 0x05, 0x06
BPF_W, BPF_ABS = 0x00, 0x20
BPF_JEQ, BPF_JSET, BPF_K = 0x10, 0x40, 0x00
# <linux/seccomp.h>
SECCOMP_RET_ALLOW = 0x7FFF0000
SECCOMP_RET_ERRNO = 0x00050000
SECCOMP_RET_KILL_PROCESS = 0x80000000
EPERM = 1
# <linux/audit.h>
AUDIT_ARCH_X86_64 = 0xC000003E
X32_SYSCALL_BIT = 0x40000000
# struct seccomp_data offsets (little-endian x86-64): nr, arch, ip, args[6]
OFF_NR, OFF_ARCH, OFF_ARGS = 0, 4, 16
AF_UNIX = 1
S_ISUID_OR_ISGID = 0o6000

# x86-64 syscall numbers
NR = {
    "socket": 41, "chmod": 90, "fchmod": 91, "ptrace": 101, "personality": 135,
    "fchmodat": 268, "process_vm_readv": 310, "process_vm_writev": 311, "fchmodat2": 452,
}
DENIED_OUTRIGHT = ("ptrace", "process_vm_readv", "process_vm_writev", "personality")
# (syscall, argument index, denied when) for the argument-inspecting rules.
SUID_RULES = (("chmod", 1), ("fchmod", 1), ("fchmodat", 2), ("fchmodat2", 2))


def _stmt(code: int, k: int) -> bytes:
    return struct.pack("<HBBI", code, 0, 0, k)


def _jump(code: int, k: int, jt: int, jf: int) -> bytes:
    return struct.pack("<HBBI", code, jt, jf, k)


def build(*, deny_unix_sockets: bool) -> bytes:
    """The filter program as raw ``struct sock_filter`` bytes.

    Layout: architecture check, syscall number load, one block per rule, then
    ``allow`` and ``deny``. Every rule that matches jumps forward to ``deny``;
    the fall-through path always leaves the syscall number in the accumulator.
    """
    # Rule blocks are built with symbolic jumps first, then resolved once the
    # position of `deny` is known.
    body: list[bytes | tuple] = []
    for name in DENIED_OUTRIGHT:
        body.append(("jeq_deny", NR[name]))
    argument_rules: list[tuple[str, int, str]] = [(name, index, "suid") for name, index in SUID_RULES]
    if deny_unix_sockets:
        argument_rules.append(("socket", 0, "unix"))
    for name, index, kind in argument_rules:
        body.append(("jne_skip", NR[name], 3))  # not this syscall: skip the block
        body.append(_stmt(BPF_LD | BPF_W | BPF_ABS, OFF_ARGS + 8 * index))  # A = low word of arg
        if kind == "suid":
            body.append(("jset_deny", S_ISUID_OR_ISGID))
        else:
            body.append(("jeq_deny", AF_UNIX))
        body.append(_stmt(BPF_LD | BPF_W | BPF_ABS, OFF_NR))  # A = nr again

    resolved: list[bytes] = []
    deny_index = len(body) + 1  # `allow` sits at len(body), `deny` right after
    for position, instruction in enumerate(body):
        if isinstance(instruction, bytes):
            resolved.append(instruction)
            continue
        distance = deny_index - (position + 1)  # jumps are relative to the next instruction
        if distance > 255:
            raise ValueError("filter too long for 8-bit jump offsets")
        kind = instruction[0]
        if kind == "jeq_deny":
            resolved.append(_jump(BPF_JMP | BPF_JEQ | BPF_K, instruction[1], distance, 0))
        elif kind == "jset_deny":
            resolved.append(_jump(BPF_JMP | BPF_JSET | BPF_K, instruction[1], distance, 0))
        elif kind == "jne_skip":
            resolved.append(_jump(BPF_JMP | BPF_JEQ | BPF_K, instruction[1], 0, instruction[2]))
        else:
            raise AssertionError(kind)

    program = [
        _stmt(BPF_LD | BPF_W | BPF_ABS, OFF_ARCH),
        _jump(BPF_JMP | BPF_JEQ | BPF_K, AUDIT_ARCH_X86_64, 1, 0),
        _stmt(BPF_RET | BPF_K, SECCOMP_RET_KILL_PROCESS),
        _stmt(BPF_LD | BPF_W | BPF_ABS, OFF_NR),
        # x32 ABI syscalls carry bit 30; refuse them rather than reason about them.
        _jump(BPF_JMP | BPF_JSET | BPF_K, X32_SYSCALL_BIT, 0, 1),
        _stmt(BPF_RET | BPF_K, SECCOMP_RET_KILL_PROCESS),
        *resolved,
        _stmt(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),
        _stmt(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | EPERM),
    ]
    return b"".join(program)


def main(argv: list[str]) -> int:
    if len(argv) not in (1, 2) or (len(argv) == 2 and argv[1] != "--allow-unix-sockets"):
        sys.stderr.write("usage: seccomp_filter.py OUTPUT [--allow-unix-sockets]\n")
        return 2
    Path(argv[0]).write_bytes(build(deny_unix_sockets=len(argv) == 1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
