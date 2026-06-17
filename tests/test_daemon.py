"""Tests for the multi-review daemon server."""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
from typing import TYPE_CHECKING

import pytest
import uvicorn

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

from httpx import ASGITransport, AsyncClient

from serve_review import cache
from serve_review.daemon import DaemonServer
from serve_review.models import (
    Decision,
    ReviewDecision,
    ReviewRequest,
    compute_diff_hash,
)


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect CACHE_DIR to a per-test tmp dir so tests can't see each other."""
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def daemon() -> DaemonServer:
    return DaemonServer(host="127.0.0.1", port=8567)


@pytest.fixture
async def client(daemon: DaemonServer) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=daemon.app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _diff_hash(review: ReviewRequest) -> str:
    return compute_diff_hash(review.files)


async def _submit(client: AsyncClient, review: ReviewRequest) -> dict:
    resp = await client.post(
        "/api/queue",
        json={"review": review.to_dict(), "diff_hash": _diff_hash(review)},
    )
    return {"status": resp.status_code, "body": resp.json()}


async def _wait_for_subscribers(daemon: DaemonServer, count: int, timeout: float = 2.0) -> None:
    """Wait until the daemon has at least ``count`` browser SSE subscribers.

    Avoids racing the publisher against an SSE handler that hasn't yet
    registered itself.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while len(daemon.queue._browser_subscribers) < count:
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError(
                f"only {len(daemon.queue._browser_subscribers)} subscribers after {timeout}s"
            )
        await asyncio.sleep(0.01)


def _free_port() -> int:
    """Pick an unused TCP port for binding a real uvicorn server."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


@pytest.fixture
async def live_daemon() -> AsyncIterator[tuple[DaemonServer, str]]:
    """Run a real uvicorn server in-process so SSE responses actually stream.

    httpx's ASGITransport buffers the entire response body before returning,
    which makes it unusable for testing Server-Sent Events. A real loopback
    server is the only way to exercise the streaming path end-to-end.
    """
    port = _free_port()
    server = DaemonServer(host="127.0.0.1", port=port)
    config = uvicorn.Config(server.app, host="127.0.0.1", port=port, log_level="error")
    uvi = uvicorn.Server(config)
    serve_task = asyncio.create_task(uvi.serve())

    # Wait for the server to be ready to accept connections.
    deadline = asyncio.get_event_loop().time() + 5.0
    while not uvi.started:
        if asyncio.get_event_loop().time() > deadline:
            raise RuntimeError("uvicorn failed to start")
        await asyncio.sleep(0.01)

    base_url = f"http://127.0.0.1:{port}"
    try:
        yield server, base_url
    finally:
        uvi.should_exit = True
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(serve_task, timeout=5.0)


async def _http_submit(base_url: str, review: ReviewRequest) -> dict:
    """Submit a review via a real HTTP client (used by SSE tests)."""
    async with AsyncClient(base_url=base_url) as c:
        resp = await c.post(
            "/api/queue",
            json={"review": review.to_dict(), "diff_hash": _diff_hash(review)},
        )
        return {"status": resp.status_code, "body": resp.json()}


class TestSubmit:
    async def test_fresh_submit_returns_201(
        self, client: AsyncClient, sample_review: ReviewRequest
    ) -> None:
        result = await _submit(client, sample_review)
        assert result["status"] == 201
        assert result["body"]["cached"] is False
        assert "review_id" in result["body"]

    async def test_cache_hit_returns_200_with_decision(
        self, client: AsyncClient, sample_review: ReviewRequest
    ) -> None:
        # Pre-populate the cache for this exact diff.
        diff_hash = _diff_hash(sample_review)
        cache.store_decision(
            diff_hash,
            ReviewDecision(decision=Decision.APPROVE, overall_comment="cached ok"),
            branch="feature",
            remote="origin",
        )

        result = await _submit(client, sample_review)
        assert result["status"] == 200
        assert result["body"]["cached"] is True
        assert result["body"]["decision"]["decision"] == "approve"
        assert result["body"]["decision"]["overall_comment"] == "cached ok"

    async def test_cache_hit_includes_cached_at(
        self, client: AsyncClient, sample_review: ReviewRequest
    ) -> None:
        diff_hash = _diff_hash(sample_review)
        cache.store_decision(
            diff_hash,
            ReviewDecision(decision=Decision.APPROVE),
        )

        result = await _submit(client, sample_review)
        assert result["status"] == 200
        assert "cached_at" in result["body"]
        # ISO 8601 timestamp; just sanity check the shape.
        assert "T" in result["body"]["cached_at"]


