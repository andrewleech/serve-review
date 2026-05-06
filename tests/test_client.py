"""Tests for the daemon client."""

from __future__ import annotations

import asyncio
import socket
import threading
import time
import urllib.error
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from serve_review import cache, client
from serve_review.client import (
    DaemonError,
    daemon_is_running,
    ensure_daemon,
    kill_daemon,
    submit_and_wait,
    submit_review,
    wait_for_decision,
)
from serve_review.models import (
    Decision,
    ReviewDecision,
    compute_diff_hash,
)

if TYPE_CHECKING:
    from pathlib import Path

    from serve_review.daemon import DaemonServer
    from serve_review.models import ReviewRequest


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect CACHE_DIR to a per-test tmp dir so tests can't see each other."""
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    return tmp_path


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def _run_in_thread(target, *args):  # type: ignore[no-untyped-def]
    """Run ``target`` in a daemon thread; return the thread."""
    t = threading.Thread(target=target, args=args, daemon=True)
    t.start()
    return t


class TestSchemeDetection:
    """Tests for _get_daemon_url scheme allowlist and sidecar handling."""

    def test_no_scheme_file_falls_back_to_http(self) -> None:
        url = client._get_daemon_url(54321, "/api/health")
        assert url == "http://127.0.0.1:54321/api/health"

    def test_https_scheme_is_honored(self) -> None:
        cache.scheme_file(54321).write_text("https")
        url = client._get_daemon_url(54321, "/x")
        assert url == "https://127.0.0.1:54321/x"

    def test_empty_scheme_file_falls_back_to_http(self) -> None:
        cache.scheme_file(54321).write_text("")
        url = client._get_daemon_url(54321, "/x")
        assert url == "http://127.0.0.1:54321/x"

    def test_unrecognised_scheme_falls_back_to_http(self) -> None:
        cache.scheme_file(54321).write_text("javascript")
        url = client._get_daemon_url(54321, "/x")
        assert url == "http://127.0.0.1:54321/x"

    def test_scheme_value_is_stripped(self) -> None:
        cache.scheme_file(54321).write_text("  https \n")
        url = client._get_daemon_url(54321, "/x")
        assert url == "https://127.0.0.1:54321/x"


class TestHttpsContext:
    def test_returns_ssl_context_with_verification_disabled(self) -> None:
        import ssl

        ctx = client._get_https_context()
        assert isinstance(ctx, ssl.SSLContext)
        assert ctx.check_hostname is False
        assert ctx.verify_mode == ssl.CERT_NONE


class TestDaemonIsRunning:
    def test_no_pid_file_returns_false(self) -> None:
        assert daemon_is_running(54321) is False

    def test_pid_file_but_no_server_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Pretend a PID is alive but no HTTP server is listening on the port.
        monkeypatch.setattr(cache, "read_pid_file", lambda port: 1)
        port = _free_port()  # nothing bound here
        assert daemon_is_running(port) is False

    async def test_live_daemon_returns_true(self, live_daemon: tuple[DaemonServer, int]) -> None:
        _, port = live_daemon
        # daemon_is_running blocks on a socket. Run it off the event loop so
        # the SSE/health endpoints can serve concurrently.
        result = await asyncio.get_event_loop().run_in_executor(None, daemon_is_running, port)
        assert result is True


