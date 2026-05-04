"""Hook-facing daemon client.

Submits review requests to a running daemon over HTTP, blocks for the decision
via Server-Sent Events, and auto-spawns a daemon if none is running on the
target port. Anything that goes wrong in this client; daemon unreachable,
malformed protocol, spawn failure, surfaces as ``DaemonError`` so the calling
git hook can fall back to standalone mode without losing functionality.

Synchronous on purpose: git hooks invoke us from sync context, and the only
HTTP we do is short-poll health, a single POST, and a long-running SSE read.
``urllib.request`` from the stdlib is sufficient and avoids adding a runtime
dependency on httpx.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any

from serve_review import cache
from serve_review.models import (
    ReviewDecision,
    ReviewRequest,
    compute_diff_hash,
)


class DaemonError(Exception):
    """Raised when the daemon is unreachable or the protocol fails.

    The CLI catches this to fall back to standalone mode, which keeps the
    git hook functional even if daemon code has bugs.
    """


def get_health(port: int) -> dict[str, Any]:
    """Get health status from the daemon.

    Returns a dict with daemon status info. Raises DaemonError if unreachable.
    """
    url = _get_daemon_url(port, "/api/health")
    try:
        kwargs: dict[str, Any] = {"timeout": 2.0}
        if url.startswith("https://"):
            kwargs["context"] = _get_https_context()

        with urllib.request.urlopen(url, **kwargs) as resp:
            if resp.status != 200:
                raise DaemonError(f"health check returned {resp.status}")
            data: Any = json.loads(resp.read().decode("utf-8"))
            return dict(data) if isinstance(data, dict) else {}
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise DaemonError(f"health check failed: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise DaemonError(f"health response invalid JSON: {exc}") from exc


def _get_daemon_url(port: int, path: str = "") -> str:
    """Get the correct URL for the daemon, detecting scheme from sidecar file."""
    scheme = "http"
    try:
        scheme_file = cache.scheme_file(port)
        if scheme_file.exists():
            scheme = scheme_file.read_text().strip()
    except Exception:
        pass
    return f"{scheme}://127.0.0.1:{port}{path}"


def _get_https_context() -> Any:
    """Create SSL context for loopback HTTPS (no hostname verification)."""
    import ssl

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def daemon_is_running(port: int) -> bool:
    """Return True if a healthy daemon is listening on ``port``.

    Uses the PID file as a fast-path check; a stale PID file is cleaned up by
    ``cache.read_pid_file`` itself. A live PID file alone is not sufficient,
    so we also probe ``/api/health`` with a short timeout.
    """
    try:
        get_health(port)
        return True
    except DaemonError:
        return False


def start_daemon(host: str, port: int) -> bool:
    """Spawn a detached daemon process and wait for it to become healthy.

    Returns True if the daemon is responding within 5 seconds, False otherwise.
    The subprocess is fully detached: stdin closed, stdout/stderr appended to
    ``cache.log_file()``, and placed in its own session so a hook exit doesn't
    take it down. Stays silent on stdout/stderr to avoid corrupting hook output.
    """
    log_path = cache.log_file()
    try:
        # Popen dups the fd into the child; the parent's handle is closed on
        # exit from the with-block. The child keeps the file open via its dup.
        with open(str(log_path), "ab") as log_fp:
            try:
                subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "serve_review.daemon",
                        "--host",
                        host,
                        "--port",
                        str(port),
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=log_fp,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            except OSError:
                return False
    except OSError:
        return False

    # 10s budget covers cold-start cost on slower hardware (uvicorn import,
    # asyncio loop bring-up, port bind). The PID-file check inside
    # daemon_is_running is cheap so the polling overhead is negligible.
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if daemon_is_running(port):
            return True
        time.sleep(0.2)
    return False


def kill_daemon(port: int) -> None:
    """Stop the daemon on ``port`` if running. SIGTERM, then SIGKILL after 3s.

    Idempotent: a missing PID file or already-dead process is treated as success.
    """
    pid = cache.read_pid_file(port)
    if pid is None:
        return

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        cache.remove_pid_file(port)
        return

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if cache.read_pid_file(port) is None:
            cache.remove_pid_file(port)
            return
        time.sleep(0.1)

    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGKILL)

    cache.remove_pid_file(port)


def ensure_daemon(host: str, port: int) -> None:
    """Make sure a daemon is running on ``port``; spawn one if not.

    If a PID file claims the port but the daemon isn't responding to health
    checks, the stale process is killed before a new one is spawned. Raises
    ``DaemonError`` if the daemon won't start.

    Concurrent first-push race: two clients seeing "no daemon" simultaneously
    will both call ``start_daemon``. The second's spawned process will fail
    in ``run_daemon`` because the first writes the PID file before binding;
    the second client's health-check then sees the first daemon's bound
    port and proceeds normally. Self-healing.
    """
    if daemon_is_running(port):
        return

    # Live PID but unhealthy: hung daemon. Kill it before spawning a replacement.
    if cache.read_pid_file(port) is not None:
        kill_daemon(port)

    if not start_daemon(host, port):
        raise DaemonError(f"failed to start daemon on port {port}")


def submit_review(
    port: int, review: ReviewRequest, diff_hash: str
) -> tuple[str, ReviewDecision | None, str | None]:
    """POST a review to the daemon's queue.

    Returns ``(review_id, cached_decision_or_None, cached_at_or_None)``. When
    the daemon reports a cache hit, the decision and ISO timestamp are returned
    inline so the caller never needs to wait. On a fresh submission both
    cached fields are None and the caller must follow up with
    ``wait_for_decision``.
    """
    url = _get_daemon_url(port, "/api/queue")
    payload = json.dumps({"review": review.to_dict(), "diff_hash": diff_hash}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        kwargs: dict[str, Any] = {"timeout": 10.0}
        if url.startswith("https://"):
            kwargs["context"] = _get_https_context()
        with urllib.request.urlopen(req, **kwargs) as resp:
            body = resp.read().decode("utf-8")
    except (urllib.error.URLError, OSError) as exc:
        raise DaemonError(f"failed to submit review: {exc}") from exc

    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise DaemonError(f"daemon returned invalid JSON: {exc}") from exc

    review_id = data["review_id"]
    if data.get("cached"):
        decision = ReviewDecision.from_dict(data["decision"])
        cached_at = data.get("cached_at")
        return review_id, decision, cached_at
    return review_id, None, None


_SSE_READ_TIMEOUT = 45.0
"""Per-line read timeout. The daemon sends keepalive comments every 20s, so
silence longer than this means the connection is dead (TCP half-open from a
sleeping laptop, NAT timeout, daemon crash). Without this, urlopen's default
of ``socket.getdefaulttimeout()`` could leave the hook hung indefinitely."""


def wait_for_decision(port: int, review_id: str) -> ReviewDecision:
    """Block on the daemon's SSE stream until a decision arrives.

    The daemon emits ``: keepalive`` SSE comments every 20s, so a silence
    longer than ``_SSE_READ_TIMEOUT`` indicates the connection is dead.
    Returns the parsed ``ReviewDecision``. Raises ``DaemonError`` if the
    stream emits an error event, closes without a decision, or fails at the
    transport level (including read timeout).
    """
    url = _get_daemon_url(port, f"/api/queue/{review_id}/decision")
    req = urllib.request.Request(url)

    try:
        kwargs: dict[str, Any] = {"timeout": _SSE_READ_TIMEOUT}
        if url.startswith("https://"):
            kwargs["context"] = _get_https_context()
        resp = urllib.request.urlopen(req, **kwargs)
    except (urllib.error.URLError, OSError) as exc:
        raise DaemonError(f"failed to open decision stream: {exc}") from exc

    try:
        current_event: str | None = None
        for raw_line in resp:
            line = raw_line.decode("utf-8").rstrip("\n").rstrip("\r")
            if line.startswith(":"):
                # SSE comment / keepalive — proves the connection is alive.
                continue
            if line.startswith("event: "):
                current_event = line[len("event: ") :]
            elif line.startswith("data: "):
                data_str = line[len("data: ") :]
                if current_event == "decision":
                    try:
                        return ReviewDecision.from_dict(json.loads(data_str))
                    except (json.JSONDecodeError, KeyError, ValueError) as exc:
                        raise DaemonError(f"malformed decision payload: {exc}") from exc
                if current_event == "error":
                    raise DaemonError(f"daemon error: {data_str}")
            elif line == "":
                current_event = None
    except TimeoutError as exc:
        raise DaemonError(
            f"decision stream silent for >{_SSE_READ_TIMEOUT:.0f}s; daemon may be unreachable"
        ) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise DaemonError(f"decision stream failed: {exc}") from exc
    finally:
        resp.close()

    raise DaemonError("daemon SSE stream closed without decision")


def submit_and_wait(
    host: str, port: int, review: ReviewRequest
) -> tuple[ReviewDecision, str | None]:
    """End-to-end: ensure the daemon is up, submit, wait for the decision.

    Returns ``(decision, cached_at_iso_or_None)``. ``cached_at`` is non-None
    iff the daemon served the decision from cache without enqueuing.
    """
    diff_hash = compute_diff_hash(review.files)
    ensure_daemon(host, port)

    review_id, cached_decision, cached_at = submit_review(port, review, diff_hash)
    if cached_decision is not None:
        return cached_decision, cached_at

    decision = wait_for_decision(port, review_id)
    return decision, None