class TestQueue:
    async def test_empty_queue_returns_empty_list(self, client: AsyncClient) -> None:
        resp = await client.get("/api/queue")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_lists_submitted_reviews(
        self, client: AsyncClient, sample_review: ReviewRequest
    ) -> None:
        await _submit(client, sample_review)
        resp = await client.get("/api/queue")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        item = data[0]
        assert item["branch"] == "feature"
        assert item["remote"] == "origin"
        assert item["commits_count"] == 1
        assert item["files_count"] == 1
        assert item["has_attention_flags"] is True
        assert item["is_force_push"] is True
        assert item["status"] == "pending"

    async def test_get_review_returns_full_data(
        self, client: AsyncClient, sample_review: ReviewRequest
    ) -> None:
        submit = await _submit(client, sample_review)
        review_id = submit["body"]["review_id"]

        resp = await client.get(f"/api/queue/{review_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["push_info"]["is_force_push"] is True
        assert len(data["commits"]) == 1
        assert len(data["files"]) == 1
        assert data["has_attention_flags"] is True

    async def test_get_review_nonexistent_returns_404(self, client: AsyncClient) -> None:
        resp = await client.get("/api/queue/does-not-exist")
        assert resp.status_code == 404


class TestDecide:
    async def test_approve_persists_to_cache(
        self,
        client: AsyncClient,
        sample_review: ReviewRequest,
    ) -> None:
        submit = await _submit(client, sample_review)
        review_id = submit["body"]["review_id"]

        resp = await client.post(
            f"/api/queue/{review_id}/approve",
            json={"overall_comment": "ship it"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "approved"

        cached = cache.lookup_cached_decision(_diff_hash(sample_review))
        assert cached is not None
        assert cached.decision.decision == Decision.APPROVE
        assert cached.decision.overall_comment == "ship it"
        assert cached.branch == "feature"
        assert cached.remote == "origin"

    async def test_deny_with_comments(
        self, client: AsyncClient, sample_review: ReviewRequest
    ) -> None:
        submit = await _submit(client, sample_review)
        review_id = submit["body"]["review_id"]

        resp = await client.post(
            f"/api/queue/{review_id}/deny",
            json={
                "overall_comment": "needs work",
                "comments": [{"body": "fix this", "file": "lib/header.h", "line": 1}],
            },
        )
        assert resp.status_code == 200

    async def test_deny_does_not_persist_to_cache(
        self, client: AsyncClient, sample_review: ReviewRequest
    ) -> None:
        # Replaying a cached deny would block legitimate retries of the same
        # diff, so denies must never reach the cache.
        submit = await _submit(client, sample_review)
        review_id = submit["body"]["review_id"]

        resp = await client.post(
            f"/api/queue/{review_id}/deny",
            json={"overall_comment": "Stale", "comments": []},
        )
        assert resp.status_code == 200
        assert cache.lookup_cached_decision(_diff_hash(sample_review)) is None

    async def test_approve_nonexistent_returns_404(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/queue/nope/approve",
            json={"overall_comment": ""},
        )
        assert resp.status_code == 404

    async def test_double_decide_returns_409(
        self, client: AsyncClient, sample_review: ReviewRequest
    ) -> None:
        submit = await _submit(client, sample_review)
        review_id = submit["body"]["review_id"]

        first = await client.post(
            f"/api/queue/{review_id}/approve",
            json={"overall_comment": "ok"},
        )
        assert first.status_code == 200

        second = await client.post(
            f"/api/queue/{review_id}/approve",
            json={"overall_comment": "again"},
        )
        assert second.status_code == 409


class TestSSE:
    """SSE tests run against a real uvicorn server.

    httpx's ASGITransport buffers responses in full and never yields streamed
    bytes (it waits for the final ``more_body=False``), so a real loopback
    server is the only way to exercise the streaming path end-to-end.
    """

    async def test_client_sse_receives_decision(
        self,
        live_daemon: tuple[DaemonServer, str],
        sample_review: ReviewRequest,
    ) -> None:
        _, base_url = live_daemon
        submit = await _http_submit(base_url, sample_review)
        review_id = submit["body"]["review_id"]

        async def receive_decision() -> dict:
            async with (
                AsyncClient(base_url=base_url, timeout=10.0) as c,
                c.stream("GET", f"/api/queue/{review_id}/decision") as resp,
            ):
                async for line in resp.aiter_lines():
                    if line.startswith("data: "):
                        return json.loads(line[len("data: ") :])
            raise AssertionError("stream ended without data line")

        receiver = asyncio.create_task(receive_decision())
        # Give the SSE handler a beat to attach itself.
        await asyncio.sleep(0.1)

        async with AsyncClient(base_url=base_url) as c:
            resp = await c.post(
                f"/api/queue/{review_id}/approve",
                json={"overall_comment": "yes"},
            )
        assert resp.status_code == 200

        decision_dict = await asyncio.wait_for(receiver, timeout=5.0)
        assert decision_dict["decision"] == "approve"
        assert decision_dict["overall_comment"] == "yes"

    async def test_browser_sse_receives_review_added(
        self,
        live_daemon: tuple[DaemonServer, str],
        sample_review: ReviewRequest,
    ) -> None:
        daemon, base_url = live_daemon

        async def receive_first_event() -> tuple[str, dict]:
            event_name: str | None = None
            async with (
                AsyncClient(base_url=base_url, timeout=10.0) as c,
                c.stream("GET", "/api/events") as resp,
            ):
                async for line in resp.aiter_lines():
                    if line.startswith("event: "):
                        event_name = line[len("event: ") :].strip()
                    elif line.startswith("data: ") and event_name is not None:
                        return event_name, json.loads(line[len("data: ") :])
            raise AssertionError("stream ended without data line")

        receiver = asyncio.create_task(receive_first_event())
        await _wait_for_subscribers(daemon, 1)
        await _http_submit(base_url, sample_review)

        name, payload = await asyncio.wait_for(receiver, timeout=5.0)
        assert name == "review_added"
        assert "id" in payload
        assert payload["summary"]["branch"] == "feature"

    async def test_browser_sse_receives_decided_then_removed(
        self,
        live_daemon: tuple[DaemonServer, str],
        sample_review: ReviewRequest,
    ) -> None:
        daemon, base_url = live_daemon

        async def collect_events(target_count: int) -> list[tuple[str, dict]]:
            events: list[tuple[str, dict]] = []
            event_name: str | None = None
            async with (
                AsyncClient(base_url=base_url, timeout=15.0) as c,
                c.stream("GET", "/api/events") as resp,
            ):
                async for line in resp.aiter_lines():
                    if line.startswith("event: "):
                        event_name = line[len("event: ") :].strip()
                    elif line.startswith("data: ") and event_name is not None:
                        events.append((event_name, json.loads(line[len("data: ") :])))
                        event_name = None
                        if len(events) >= target_count:
                            return events
            return events

        # We expect: review_added, review_decided, review_removed.
        receiver = asyncio.create_task(collect_events(3))
        await _wait_for_subscribers(daemon, 1)

        submit = await _http_submit(base_url, sample_review)
        review_id = submit["body"]["review_id"]
        async with AsyncClient(base_url=base_url) as c:
            await c.post(
                f"/api/queue/{review_id}/approve",
                json={"overall_comment": "ok"},
            )

        # ``decide`` schedules reap 3s later via call_later, so wait a bit
        # longer than that for the third event.
        events = await asyncio.wait_for(receiver, timeout=8.0)
        names = [name for name, _ in events]
        assert names == ["review_added", "review_decided", "review_removed"]
        assert events[1][1]["decision"]["decision"] == "approve"
        assert events[2][1]["id"] == review_id


class TestHealth:
    async def test_health_returns_status(self, client: AsyncClient) -> None:
        resp = await client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["port"] == 8567
        assert data["queued"] == 0

    async def test_health_includes_scheme(self, client: AsyncClient) -> None:
        resp = await client.get("/api/health")
        data = resp.json()
        assert data["scheme"] in ("http", "https")

    async def test_health_reflects_queued_count(
        self, client: AsyncClient, sample_review: ReviewRequest
    ) -> None:
        await _submit(client, sample_review)
        resp = await client.get("/api/health")
        assert resp.json()["queued"] == 1


class TestRunDaemonHelpers:
    """Tests for helpers in run_daemon that we can exercise without uvicorn."""

    def test_serve_review_logging_is_idempotent(self) -> None:
        import logging

        from serve_review.daemon import _configure_serve_review_logging

        pkg_logger = logging.getLogger("serve_review")
        before = len(pkg_logger.handlers)
        _configure_serve_review_logging()
        _configure_serve_review_logging()
        after = len(pkg_logger.handlers)
        # Should add at most one handler regardless of how many times called.
        assert after - before <= 1


class TestDaemonServerScheme:
    """DaemonServer surfaces the configured scheme via /api/health."""

    async def test_https_scheme_in_health(self) -> None:
        from httpx import ASGITransport
        from httpx import AsyncClient as _AC

        from serve_review.daemon import DaemonServer as _DS

        server = _DS(host="127.0.0.1", port=8567, scheme="https")
        transport = ASGITransport(app=server.app)
        async with _AC(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/health")
            assert resp.json()["scheme"] == "https"


class TestCacheReplayDefense:
    """The server must recompute diff_hash from the submitted review.

    A client that supplies a hash matching a previously-approved diff but
    submits unrelated content must not get the cached APPROVE replayed.
    """

    async def test_client_supplied_hash_is_ignored(
        self, client: AsyncClient, sample_review: ReviewRequest
    ) -> None:
        # Pre-populate cache under an unrelated, attacker-controlled hash.
        attacker_hash = "f" * 64
        cache.store_decision(
            attacker_hash,
            ReviewDecision(decision=Decision.APPROVE, overall_comment="forged"),
        )

        # Submit a fresh review claiming the cached hash. Server must
        # ignore the supplied hash and queue the review for real review.
        resp = await client.post(
            "/api/queue",
            json={"review": sample_review.to_dict(), "diff_hash": attacker_hash},
        )
        assert resp.status_code == 201
        assert resp.json()["cached"] is False


class TestBodyValidation:
    async def test_submit_missing_review_returns_400(self, client: AsyncClient) -> None:
        resp = await client.post("/api/queue", json={})
        assert resp.status_code == 400
        assert "error" in resp.json()

    async def test_submit_invalid_json_returns_400(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/queue",
            content=b"not json",
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 400

    async def test_oversized_body_returns_413(self, client: AsyncClient) -> None:
        # Lie about the size to skip actually allocating 5+ MiB in the test.
        resp = await client.post(
            "/api/queue",
            content=b"{}",
            headers={
                "content-type": "application/json",
                "content-length": str(10 * 1024 * 1024),
            },
        )
        assert resp.status_code == 413

    async def test_approve_invalid_json_returns_400(
        self, client: AsyncClient, sample_review: ReviewRequest
    ) -> None:
        result = await _submit(client, sample_review)
        review_id = result["body"]["review_id"]
        resp = await client.post(
            f"/api/queue/{review_id}/approve",
            content=b"not json",
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 400


class TestOrphan:
    async def test_disconnect_marks_orphaned_and_reaps(
        self, daemon: DaemonServer, sample_review: ReviewRequest
    ) -> None:
        """Cancelling the client SSE stream transitions to orphaned, then reaps."""
        review_id, _ = daemon.queue.submit(sample_review, _diff_hash(sample_review))

        # Patch reap delay so the test runs in <1s.
        original_reap_delay = 3.0
        try:
            from serve_review import daemon as daemon_module

            daemon_module._REAP_DELAY = 0.05  # type: ignore[attr-defined]
        except AttributeError:
            pass

        # Simulate the SSE handler's CancelledError path.
        daemon.queue.mark_client_disconnected(review_id)
        item = daemon.queue.get_review(review_id)
        assert item is not None
        assert item.status == "orphaned"

        # Wait for the scheduled reap (3s default in this branch). Use a
        # short polling loop with generous deadline since we can't easily
        # patch the reap timer post-hoc.
        for _ in range(40):
            if daemon.queue.get_review(review_id) is None:
                break
            await asyncio.sleep(0.1)
        assert daemon.queue.get_review(review_id) is None, (
            f"review {review_id} not reaped after orphan; original delay {original_reap_delay}s"
        )


class TestConcurrentReviews:
    async def test_two_concurrent_reviews_decide_independently(
        self,
        daemon: DaemonServer,
        sample_review: ReviewRequest,
    ) -> None:
        """Two reviews submitted simultaneously each get their own decision."""
        # Submit two distinct reviews. We mutate sample_review's files to
        # produce two different diff hashes so the second one isn't a cache
        # hit on the first.
        from dataclasses import replace

        review_a = sample_review
        review_b = replace(sample_review, has_attention_flags=False)
        # Different content => different hash. Modify a line.
        from serve_review.models import DiffHunk, FileDiff

        first_file = review_b.files[0]
        modified_lines = [
            replace(line, content=line.content + " // b") for line in first_file.hunks[0].lines
        ]
        new_hunk = DiffHunk(header=first_file.hunks[0].header, lines=modified_lines)
        review_b = replace(
            review_b,
            files=[
                FileDiff(
                    old_path=first_file.old_path,
                    new_path=first_file.new_path,
                    is_new=first_file.is_new,
                    is_deleted=first_file.is_deleted,
                    is_rename=first_file.is_rename,
                    language=first_file.language,
                    hunks=[new_hunk],
                )
            ],
        )

        rid_a, _ = daemon.queue.submit(review_a, _diff_hash(review_a))
        rid_b, _ = daemon.queue.submit(review_b, _diff_hash(review_b))
        assert rid_a != rid_b
        assert daemon.queue.get_review(rid_a) is not None
        assert daemon.queue.get_review(rid_b) is not None

        # Decide them in interleaved order.
        wait_a = asyncio.create_task(daemon.queue.wait_for_decision(rid_a))
        wait_b = asyncio.create_task(daemon.queue.wait_for_decision(rid_b))
        await asyncio.sleep(0.01)
        assert daemon.queue.decide(
            rid_b, ReviewDecision(decision=Decision.DENY, overall_comment="b")
        )
        assert daemon.queue.decide(
            rid_a, ReviewDecision(decision=Decision.APPROVE, overall_comment="a")
        )

        decision_a = await asyncio.wait_for(wait_a, timeout=1.0)
        decision_b = await asyncio.wait_for(wait_b, timeout=1.0)
        assert decision_a.decision == Decision.APPROVE
        assert decision_a.overall_comment == "a"
        assert decision_b.decision == Decision.DENY
        assert decision_b.overall_comment == "b"


class TestCancel:
    async def test_cancel_marks_orphaned_and_reaps(
        self, daemon: DaemonServer, client: AsyncClient, sample_review: ReviewRequest
    ) -> None:
        review_id, _ = daemon.queue.submit(sample_review, _diff_hash(sample_review))

        resp = await client.post(f"/api/queue/{review_id}/cancel")
        assert resp.status_code == 200
        assert resp.json() == {"status": "cancelled"}

        item = daemon.queue.get_review(review_id)
        assert item is not None
        assert item.status == "orphaned"

        for _ in range(40):
            if daemon.queue.get_review(review_id) is None:
                break
            await asyncio.sleep(0.1)
        assert daemon.queue.get_review(review_id) is None

    async def test_cancel_nonexistent_returns_409(
        self, client: AsyncClient
    ) -> None:
        resp = await client.post("/api/queue/no-such-id/cancel")
        assert resp.status_code == 409

    async def test_cancel_already_decided_returns_409(
        self, daemon: DaemonServer, client: AsyncClient, sample_review: ReviewRequest
    ) -> None:
        review_id, _ = daemon.queue.submit(sample_review, _diff_hash(sample_review))
        daemon.queue.decide(review_id, ReviewDecision(decision=Decision.APPROVE))
        resp = await client.post(f"/api/queue/{review_id}/cancel")
        assert resp.status_code == 409

    async def test_cancel_wakes_wait_for_decision(
        self, daemon: DaemonServer, sample_review: ReviewRequest
    ) -> None:
        review_id, _ = daemon.queue.submit(sample_review, _diff_hash(sample_review))
        wait_task = asyncio.create_task(daemon.queue.wait_for_decision(review_id))
        await asyncio.sleep(0.01)
        daemon.queue.cancel(review_id)
        with pytest.raises(RuntimeError):
            await asyncio.wait_for(wait_task, timeout=1.0)
