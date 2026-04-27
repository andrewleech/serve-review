"""On-disk cache for daemon PID files and approved review decisions.

Two kinds of state live here:

1. Decision cache: ``decisions/{diff_hash}.json``. Written by the daemon when a
   review is decided, looked up by the daemon when a new review is submitted.
   Keyed by diff content hash so identical diffs (e.g. after a rebase) hit cache
   instead of re-prompting. The cache is shared across all daemons regardless
   of port.

2. PID files: ``daemon-{port}.pid``. One per running daemon. The client uses
   these to discover whether a daemon is already running on a given port.

Cache files older than 48h are swept on daemon startup. PID files for dead
processes are cleaned up lazily by readers.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from serve_review.models import CachedDecision, ReviewDecision

CACHE_DIR = Path("~/.cache/serve-review").expanduser()

_PID_FILE_RE = re.compile(r"^daemon-(\d+)\.pid$")


def _ensure_dir(path: Path) -> Path:
    """Create the directory if missing with mode 0700, return it.

    The cache holds review diffs and PID files. Both are single-user state, so
    we restrict the directory to the owner to keep filenames out of other
    users' listings on multi-tenant hosts. File contents are already 0600 via
    ``tempfile.mkstemp``.
    """
    path.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        path.chmod(0o700)
    return path


def decisions_dir() -> Path:
    """Directory where cached decisions are persisted. Created on demand."""
    return _ensure_dir(CACHE_DIR / "decisions")


def pid_file(port: int) -> Path:
    """Path to the per-port PID file. Parent directory is created on demand."""
    _ensure_dir(CACHE_DIR)
    return CACHE_DIR / f"daemon-{port}.pid"


def log_file() -> Path:
    """Path to the shared daemon log. Parent directory is created on demand."""
    _ensure_dir(CACHE_DIR)
    return CACHE_DIR / "daemon.log"


# --- Decision cache ---


def _decision_path(diff_hash: str) -> Path:
    return decisions_dir() / f"{diff_hash}.json"


def lookup_cached_decision(diff_hash: str, max_age_hours: int = 48) -> CachedDecision | None:
    """Return the cached decision for ``diff_hash`` if present and fresh.

    Silently returns None on missing file, malformed JSON, or stale entries.
    Stale entries are not deleted here; ``cleanup_stale_decisions`` handles that
    in bulk so this function stays read-only.
    """
    path = _decision_path(diff_hash)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        return None

    try:
        data = json.loads(raw)
        cached = CachedDecision.from_dict(data)
    except (json.JSONDecodeError, KeyError, ValueError):
        return None

    try:
        when = _parse_iso(cached.timestamp)
    except ValueError:
        return None

    if datetime.now(UTC) - when > timedelta(hours=max_age_hours):
        return None

    return cached


def store_decision(
    diff_hash: str,
    decision: ReviewDecision,
    branch: str = "",
    remote: str = "",
) -> None:
    """Persist a decision to the cache. Atomic via temp-file + rename.

    Two daemons writing the same hash concurrently is benign: both produce
    semantically equivalent JSON, and ``os.replace`` guarantees the final file
    is one or the other in full, never a partial mix.
    """
    cached = CachedDecision(
        diff_hash=diff_hash,
        timestamp=datetime.now(UTC).isoformat(timespec="seconds"),
        decision=decision,
        branch=branch,
        remote=remote,
    )
    target = _decision_path(diff_hash)
    decisions_dir()  # ensure parent exists

    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{diff_hash}.", suffix=".json.tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cached.to_dict(), f, indent=2)
        os.replace(tmp_path, target)
    except Exception:
        # Best-effort cleanup of the temp file on any error.
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


def cleanup_stale_decisions(max_age_hours: int = 48) -> int:
    """Delete cached decisions older than ``max_age_hours``. Returns count deleted."""
    target = CACHE_DIR / "decisions"
    if not target.exists():
        return 0

    cutoff = datetime.now(UTC) - timedelta(hours=max_age_hours)
    deleted = 0

    for entry in target.iterdir():
        if not entry.is_file() or entry.suffix != ".json":
            continue
        try:
            data = json.loads(entry.read_text(encoding="utf-8"))
            when = _parse_iso(data["timestamp"])
        except (OSError, json.JSONDecodeError, KeyError, ValueError):
            # Malformed entry; treat as stale.
            try:
                entry.unlink()
                deleted += 1
            except OSError:
                continue
            continue

        if when < cutoff:
            try:
                entry.unlink()
                deleted += 1
            except OSError:
                continue

    return deleted


def cache_age_human(timestamp: str) -> str:
    """Format an ISO 8601 timestamp as an age string for terminal output.

    Buckets: under 60s -> "just now", under 1h -> "Nm ago", under 1d -> "Nh ago",
    older -> "Nd ago". Decisions older than 48h won't be served from cache anyway,
    so days is a sufficient ceiling.
    """
    try:
        when = _parse_iso(timestamp)
    except ValueError:
        return "unknown age"

    delta = datetime.now(UTC) - when
    seconds = int(delta.total_seconds())

    if seconds < 0:
        return "just now"  # clock skew or future timestamp
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _parse_iso(timestamp: str) -> datetime:
    """Parse an ISO 8601 timestamp, defaulting naive timestamps to UTC."""
    when = datetime.fromisoformat(timestamp)
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when


# --- PID files ---


def _pid_alive(pid: int) -> bool:
    """Cheap liveness check: signal 0 doesn't deliver but raises if process is gone."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but is owned by someone else. Treat as alive.
        return True
    return True


