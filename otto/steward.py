"""System steward (#72) — keep the Mac, and Orrin's own footprint, healthy.

Pure, dependency-free helpers (no psutil): read machine + Orrin-fleet health via `ps`/`vm_stat`/
`shutil`, and perform ONLY Orrin-owned remediations — sweep Orrin's orphaned STT workers, clear a
stale single-instance lock, trim Orrin's own oversized logs. Everything here is safe to run
unattended: it never touches another app, the system, user files, or a live child of the running
Orrin. Non-Orrin problems (low disk, high memory from other apps) are reported, never acted on —
that decision lives in the orchestrator, which alerts the user.

Kept side-effect-light and import-light so it's unit-testable and can run on the event loop's
executor without pulling in the voice stack.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
from pathlib import Path

# Path markers that identify OUR processes (main + multiprocessing STT workers), mirroring
# menubar._OTTO_PROC_MARKERS so the steward and the launcher agree on what "ours" means.
_OTTO_PROC_MARKERS = ("personal-assist/.venv", "Orrin.app/Contents/MacOS",
                      "Application Support/Otto/venv", "otto-assist")


def _is_otto_worker(cmd: str) -> bool:
    """True if this command line is one of OUR RealtimeSTT multiprocessing workers. The EXECUTABLE
    (argv[0]) must be the otto python / Orrin binary, so a shell/editor/grep that merely mentions
    the path isn't matched."""
    if "multiprocessing" not in cmd:
        return False
    exe = cmd.split(" ", 1)[0]
    return any(m in exe for m in _OTTO_PROC_MARKERS)


def _is_otto_proc(cmd: str) -> bool:
    """True for any of our processes (main or worker) — used to sum Orrin's total footprint."""
    exe = cmd.split(" ", 1)[0]
    return any(m in exe for m in _OTTO_PROC_MARKERS)


def _ps() -> list[tuple[int, int, int, str]]:
    """(pid, ppid, rss_kb, command) for every process, best-effort (empty on failure)."""
    try:
        out = subprocess.run(["ps", "-axo", "pid=,ppid=,rss=,command="],
                             capture_output=True, text=True, timeout=5)
    except Exception:
        return []
    rows: list[tuple[int, int, int, str]] = []
    for line in out.stdout.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        try:
            rows.append((int(parts[0]), int(parts[1]), int(parts[2]), parts[3]))
        except ValueError:
            continue
    return rows


def orphan_workers(rows: list[tuple[int, int, int, str]] | None = None) -> list[int]:
    """PIDs of OUR STT workers that have been reparented to launchd (ppid==1) — i.e. orphans from a
    prior crashed/duplicate Orrin. A worker that is still a child of the running main is NOT an
    orphan and is never listed, so sweeping these can't touch the live instance."""
    rows = rows if rows is not None else _ps()
    return [pid for (pid, ppid, _rss, cmd) in rows if ppid == 1 and _is_otto_worker(cmd)]


def sweep_orphans(rows: list[tuple[int, int, int, str]] | None = None) -> int:
    """SIGKILL Orrin's orphaned STT workers (ppid==1). Returns the count killed. Orrin-owned only."""
    killed = 0
    for pid in orphan_workers(rows):
        try:
            os.kill(pid, signal.SIGKILL)
            killed += 1
        except Exception:
            pass
    return killed


def orrin_rss_mb(rows: list[tuple[int, int, int, str]] | None = None) -> float:
    """Total resident memory (MB) of all Orrin processes — main + workers."""
    rows = rows if rows is not None else _ps()
    kb = sum(rss for (_pid, _ppid, rss, cmd) in rows if _is_otto_proc(cmd))
    return round(kb / 1024.0, 1)


def free_mem_gb() -> float:
    """Approx free (free + inactive) physical memory in GB, via vm_stat. 0.0 if unavailable."""
    try:
        out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5)
    except Exception:
        return 0.0
    page = 4096
    free = inactive = 0
    for line in out.stdout.splitlines():
        if line.startswith("Mach Virtual Memory Statistics"):
            # e.g. "(page size of 16384 bytes)"
            for tok in line.replace("(", " ").replace(")", " ").split():
                if tok.isdigit():
                    page = int(tok)
                    break
        elif line.startswith("Pages free:"):
            free = int(line.rsplit(" ", 1)[-1].strip(". "))
        elif line.startswith("Pages inactive:"):
            inactive = int(line.rsplit(" ", 1)[-1].strip(". "))
    return round((free + inactive) * page / (1024 ** 3), 2)


def disk_free_gb(path: str = "/") -> float:
    try:
        return round(shutil.disk_usage(path).free / (1024 ** 3), 1)
    except Exception:
        return 0.0


def clear_stale_lock(lock_path: str | Path, live_pid: int | None = None) -> bool:
    """Remove Orrin's single-instance lock file ONLY if it points at a dead pid (a ghost hold from a
    crashed instance). Never removes a lock held by the live main. Returns True if removed."""
    p = Path(lock_path)
    try:
        if not p.exists():
            return False
        pid_s = p.read_text().strip()
        pid = int(pid_s) if pid_s.isdigit() else None
    except Exception:
        return False
    if pid is None:
        return False
    if live_pid is not None and pid == live_pid:
        return False                                  # held by us — leave it
    try:
        os.kill(pid, 0)                               # pid alive → not stale
        return False
    except ProcessLookupError:
        pass                                          # dead → stale, remove below
    except Exception:
        return False
    try:
        p.unlink()
        return True
    except Exception:
        return False


def trim_logs(log_dir: str | Path, max_mb: float = 50.0) -> int:
    """Truncate Orrin's OWN *.log files that have grown past max_mb (keep the tail). Returns the
    count trimmed. Operates only inside the given directory (Orrin's log dir)."""
    d = Path(log_dir)
    if not d.is_dir():
        return 0
    cap = int(max_mb * 1024 * 1024)
    keep = cap // 2 or 1
    trimmed = 0
    for f in d.glob("*.log"):
        try:
            if f.stat().st_size <= cap:
                continue
            with open(f, "rb") as fh:
                fh.seek(-keep, os.SEEK_END)
                tail = fh.read()
            with open(f, "wb") as fh:
                fh.write(b"[... trimmed by Orrin steward ...]\n")
                fh.write(tail)
            trimmed += 1
        except Exception:
            pass
    return trimmed


def scan(*, disk_path: str = "/") -> dict:
    """One health snapshot: Orrin's footprint + orphan count, free memory, free disk."""
    rows = _ps()
    return {
        "orrin_rss_mb": orrin_rss_mb(rows),
        "orphan_workers": orphan_workers(rows),
        "free_mem_gb": free_mem_gb(),
        "disk_free_gb": disk_free_gb(disk_path),
    }
