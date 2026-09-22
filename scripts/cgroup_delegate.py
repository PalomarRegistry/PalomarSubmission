#!/usr/bin/env python3
"""Enter a cgroup v2 subtree delegated to an unprivileged user, then run a command as that user.

The root half of Palomar's supervisor, for runners without systemd (a Namespace
job container: PID 1 is a shell, ``/sys/fs/cgroup`` is a writable cgroup2 mount
and the job sits at the namespace root). It does, once and idempotently, what
systemd's ``Delegate=`` would have done at boot:

1. The namespace root cannot enable controllers for its children while it has
   processes of its own, so every process at the root is moved into a leaf,
   ``palomar-host``, and ``memory pids`` (and ``cpu`` when available) are enabled
   at the root.
2. ``palomar`` is created beneath the root with the same controllers enabled.
3. A fresh ``palomar/run-<token>`` is created, this process moves into it, and
   the run directory is chowned to the target user.
4. Privileges are dropped to that user and the command is exec'd, inheriting the
   cgroup. The command is the babysitter with ``--parent self``.

Nothing here climbs to an ancestor merely because it is writable: the only
tree it manages is the container root of a runner that has no other manager,
and it refuses to run where PID 1 is systemd. Meant to be invoked as
``sudo -n cgroup_delegate.py --uid U --gid G -- <command...>``.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import secrets
import sys
import time
from pathlib import Path

CGROUP_ROOT = Path("/sys/fs/cgroup")
HOST_LEAF = CGROUP_ROOT / "palomar-host"
SUBTREE = CGROUP_ROOT / "palomar"


class DelegationError(RuntimeError):
    pass


def write(path: Path, value: str) -> None:
    with open(path, "w", encoding="ascii") as handle:
        handle.write(value)


def controllers(path: Path) -> set[str]:
    try:
        return set((path / "cgroup.controllers").read_text().split())
    except OSError:
        return set()


def enabled(path: Path) -> set[str]:
    try:
        return set((path / "cgroup.subtree_control").read_text().split())
    except OSError:
        return set()


def procs(path: Path) -> list[int]:
    try:
        return [int(p) for p in (path / "cgroup.procs").read_text().split()]
    except (OSError, ValueError):
        return []


def preflight() -> dict:
    pid1 = Path("/proc/1/comm").read_text().strip()
    if pid1 == "systemd":
        raise DelegationError("PID 1 is systemd; obtain a Delegate=yes scope instead of managing the root")
    mount = ""
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        parts = line.split()
        if len(parts) > 8 and parts[4] == str(CGROUP_ROOT):
            mount = " ".join(parts[5:])
            break
    if "cgroup2" not in mount:
        raise DelegationError(f"{CGROUP_ROOT} is not a cgroup2 mount: {mount!r}")
    have = controllers(CGROUP_ROOT)
    missing = {"memory", "pids"} - have
    if missing:
        raise DelegationError(
            f"root cgroup lacks controllers {sorted(missing)}; the container runtime must grant them"
        )
    return {"pid1": pid1, "mount": mount, "root_controllers": sorted(have)}


def drain_root() -> int:
    """Move every root process into the host leaf. Returns how many remain."""
    HOST_LEAF.mkdir(exist_ok=True)
    for _ in range(8):
        remaining = procs(CGROUP_ROOT)
        if not remaining:
            break
        for pid in remaining:
            try:
                write(HOST_LEAF / "cgroup.procs", str(pid))
            except OSError:
                pass  # exited, or a kernel thread that cannot move
        time.sleep(0.05)
    return len(procs(CGROUP_ROOT))


def enable(path: Path, names: list[str]) -> list[str]:
    applied = []
    for name in names:
        try:
            write(path / "cgroup.subtree_control", f"+{name}")
            applied.append(name)
        except OSError:
            if name in ("memory", "pids"):
                raise
    return applied


def bootstrap() -> dict:
    report = preflight()
    if not {"memory", "pids"} <= enabled(CGROUP_ROOT):
        left = drain_root()
        report["root_procs_left"] = left
        if left:
            raise DelegationError(f"{left} process(es) could not be moved out of the root cgroup")
    report["root_enabled"] = enable(CGROUP_ROOT, ["memory", "pids", "cpu"])
    SUBTREE.mkdir(exist_ok=True)
    report["subtree_enabled"] = enable(SUBTREE, ["memory", "pids", "cpu"])
    return report


def chown_tree(path: Path, uid: int, gid: int) -> None:
    os.chown(path, uid, gid)
    for child in path.iterdir():
        try:
            os.chown(child, uid, gid)
        except OSError:
            pass


def is_populated(path: Path) -> bool:
    try:
        return "populated 1" in (path / "cgroup.events").read_text()
    except OSError:
        return True  # unknown: treat as in use


def sweep_stale(*, locked: bool = False) -> int:
    """Remove run directories no process lives in any more.

    Only *empty* runs are removed. A populated run belongs to a verifier that is
    still working, or to a workload whose babysitter will tear it down when its
    liveness FIFO reports the verifier gone; killing it from here would be a way
    for one verifier to end another's phase. The directory fd lock keeps two
    concurrent bootstraps from racing each other's sweep.
    """
    removed = 0
    lock = os.open(SUBTREE, os.O_RDONLY | os.O_DIRECTORY)
    try:
        if not locked:
            fcntl.flock(lock, fcntl.LOCK_EX)
        for run in SUBTREE.glob("run-*"):
            if is_populated(run):
                continue
            try:
                for phase in run.iterdir():
                    if phase.is_dir():
                        phase.rmdir()
                run.rmdir()
                removed += 1
            except OSError:
                pass
    finally:
        os.close(lock)
    return removed


def teardown() -> dict:
    removed = sweep_stale()
    try:
        SUBTREE.rmdir()
    except OSError:
        pass
    return {"removed_runs": removed}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--uid", type=int)
    parser.add_argument("--gid", type=int)
    parser.add_argument("--report")
    parser.add_argument("--teardown", action="store_true")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        raise DelegationError("must run as root (via sudo -n)")
    if args.teardown:
        report = teardown()
        if args.report:
            Path(args.report).write_text(json.dumps(report, sort_keys=True) + "\n")
        return 0
    if args.uid is None or args.gid is None or args.uid == 0:
        raise DelegationError("--uid and --gid of an unprivileged user are required")
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        raise DelegationError("no command to run in the delegated subtree")

    report = bootstrap()
    # One lock covers the sweep and this run's creation and placement, so a
    # concurrent bootstrap cannot mistake a run that exists but is not yet
    # populated for a stale one.
    lock = os.open(SUBTREE, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        report["stale_runs_removed"] = sweep_stale(locked=True)
        run_dir = SUBTREE / f"run-{secrets.token_hex(8)}"
        run_dir.mkdir()
        write(run_dir / "cgroup.procs", str(os.getpid()))
        chown_tree(run_dir, args.uid, args.gid)
    finally:
        os.close(lock)
    report["run_cgroup"] = str(run_dir)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as handle:
            json.dump(report, handle, sort_keys=True)
            handle.write("\n")
        os.chown(args.report, args.uid, args.gid)

    os.setgroups([])
    os.setgid(args.gid)
    os.setuid(args.uid)
    if os.getuid() != args.uid or os.geteuid() != args.uid:
        raise DelegationError("privilege drop did not take")
    os.execv(command[0], command)
    return 127  # unreachable


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except DelegationError as error:
        sys.stderr.write(f"cgroup_delegate: {error}\n")
        sys.exit(125)