class TestSubmitReview:
    async def test_submit_returns_review_id(
        self,
        live_daemon: tuple[DaemonServer, int],
        sample_review: ReviewRequest,
    ) -> None:
        _, port = live_daemon
        diff_hash = compute_diff_hash(sample_review.files)

        review_id, cached_decision, cached_at = await asyncio.get_event_loop().run_in_executor(
            None, submit_review, port, sample_review, diff_hash
        )
        assert isinstance(review_id, str)
        assert cached_decision is None
        assert cached_at is None

    async def test_cache_hit_returns_decision(
        self,
        live_daemon: tuple[DaemonServer, int],
        sample_review: ReviewRequest,
    ) -> None:
        _, port = live_daemon
        diff_hash = compute_diff_hash(sample_review.files)
        cache.store_decision(
            diff_hash,
            ReviewDecision(decision=Decision.APPROVE, overall_comment="cached"),
            branch="feature",
            remote="origin",
        )

        review_id, cached_decision, cached_at = await asyncio.get_event_loop().run_in_executor(
            None, submit_review, port, sample_review, diff_hash
        )
        assert isinstance(review_id, str)
        assert cached_decision is not None
        assert cached_decision.decision == Decision.APPROVE
        assert cached_decision.overall_comment == "cached"
        assert cached_at is not None
        assert "T" in cached_at

    def test_url_error_raises_daemon_error(
        self, sample_review: ReviewRequest, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_urlopen(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)
        with pytest.raises(DaemonError, match="failed to submit review"):
            submit_review(54321, sample_review, "deadbeef")


class TestWaitForDecision:
    async def test_returns_decision_from_sse(
        self,
        live_daemon: tuple[DaemonServer, int],
        sample_review: ReviewRequest,
    ) -> None:
        server, port = live_daemon

        # Submit a review via the daemon's queue directly so we have an id.
        diff_hash = compute_diff_hash(sample_review.files)
        review_id, _ = server.queue.submit(sample_review, diff_hash)

        result_holder: dict[str, ReviewDecision] = {}
        error_holder: dict[str, BaseException] = {}

        def run_wait() -> None:
            try:
                result_holder["decision"] = wait_for_decision(port, review_id)
            except BaseException as exc:
                error_holder["error"] = exc

        thread = _run_in_thread(run_wait)

        # Give the SSE handler time to attach itself, then decide.
        await asyncio.sleep(0.2)
        server.queue.decide(
            review_id,
            ReviewDecision(decision=Decision.APPROVE, overall_comment="ok"),
        )

        # Join the thread off the event loop.
        await asyncio.get_event_loop().run_in_executor(None, thread.join, 5.0)
        assert "error" not in error_holder, error_holder.get("error")
        assert "decision" in result_holder
        decision = result_holder["decision"]
        assert decision.decision == Decision.APPROVE
        assert decision.overall_comment == "ok"

    async def test_404_raises_daemon_error(self, live_daemon: tuple[DaemonServer, int]) -> None:
        _, port = live_daemon
        # Unknown review id: daemon returns 404 with a JSON body, urllib raises
        # HTTPError (a URLError subclass). The client must surface DaemonError.
        with pytest.raises(DaemonError):
            await asyncio.get_event_loop().run_in_executor(
                None, wait_for_decision, port, "does-not-exist"
            )

    def test_sse_error_event_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An ``event: error`` SSE frame must surface as DaemonError.

        Mocks urlopen to return a fake response that yields the error frame
        line-by-line. Avoids spinning up a server that emits malformed SSE.
        """
        fake_lines = [b"event: error\n", b'data: {"error": "boom"}\n', b"\n"]

        class FakeResponse:
            def __iter__(self):  # type: ignore[no-untyped-def]
                return iter(fake_lines)

            def close(self) -> None:
                pass

        def fake_urlopen(req, timeout=None):  # type: ignore[no-untyped-def]
            return FakeResponse()

        monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)

        with pytest.raises(DaemonError, match="daemon error"):
            wait_for_decision(8567, "fake-id")


class TestSubmitAndWait:
    async def test_end_to_end(
        self,
        live_daemon: tuple[DaemonServer, int],
        sample_review: ReviewRequest,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        server, port = live_daemon

        # ensure_daemon calls daemon_is_running which checks the PID file. The
        # live_daemon fixture already wrote one; that path is healthy, so
        # ensure_daemon should be a no-op. start_daemon must NOT be invoked.
        monkeypatch.setattr(
            client,
            "start_daemon",
            lambda host, port: pytest.fail("start_daemon should not be called"),
        )

        result_holder: dict[str, tuple[ReviewDecision, str | None]] = {}
        error_holder: dict[str, BaseException] = {}

        def run() -> None:
            try:
                result_holder["pair"] = submit_and_wait("127.0.0.1", port, sample_review)
            except BaseException as exc:
                error_holder["error"] = exc

        thread = _run_in_thread(run)

        # Wait for the review to land in the queue, then decide.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            summaries = server.queue.get_summaries()
            if summaries:
                break
            await asyncio.sleep(0.05)
        summaries = server.queue.get_summaries()
        assert summaries, "review never landed in the queue"
        review_id = summaries[0]["id"]
        server.queue.decide(
            review_id,
            ReviewDecision(decision=Decision.APPROVE, overall_comment="ship"),
        )

        await asyncio.get_event_loop().run_in_executor(None, thread.join, 5.0)
        assert "error" not in error_holder, error_holder.get("error")
        decision, cached_at = result_holder["pair"]
        assert decision.decision == Decision.APPROVE
        assert decision.overall_comment == "ship"
        assert cached_at is None

    async def test_cache_hit_short_circuits(
        self,
        live_daemon: tuple[DaemonServer, int],
        sample_review: ReviewRequest,
    ) -> None:
        _, port = live_daemon
        diff_hash = compute_diff_hash(sample_review.files)
        cache.store_decision(
            diff_hash,
            ReviewDecision(decision=Decision.APPROVE, overall_comment="prior"),
            branch="feature",
            remote="origin",
        )

        decision, cached_at = await asyncio.get_event_loop().run_in_executor(
            None, submit_and_wait, "127.0.0.1", port, sample_review
        )
        assert decision.decision == Decision.APPROVE
        assert decision.overall_comment == "prior"
        assert cached_at is not None
        assert "T" in cached_at


class TestEnsureDaemon:
    def test_already_running_does_not_spawn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(client, "daemon_is_running", lambda port: True)
        spawn = MagicMock()
        monkeypatch.setattr(client, "start_daemon", spawn)
        ensure_daemon("127.0.0.1", 8567)
        spawn.assert_not_called()

    def test_not_running_calls_start_daemon(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(client, "daemon_is_running", lambda port: False)
        monkeypatch.setattr(cache, "read_pid_file", lambda port: None)
        spawn = MagicMock(return_value=True)
        monkeypatch.setattr(client, "start_daemon", spawn)
        ensure_daemon("127.0.0.1", 8567)
        spawn.assert_called_once_with("127.0.0.1", 8567)

    def test_start_daemon_failure_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(client, "daemon_is_running", lambda port: False)
        monkeypatch.setattr(cache, "read_pid_file", lambda port: None)
        monkeypatch.setattr(client, "start_daemon", lambda host, port: False)
        with pytest.raises(DaemonError, match="failed to start daemon"):
            ensure_daemon("127.0.0.1", 8567)

    def test_unhealthy_with_pid_kills_then_spawns(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Simulate: daemon_is_running False (health probe fails), but PID file
        # has a live PID. We expect kill_daemon then start_daemon.
        monkeypatch.setattr(client, "daemon_is_running", lambda port: False)
        monkeypatch.setattr(cache, "read_pid_file", lambda port: 12345)
        kill = MagicMock()
        monkeypatch.setattr(client, "kill_daemon", kill)
        spawn = MagicMock(return_value=True)
        monkeypatch.setattr(client, "start_daemon", spawn)
        ensure_daemon("127.0.0.1", 8567)
        kill.assert_called_once_with(8567)
        spawn.assert_called_once_with("127.0.0.1", 8567)


class TestKillDaemon:
    def test_no_pid_is_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cache, "read_pid_file", lambda port: None)
        kill = MagicMock()
        with patch("os.kill", kill):
            kill_daemon(8567)
        kill.assert_not_called()

    def test_sigterm_sent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # First read_pid_file call returns the PID; subsequent calls (the poll
        # loop) return None to signal the process exited.
        calls = {"n": 0}

        def fake_read(port: int) -> int | None:
            calls["n"] += 1
            return 12345 if calls["n"] == 1 else None

        monkeypatch.setattr(cache, "read_pid_file", fake_read)
        kill = MagicMock()
        with patch("os.kill", kill):
            kill_daemon(8567)
        assert kill.call_args_list[0].args == (12345, 15)  # SIGTERM == 15

    def test_sigkill_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # PID never disappears: kill_daemon must fall back to SIGKILL.
        monkeypatch.setattr(cache, "read_pid_file", lambda port: 12345)

        sent_signals: list[int] = []

        def fake_kill(pid: int, sig: int) -> None:
            sent_signals.append(sig)

        # Make the wait loop terminate quickly by mocking time.monotonic.
        times = iter([0.0, 0.0, 4.0, 4.0])

        def fake_monotonic() -> float:
            return next(times)

        monkeypatch.setattr(client.time, "monotonic", fake_monotonic)
        monkeypatch.setattr(client.time, "sleep", lambda s: None)

        with patch("os.kill", fake_kill):
            kill_daemon(8567)

        # SIGTERM (15) first, then SIGKILL (9).
        assert 15 in sent_signals
        assert 9 in sent_signals

    def test_already_dead_swallowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cache, "read_pid_file", lambda port: 12345)

        def fake_kill(pid: int, sig: int) -> None:
            raise ProcessLookupError

        # Should not raise.
        with patch("os.kill", fake_kill):
            kill_daemon(8567)
