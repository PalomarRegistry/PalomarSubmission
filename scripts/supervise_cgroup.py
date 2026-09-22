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
enables the controllers on the parent, and creates ``<parent>/<name>`` carrying
the limits, with the workload one level further down in ``<name>/leaf``. The
extra level matters: bubblewrap gives the payload its own cgroup namespace
rooted at ``leaf``, so even where the kernel lets a namespace root rewrite its
own controller files (no ``nsdelegate``), the limits live in a cgroup the
payload cannot see. Nothing here ever needs to write a cgroup it does not own.
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
POPULATED_WAIT_SECONDS = 30.0
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
    """Every process in the cgroup and its descendants."""
    pids: list[int] = []
    for directory in (cgroup, *[p for p in cgroup.rglob("*") if p.is_dir()]):
        try:
            pids.extend(int(p) for p in (directory / "cgroup.procs").read_text().split())
        except (OSError, ValueError):
            continue
    return pids


def populated(cgroup: Path) -> bool | None:
    try:
        for line in (cgroup / "cgroup.events").read_text().splitlines():
            key, _, raw = line.partition(" ")
            if key == "populated":
                return raw.strip() == "1"
    except OSError:
        return None
    return None


def kill_tree(cgroup: Path) -> bool:
    """Terminate everything in the cgroup, whatever it did to signal handling.

    Returns whether the cgroup was observed empty afterwards.
    """
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
    return populated(cgroup) is False


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


def open_liveness(path: str) -> int:
    """Open the verifier's FIFO and prove a writer is present right now.

    A FIFO reader that has never seen a writer gets no ``POLLHUP``, so a
    verifier that died before this process opened the FIFO would otherwise go
    unnoticed. A non-blocking read settles it: ``EAGAIN`` means a writer holds
    the other end, end-of-file means nobody does.
    """
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        if os.read(fd, 1) == b"":
            raise SupervisorError("verifier is not holding the liveness FIFO")
    except BlockingIOError:
        pass
    return fd


