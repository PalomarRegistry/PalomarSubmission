#!/usr/bin/env python3
"""Run one confined phase inside its own cgroup v2 and report how it ended.

This is the babysitter half of Palomar's supervisor. It replaces the transient
``systemd-run`` unit: something has to own a cgroup for the workload, apply the
memory, task and file limits, enforce the wall-clock deadline, tear the whole
process tree down when the phase is over, and leave behind trustworthy evidence
of *why* it ended. systemd did all of that; on a runner without systemd nothing
does, so this does.

It is deliberately small and stdlib-only, and it never writes to stdout or
stderr: those belong to the payload and are captured by the verifier. Its only
trusted channel back is the status file, written atomically, twice: once as soon
as the cgroup exists (so a verifier that has to abandon the phase can still find
and kill it) and once when the phase is over.

Placement model. The babysitter starts *already inside* a cgroup that has been
delegated to this user, either a ``systemd-run --scope -p Delegate=yes`` scope
on a systemd host, or a subtree a root helper created, entered and chowned
before dropping privileges and exec'ing this script. That cgroup is
``--parent self``. The babysitter moves itself into ``<parent>/supervisor``
(cgroup v2 forbids controllers on a cgroup that still has processes of its own),
enables the controllers on the parent, and creates ``<parent>/<name>`` for the
workload. Nothing here ever needs to write a cgroup it does not own.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import resource
import secrets
import select
import signal
import sys
import threading
import time
from pathlib import Path

CGROUP_ROOT = Path("/sys/fs/cgroup")
# Written in this order. memory.oom.group first so that an OOM kill takes the
# whole workload (observer included) rather than one process of it; swap off so
# memory.max is the actual ceiling; high before max so a lower high never trips
# while max is still unset.
LIMIT_ORDER = ("memory.oom.group", "memory.swap.max", "memory.high", "memory.max", "pids.max", "cpu.max")
BEST_EFFORT_LIMITS = {"cpu.max"}
POPULATED_WAIT_SECONDS = 10.0
POLL_SECONDS = 0.2
PR_SET_NO_NEW_PRIVS = 38
PR_SET_PDEATHSIG = 1


class SupervisorError(RuntimeError):
    pass


def own_cgroup() -> Path:
    for line in Path("/proc/self/cgroup").read_text(encoding="utf-8").splitlines():
        if line.startswith("0::"):
            relative = line[3:].strip()
            if ".." in Path(relative).parts:
                raise SupervisorError("refusing a cgroup path containing '..'")
            return CGROUP_ROOT / relative.lstrip("/")
    raise SupervisorError("no cgroup v2 membership in /proc/self/cgroup")


def write(path: Path, value: str) -> None:
    with open(path, "w", encoding="ascii") as handle:
        handle.write(value)


def read_procs(cgroup: Path) -> list[int]:
    try:
        return [int(p) for p in (cgroup / "cgroup.procs").read_text().split()]
    except (OSError, ValueError):
        return []


def populated(cgroup: Path) -> bool | None:
    try:
        for line in (cgroup / "cgroup.events").read_text().splitlines():
            key, _, raw = line.partition(" ")
            if key == "populated":
                return raw.strip() == "1"
    except OSError:
        return None
    return None


def kill_tree(cgroup: Path) -> None:
    """Terminate everything in the cgroup, whatever it did to signal handling."""
    try:
        write(cgroup / "cgroup.kill", "1")
    except OSError:
        # Older kernels: freeze so nothing can fork past us, then kill each pid.
        try:
            write(cgroup / "cgroup.freeze", "1")
        except OSError:
            pass
        for _ in range(50):
            pids = read_procs(cgroup)
            if not pids:
                break
            for pid in pids:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            time.sleep(0.05)
        try:
            write(cgroup / "cgroup.freeze", "0")
        except OSError:
            pass
    deadline = time.monotonic() + POPULATED_WAIT_SECONDS
    while populated(cgroup) and time.monotonic() < deadline:
        time.sleep(0.05)


def write_status(path: Path | None, payload: dict) -> None:
    if path is None:
        return
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def parse_pairs(values: list[str], what: str) -> list[tuple[str, str]]:
    pairs = []
    for value in values:
        key, sep, val = value.partition("=")
        if not sep or not key:
            raise SupervisorError(f"malformed {what}: {value!r}")
        pairs.append((key, val))
    return pairs


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--parent", required=True, help="cgroup directory, or `self`")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--name")
    group.add_argument("--collect", action="store_true")
    parser.add_argument("--limit", action="append", default=[])
    parser.add_argument("--rlimit", action="append", default=[])
    parser.add_argument("--deadline", type=float, required=True)
    parser.add_argument("--grace", type=float, default=30.0)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--status")
    parser.add_argument("--liveness", help="FIFO whose writer is the verifier; EOF means it died")
    parser.add_argument("--stdout", help="file that receives the workload's stdout")
    parser.add_argument("--setenv", action="append", default=[])
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        raise SupervisorError("no command given")
    if args.name is not None and not (args.name.startswith("palomar-") and len(args.name) == 32
                                      and all(c in "0123456789abcdef" for c in args.name[8:])):
        raise SupervisorError("invalid phase cgroup name")

    limits = parse_pairs(args.limit, "limit")
    unknown = [k for k, _ in limits if k not in LIMIT_ORDER]
    if unknown:
        raise SupervisorError(f"unsupported cgroup limit(s): {unknown}")
    rlimits: list[tuple[int, int]] = []
    rlimits_effective: dict[str, int] = {}
    for key, val in parse_pairs(args.rlimit, "rlimit"):
        attr = f"RLIMIT_{key}"
        if key not in {"NOFILE", "FSIZE"} or not hasattr(resource, attr):
            # RLIMIT_AS in particular is refused: address-space reservations
            # (con-ron keeps ~1 GiB per worker) would surface as candidate
            # failures instead of resource outcomes. memory.max is the ceiling.
            raise SupervisorError(f"unsupported rlimit: {key}")
        rlimits.append((getattr(resource, attr), int(val)))
        _, hard = resource.getrlimit(getattr(resource, attr))
        rlimits_effective[key] = int(val) if hard == resource.RLIM_INFINITY else min(int(val), hard)
    env = dict(parse_pairs(args.setenv, "setenv"))
    status_path = Path(args.status) if args.status else None
    cwd = Path(args.cwd)

    parent = own_cgroup() if args.parent == "self" else Path(args.parent)
    name = args.name or f"palomar-{secrets.token_hex(12)}"
    cgroup = parent / name
    started = time.monotonic()

    # Leaf trick: a cgroup with processes cannot enable controllers for its
    # children, so move ourselves to a leaf first. Harmless when already done.
    supervisor_leaf = parent / "supervisor"
    supervisor_leaf.mkdir(exist_ok=True)
    write(supervisor_leaf / "cgroup.procs", str(os.getpid()))
    wanted = "+memory +pids"
    try:
        write(parent / "cgroup.subtree_control", wanted)
    except OSError as error:
        raise SupervisorError(f"cannot enable controllers on {parent}: {error}") from error
    try:
        write(parent / "cgroup.subtree_control", "+cpu")
        cpu_delegated = True
    except OSError:
        cpu_delegated = False

    cgroup.mkdir()
    write_status(status_path, {
        "state": "started", "cgroup": str(cgroup), "supervisor_pid": os.getpid(),
        "cpu_delegated": cpu_delegated,
    })
    applied: dict[str, bool] = {}
    ordered = sorted(limits, key=lambda kv: LIMIT_ORDER.index(kv[0]))
    for key, val in ordered:
        try:
            write(cgroup / key, val)
            applied[key] = True
        except OSError as error:
            if key in BEST_EFFORT_LIMITS:
                applied[key] = False
            else:
                kill_tree(cgroup)
                raise SupervisorError(f"cannot apply {key}={val} on {cgroup}: {error}") from error

    # Placement acknowledgement travels over a pipe the child writes only after
    # it has joined the cgroup and applied its rlimits; the parent refuses to
    # count a run that never acknowledged.
    ack_r, ack_w = os.pipe()
    stdout_fd = None
    if args.stdout:
        stdout_fd = os.open(args.stdout, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    pid = os.fork()
    if pid == 0:  # child
        try:
            os.close(ack_r)
            if stdout_fd is not None:
                os.dup2(stdout_fd, 1)
                os.close(stdout_fd)
            write(cgroup / "cgroup.procs", str(os.getpid()))
            for rl, value in rlimits:
                # An unprivileged process cannot raise a hard limit; the ceiling
                # is then the inherited hard limit, which the status records.
                _, hard = resource.getrlimit(rl)
                if hard != resource.RLIM_INFINITY:
                    value = min(value, hard)
                resource.setrlimit(rl, (value, value))
            os.chdir(cwd)
            libc = ctypes.CDLL(None, use_errno=True)
            libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)
            libc.prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0)
            os.write(ack_w, b"ok\n")
            os.close(ack_w)
            os.execve(command[0], command, env)
        except BaseException as error:  # noqa: BLE001 -- must not return into the parent's code
            try:
                os.write(ack_w, f"fail:{type(error).__name__}:{error}\n".encode())
            except OSError:
                pass
            os._exit(127)
    os.close(ack_w)
    if stdout_fd is not None:
        os.close(stdout_fd)
    ack = b""
    while not ack.endswith(b"\n"):
        chunk = os.read(ack_r, 256)
        if not chunk:
            break
        ack += chunk
    os.close(ack_r)
    placement_ok = ack == b"ok\n"
    placement_error = None if placement_ok else ack.decode(errors="replace").strip()

    stop = threading.Event()

    def on_liveness_lost() -> None:
        stop.set()

    if args.liveness:
        # The verifier holds the FIFO open O_RDWR for the run's lifetime and
        # never writes; POLLHUP on the read side means every writer is gone,
        # including the case where it died before this process started.
        liveness_fd = os.open(args.liveness, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)

        def watch() -> None:
            poller = select.poll()
            poller.register(liveness_fd, select.POLLIN | select.POLLHUP | select.POLLERR)
            while True:
                events = poller.poll()
                if any(flags & (select.POLLHUP | select.POLLERR) for _, flags in events):
                    break
                try:
                    if os.read(liveness_fd, 256) == b"":
                        break
                except BlockingIOError:
                    continue
                except OSError:
                    break
            on_liveness_lost()
        threading.Thread(target=watch, daemon=True).start()

    def on_signal(signum, frame):  # noqa: ANN001
        stop.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    deadline_fired = False
    exit_status: int | None = None
    term_signal: int | None = None
    term_sent_at: float | None = None
    while True:
        wpid, status = os.waitpid(pid, os.WNOHANG)
        if wpid == pid:
            if os.WIFEXITED(status):
                exit_status = os.WEXITSTATUS(status)
            elif os.WIFSIGNALED(status):
                term_signal = os.WTERMSIG(status)
            break
        now = time.monotonic()
        if stop.is_set():
            kill_tree(cgroup)
            deadline_fired = deadline_fired or False
        elif not deadline_fired and now - started >= args.deadline:
            deadline_fired = True
            term_sent_at = now
            for p in read_procs(cgroup):
                try:
                    os.kill(p, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        elif deadline_fired and term_sent_at is not None and now - term_sent_at >= args.grace:
            kill_tree(cgroup)
        time.sleep(POLL_SECONDS)
    # KillMode=control-group: nothing the workload left behind survives the phase.
    kill_tree(cgroup)
    still_populated = populated(cgroup)

    def read_int(path: Path) -> int | None:
        try:
            raw = path.read_text().strip()
            return int(raw) if raw.isdigit() else None
        except OSError:
            return None

    def read_kv(path: Path) -> dict[str, int]:
        out: dict[str, int] = {}
        try:
            for line in path.read_text().splitlines():
                key, _, raw = line.partition(" ")
                if raw.strip().isdigit():
                    out[key] = int(raw)
        except OSError:
            pass
        return out

    write_status(status_path, {
        "state": "finished", "cgroup": str(cgroup), "supervisor_pid": os.getpid(),
        "exit_status": exit_status, "term_signal": term_signal,
        "deadline_fired": deadline_fired, "liveness_lost": stop.is_set(),
        "elapsed": round(time.monotonic() - started, 3),
        "placement_ok": placement_ok, "placement_error": placement_error,
        "cpu_delegated": cpu_delegated, "limits_applied": applied,
        "rlimits_applied": rlimits_effective,
        "memory_events": read_kv(cgroup / "memory.events"),
        "memory_peak": read_int(cgroup / "memory.peak"),
        "pids_events": read_kv(cgroup / "pids.events"),
        "cpu_stat": read_kv(cgroup / "cpu.stat"),
        "populated_after_kill": still_populated,
    })
    if args.collect:
        for _ in range(20):
            try:
                cgroup.rmdir()
                break
            except OSError:
                time.sleep(0.1)
    if stop.is_set() and exit_status is None and term_signal is None:
        return 143
    if exit_status is not None:
        return exit_status
    return 128 + (term_signal or 9)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except SupervisorError as error:
        # The one thing that may reach stderr: a babysitter that could not even
        # start is an infrastructure fault, and the verifier classifies it so.
        sys.stderr.write(f"supervise_cgroup: {error}\n")
        sys.exit(125)