def _pid_is_serve_review(pid: int) -> bool:
    """Verify the running PID is actually a serve-review daemon.

    Without this, a stale PID file from a long-dead daemon whose PID has been
    reused by an unrelated process would cause ``daemon stop --all`` to signal
    that unrelated process. We read /proc/<pid>/cmdline (Linux) and confirm
    the command line references this package. On platforms without /proc, we
    fall back to trusting the PID file (best-effort).
    """
    cmdline_path = Path(f"/proc/{pid}/cmdline")
    try:
        cmdline = cmdline_path.read_bytes()
    except (FileNotFoundError, NotADirectoryError, PermissionError):
        # No /proc on this platform, or no permission. Trust the PID file.
        return True
    except OSError:
        return False
    # cmdline is NUL-separated. Search for our module name in any argv slot.
    return b"serve_review" in cmdline or b"serve-review" in cmdline


def write_pid_file(port: int, pid: int) -> None:
    """Write the PID for the daemon listening on ``port``. Atomic via rename."""
    path = pid_file(port)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".daemon-{port}.", suffix=".pid.tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(f"{pid}\n")
        os.replace(tmp_path, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


def read_pid_file(port: int) -> int | None:
    """Return the live PID for the daemon on ``port``, or None.

    If the file exists but the PID is dead, the file is removed as a side effect
    so callers don't need to clean up after themselves.
    """
    path = pid_file(port)
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError:
        return None

    try:
        pid = int(raw)
    except ValueError:
        # Corrupt PID file; remove it so a fresh daemon can write.
        with contextlib.suppress(OSError):
            path.unlink()
        return None

    if not _pid_alive(pid):
        with contextlib.suppress(OSError):
            path.unlink()
        return None

    if not _pid_is_serve_review(pid):
        # PID was reused by an unrelated process. Treat the file as stale.
        with contextlib.suppress(OSError):
            path.unlink()
        return None

    return pid


def remove_pid_file(port: int) -> None:
    """Remove the PID file for ``port`` if it exists."""
    with contextlib.suppress(FileNotFoundError, OSError):
        pid_file(port).unlink()


def list_daemons() -> list[tuple[int, int]]:
    """Return ``(port, pid)`` for every live daemon. Cleans up stale PID files.

    Used by ``daemon status`` to enumerate running daemons. Order is by port
    ascending.
    """
    if not CACHE_DIR.exists():
        return []

    daemons: list[tuple[int, int]] = []
    for entry in CACHE_DIR.iterdir():
        match = _PID_FILE_RE.match(entry.name)
        if not match:
            continue
        port = int(match.group(1))
        pid = read_pid_file(port)  # validates and cleans up if dead
        if pid is not None:
            daemons.append((port, pid))

    daemons.sort(key=lambda pair: pair[0])
    return daemons