def wait_status(status: int) -> tuple[int | None, int | None]:
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status), None
    if os.WIFSIGNALED(status):
        return None, os.WTERMSIG(status)
    return None, None


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
    parser.add_argument(
        "--sandbox-status-fd", type=int,
        help="descriptor the sandbox reports on (bwrap --json-status-fd); a phase whose "
             "sandbox never started is an infrastructure failure, not a candidate result",
    )
    parser.add_argument(
        "--pass-file", action="append", default=[],
        help="FD=PATH: open PATH read-only and hand it to the workload as descriptor FD",
    )
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
    passed_files: list[tuple[int, int]] = []
    for key, val in parse_pairs(args.pass_file, "pass-file"):
        if not key.isdigit() or int(key) < 3:
            raise SupervisorError(f"pass-file descriptor must be 3 or above: {key!r}")
        passed_files.append((int(key), os.open(val, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)))
    if args.sandbox_status_fd is not None and args.sandbox_status_fd < 3:
        raise SupervisorError("sandbox status descriptor must be 3 or above")
    env = dict(parse_pairs(args.setenv, "setenv"))
    status_path = Path(args.status) if args.status else None
    cwd = Path(args.cwd)

    # Liveness is checked before anything is created: a verifier that is
    # already gone gets no workload at all.
    liveness_fd = open_liveness(args.liveness) if args.liveness else None

    parent = own_cgroup() if args.parent == "self" else Path(args.parent)
    name = args.name or f"palomar-{secrets.token_hex(12)}"
    cgroup = parent / name
    leaf = cgroup / "leaf"
    started = time.monotonic()

    # Leaf trick: a cgroup with processes cannot enable controllers for its
    # children, so move ourselves to a leaf first. Harmless when already done.
    supervisor_leaf = parent / "supervisor"
    supervisor_leaf.mkdir(exist_ok=True)
    write(supervisor_leaf / "cgroup.procs", str(os.getpid()))
    try:
        write(parent / "cgroup.subtree_control", "+memory +pids")
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
    # The limits stay on `cgroup`; the workload lives in `leaf` beneath it.
    try:
        write(cgroup / "cgroup.subtree_control", "+memory +pids")
        if cpu_delegated:
            write(cgroup / "cgroup.subtree_control", "+cpu")
    except OSError as error:
        raise SupervisorError(f"cannot enable controllers on {cgroup}: {error}") from error
    leaf.mkdir()

    # Placement acknowledgement travels over a close-on-exec pipe: the child
    # writes `placed` after it has joined the cgroup and applied its rlimits,
    # and the pipe closes by itself when execve succeeds. Anything after
    # `placed` is therefore a launch failure, which the verifier must never
    # attribute to the candidate.
    ack_r, ack_w = os.pipe()
    stdout_fd = None
    if args.stdout:
        stdout_fd = os.open(args.stdout, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    sandbox_r = sandbox_w = None
    if args.sandbox_status_fd is not None:
        sandbox_r, sandbox_w = os.pipe()
    # Every descriptor the child must install at a fixed number is first lifted
    # above that range, so installing one cannot clobber another still waiting.
    installs: list[tuple[int, int]] = list(passed_files)
    if sandbox_w is not None:
        installs.append((args.sandbox_status_fd, sandbox_w))
    if stdout_fd is not None:
        installs.append((1, stdout_fd))
    ceiling = max((target for target, _ in installs), default=2) + 1
    pid = os.fork()
    if pid == 0:  # child
        try:
            os.close(ack_r)
            if sandbox_r is not None:
                os.close(sandbox_r)
            lifted = []
            for target, source in installs:
                while source < ceiling:
                    source = os.dup(source)
                lifted.append((target, source))
            for target, source in lifted:
                os.dup2(source, target)  # dup2 clears close-on-exec on the target
            for _, source in lifted:
                os.close(source)
            write(leaf / "cgroup.procs", str(os.getpid()))
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
            os.write(ack_w, b"placed\n")
            os.execve(command[0], command, env)
        except BaseException as error:  # noqa: BLE001 -- must not return into the parent's code
            try:
                os.write(ack_w, f"fail:{type(error).__name__}:{error}\n".encode())
            except OSError:
                pass
            os._exit(127)
    os.close(ack_w)
    if sandbox_w is not None:
        os.close(sandbox_w)
    if stdout_fd is not None:
        os.close(stdout_fd)
    for _, source in passed_files:
        os.close(source)
    ack = b""
    while True:
        chunk = os.read(ack_r, 256)
        if not chunk:
            break
        ack += chunk
    os.close(ack_r)
    placement_ok = ack.startswith(b"placed\n")
    launch_error = ack[len(b"placed\n"):].decode(errors="replace").strip() if placement_ok else ""
    placement_error = None if placement_ok else ack.decode(errors="replace").strip()

    stop = threading.Event()
    sandbox_started: bool | None = None if sandbox_r is None else False

    def watch_sandbox() -> None:
        nonlocal sandbox_started
        buffered = b""
        while True:
            try:
                chunk = os.read(sandbox_r, 4096)
            except OSError:
                break
            if not chunk:
                break
            buffered += chunk
            if b'"child-pid"' in buffered:
                sandbox_started = True
        os.close(sandbox_r)

    if sandbox_r is not None:
        threading.Thread(target=watch_sandbox, daemon=True).start()

    if liveness_fd is not None:
        # The verifier holds the FIFO open O_RDWR for the run's lifetime and
        # never writes; POLLHUP on the read side means every writer is gone.
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
            stop.set()
        threading.Thread(target=watch, daemon=True).start()

    def on_signal(signum, frame):  # noqa: ANN001
        stop.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    # The direct child is the trusted launcher (observer, then bwrap). At the
    # deadline the workload gets SIGTERM and the grace period; the launcher is
    # left alone so that it can still collect its evidence, and the whole tree
    # is killed once the leader has exited and either the grace has elapsed or
    # nothing is left.
    deadline_fired = False
    exit_status: int | None = None
    term_signal: int | None = None
    term_sent_at: float | None = None
    leader_exited = False
    killed = False
    while True:
        if not leader_exited:
            wpid, status = os.waitpid(pid, os.WNOHANG)
            if wpid == pid:
                leader_exited = True
                exit_status, term_signal = wait_status(status)
        now = time.monotonic()
        if stop.is_set():
            killed = True
            kill_tree(cgroup)
        elif not deadline_fired and now - started >= args.deadline:
            deadline_fired = True
            term_sent_at = now
            for p in read_procs(cgroup):
                if p == pid:
                    continue
                try:
                    os.kill(p, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        elif deadline_fired and term_sent_at is not None and now - term_sent_at >= args.grace:
            killed = True
            kill_tree(cgroup)
        if leader_exited:
            grace_pending = deadline_fired and not killed and populated(cgroup)
            if not grace_pending:
                break
        time.sleep(POLL_SECONDS)
    # KillMode=control-group: nothing the workload left behind survives the phase.
    emptied = kill_tree(cgroup)

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
        "launch_error": launch_error or None,
        "sandbox_started": sandbox_started,
        "cpu_delegated": cpu_delegated, "limits_applied": applied,
        "rlimits_applied": rlimits_effective,
        "memory_events": read_kv(cgroup / "memory.events"),
        "memory_peak": read_int(cgroup / "memory.peak"),
        "pids_events": read_kv(cgroup / "pids.events"),
        "cpu_stat": read_kv(cgroup / "cpu.stat"),
        "populated_after_kill": not emptied,
    })
    if args.collect:
        for directory in (leaf, cgroup):
            for _ in range(20):
                try:
                    directory.rmdir()
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
        # The status file is the only diagnostic channel that is not the
        # payload's; stderr is left to the workload.
        if "--status" in sys.argv:
            try:
                write_status(Path(sys.argv[sys.argv.index("--status") + 1]),
                             {"state": "failed", "error": str(error)})
            except (OSError, ValueError, IndexError):
                pass
        sys.exit(125)
