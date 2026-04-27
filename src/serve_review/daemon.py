"""Multi-review daemon: queue, Starlette app, and lifecycle management.

This module implements the long-running daemon process that serves the review
UI to browsers and accepts review submissions from CLI clients. Unlike the
single-shot ``server.ReviewServer``, the daemon holds a queue of concurrent
reviews, replays cached decisions for previously-seen diffs, and pushes events
to subscribed browsers via Server-Sent Events.

The daemon's lifecycle is owned by ``run_daemon``: it claims a per-port PID
file, sweeps stale cached decisions, and runs uvicorn until interrupted. The
PID file is removed via ``atexit`` on clean shutdown.
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import importlib.resources
import json
import os
import sys
import time
import uuid
from typing import TYPE_CHECKING, Any

from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from serve_review import cache
from serve_review.models import (
    CachedDecision,
    Decision,
    ReviewDecision,
    ReviewQueueItem,
    ReviewRequest,
    compute_diff_hash,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from starlette.requests import Request


# Reject POST bodies above this size before reading. Real review submissions
# are well under 1 MiB; the cap exists to bound the cost of a bad client or a
# bug, not to defend against attackers (the daemon is single-user by design).
_MAX_BODY_BYTES = 5 * 1024 * 1024

# SSE keepalive interval. Sent as an SSE comment so neither the browser nor
# the client treats the connection as idle.
_SSE_KEEPALIVE_SECONDS = 20.0

# How often the daemon sweeps stale cache files while running. The 48h TTL
# means most entries expire on their own; this catches accumulation in
# long-running daemons.
_CACHE_SWEEP_SECONDS = 3600.0

# Maximum events buffered per browser SSE subscriber. If a subscriber falls
# this far behind we drop oldest events rather than block the publisher.
_BROWSER_QUEUE_SIZE = 256


def _strip_refs_heads(ref: str) -> str:
    """Strip ``refs/heads/`` prefix from a ref name if present."""
    prefix = "refs/heads/"
    if ref.startswith(prefix):
        return ref[len(prefix) :]
    return ref


async def _read_json_body(request: Request) -> Any:
    """Read and parse a JSON request body with a size cap.

    Returns the parsed value on success, or a Response (4xx) on failure that
    the route handler can return directly.
    """
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            length = int(content_length)
        except ValueError:
            return JSONResponse({"error": "invalid content-length"}, status_code=400)
        if length > _MAX_BODY_BYTES:
            return JSONResponse({"error": "body too large"}, status_code=413)

    raw = await request.body()
    if len(raw) > _MAX_BODY_BYTES:
        return JSONResponse({"error": "body too large"}, status_code=413)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        return JSONResponse({"error": f"invalid JSON: {exc}"}, status_code=400)


class ReviewQueue:
    """In-memory queue of pending and recently-decided reviews.

    All methods run on a single asyncio event loop; no locking is needed beyond
    that. Decided items linger for a few seconds so SSE subscribers can see the
    final state, then ``_reap`` removes them.
    """

    def __init__(self) -> None:
        self._items: dict[str, ReviewQueueItem] = {}
        self._client_events: dict[str, asyncio.Event] = {}
        self._browser_subscribers: list[asyncio.Queue[dict[str, Any]]] = []

    def __len__(self) -> int:
        return len(self._items)

    def submit(self, review: ReviewRequest, diff_hash: str) -> tuple[str, CachedDecision | None]:
        """Add a review to the queue, or replay a cached decision if one exists.

        Returns ``(review_id, cached_decision_or_None)``. When a cache hit
        occurs the review is never enqueued; the caller (HTTP handler) returns
        the cached decision to the client immediately.
        """
        cached = cache.lookup_cached_decision(diff_hash)
        review_id = str(uuid.uuid4())
        if cached is not None:
            return review_id, cached

        item = ReviewQueueItem(
            id=review_id,
            diff_hash=diff_hash,
            review=review,
            status="pending",
            decision=None,
            submitted_at=time.time(),
            decided_at=None,
        )
        self._items[review_id] = item
        self._client_events[review_id] = asyncio.Event()

        self._publish(
            {
                "event": "review_added",
                "id": review_id,
                "summary": self._summary(item),
            }
        )
        return review_id, None

    def decide(self, review_id: str, decision: ReviewDecision) -> bool:
        """Record a decision, persist it to cache, and wake the waiting client.

        Returns False if the review is unknown or already decided.
        """
        item = self._items.get(review_id)
        if item is None or item.status != "pending":
            return False

        item.decision = decision
        item.status = "decided"
        item.decided_at = time.time()

        # Only cache approvals. Replaying a deny on a re-push would block
        # legitimate retries: the user denied a specific push (often for a
        # reason that no longer applies, e.g. confusion about the diff
        # display) but might explicitly want to retry the same content.
        # Approvals are safe to replay; denies are user-hostile to replay.
        if decision.decision == Decision.APPROVE:
            branch = _strip_refs_heads(item.review.push_info.local_ref)
            cache.store_decision(
                item.diff_hash,
                decision,
                branch=branch,
                remote=item.review.push_info.remote_name,
            )

        event = self._client_events.get(review_id)
        if event is not None:
            event.set()

        self._publish(
            {
                "event": "review_decided",
                "id": review_id,
                "decision": decision.to_dict(),
            }
        )

        self._schedule_reap(review_id)
        return True

    def mark_client_disconnected(self, review_id: str) -> None:
        """Mark the SSE client gone. Pending items become orphaned then reap."""
        item = self._items.get(review_id)
        if item is None:
            return
        if item.status != "pending":
            # Already decided; don't downgrade the state.
            return

        item.status = "orphaned"

        self._publish({"event": "review_orphaned", "id": review_id})
        self._schedule_reap(review_id)

    async def _reap(self, review_id: str) -> None:
        """Remove a finalized item from the queue and notify browsers."""
        self._items.pop(review_id, None)
        self._client_events.pop(review_id, None)
        self._publish({"event": "review_removed", "id": review_id})

    def _schedule_reap(self, review_id: str, delay: float = 3.0) -> None:
        """Schedule ``_reap`` to run ``delay`` seconds from now.

        The default 3s matches the ``tabFade`` keyframe duration in
        ``static/style.css``: the tab visually fades while the daemon
        keeps the entry so SSE subscribers can still render the final
        state, then both reach the end together. If you change one,
        update the other.
        """
        loop = asyncio.get_running_loop()
        loop.call_later(delay, lambda: asyncio.create_task(self._reap(review_id)))

    async def wait_for_decision(self, review_id: str) -> ReviewDecision:
        """Wait until ``decide`` is called for ``review_id`` and return the result.

        Raises ``KeyError`` if the review is unknown (already reaped, or never
        existed). Raises ``RuntimeError`` if the event fires without a decision
        being stored, which indicates a programming error.
        """
        if review_id not in self._client_events:
            raise KeyError(review_id)

        item = self._items.get(review_id)
        if item is not None and item.decision is not None:
            # Race: decision arrived before the SSE handler could subscribe.
            return item.decision

        await self._client_events[review_id].wait()

        item = self._items.get(review_id)
        if item is None or item.decision is None:
            raise RuntimeError(f"decision missing for {review_id}")
        return item.decision

    def get_summaries(self) -> list[dict[str, Any]]:
        """Return summaries for the tab bar, sorted by submission time."""
        items = sorted(self._items.values(), key=lambda i: i.submitted_at)
        return [self._summary(item) for item in items]

    def get_review(self, review_id: str) -> ReviewQueueItem | None:
        return self._items.get(review_id)

    def subscribe_browser(self) -> asyncio.Queue[dict[str, Any]]:
        """Register a browser SSE listener and return its event queue."""
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=_BROWSER_QUEUE_SIZE)
        self._browser_subscribers.append(queue)
        return queue

    def unsubscribe_browser(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        """Remove a previously-registered browser listener. Idempotent."""
        with contextlib.suppress(ValueError):
            self._browser_subscribers.remove(q)

    def _publish(self, event: dict[str, Any]) -> None:
        """Fan out an event to every browser subscriber.

        Queues are bounded; if a subscriber falls too far behind we drop the
        oldest event rather than block the publisher. A subscriber that's
        gone away (browser tab closed without clean cancel) gets pruned the
        next time we try to push to its queue and find it full.
        """
        for q in self._browser_subscribers:
            while True:
                try:
                    q.put_nowait(event)
                    break
                except asyncio.QueueFull:
                    with contextlib.suppress(asyncio.QueueEmpty):
                        q.get_nowait()
                    continue

    def _summary(self, item: ReviewQueueItem) -> dict[str, Any]:
        """Build the per-item summary dict used by the tab bar."""
        push = item.review.push_info
        return {
            "id": item.id,
            "branch": _strip_refs_heads(push.local_ref),
            "remote": push.remote_name,
            "commits_count": len(item.review.commits),
            "files_count": len(item.review.files),
            "has_attention_flags": item.review.has_attention_flags,
            "is_force_push": push.is_force_push,
            "status": item.status,
            "submitted_at": item.submitted_at,
        }


class DaemonServer:
    """Owns the Starlette app and the in-memory ``ReviewQueue``."""

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.queue = ReviewQueue()
        self.app = self._build_app()

    def _build_app(self) -> Starlette:
        static_dir = str(importlib.resources.files("serve_review").joinpath("static"))
        routes = [
            Route("/", self._index),
            Route("/api/health", self._health),
            Route("/api/queue", self._list_queue, methods=["GET"]),
            Route("/api/queue", self._submit, methods=["POST"]),
            Route("/api/queue/{id}", self._get_review, methods=["GET"]),
            Route(
                "/api/queue/{id}/decision",
                self._client_decision_sse,
                methods=["GET"],
            ),
            Route(
                "/api/queue/{id}/approve",
                self._approve,
                methods=["POST"],
            ),
            Route("/api/queue/{id}/deny", self._deny, methods=["POST"]),
            Route("/api/events", self._browser_events_sse, methods=["GET"]),
            Mount(
                "/static",
                app=StaticFiles(directory=static_dir),
                name="static",
            ),
        ]

        @contextlib.asynccontextmanager
        async def lifespan(app: Starlette) -> AsyncIterator[None]:
            sweep_task = asyncio.create_task(self._cache_sweep_loop())
            try:
                yield
            finally:
                sweep_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await sweep_task

        return Starlette(routes=routes, lifespan=lifespan)

    async def _cache_sweep_loop(self) -> None:
        """Sweep stale cache entries on a slow cadence. Runs for the daemon's life."""
        while True:
            try:
                await asyncio.sleep(_CACHE_SWEEP_SECONDS)
            except asyncio.CancelledError:
                return
            with contextlib.suppress(Exception):
                cache.cleanup_stale_decisions()

    async def _index(self, request: Request) -> Response:
        static_dir = importlib.resources.files("serve_review").joinpath("static")
        index_path = static_dir.joinpath("index.html")
        html = index_path.read_text(encoding="utf-8")
        return HTMLResponse(html)

    async def _health(self, request: Request) -> Response:
        return JSONResponse(
            {
                "status": "ok",
                "queued": len(self.queue),
                "port": self.port,
            }
        )

    async def _list_queue(self, request: Request) -> Response:
        return JSONResponse(self.queue.get_summaries())

    async def _submit(self, request: Request) -> Response:
        body = await _read_json_body(request)
        if isinstance(body, Response):
            return body
        try:
            review = ReviewRequest.from_dict(body["review"])
        except (KeyError, TypeError, ValueError) as exc:
            return JSONResponse(
                {"error": f"malformed review payload: {exc}"}, status_code=400
            )

        # Recompute the hash server-side. Trusting a client-supplied value
        # would let any reachable client claim a previously-approved hash for
        # an unrelated diff, replaying the cached APPROVE without review.
        diff_hash = compute_diff_hash(review.files)

        review_id, cached = self.queue.submit(review, diff_hash)
        if cached is not None:
            return JSONResponse(
                {
                    "review_id": review_id,
                    "cached": True,
                    "decision": cached.decision.to_dict(),
                    "cached_at": cached.timestamp,
                },
                status_code=200,
            )
        return JSONResponse(
            {"review_id": review_id, "cached": False},
            status_code=201,
        )

    async def _get_review(self, request: Request) -> Response:
        review_id = request.path_params["id"]
        item = self.queue.get_review(review_id)
        if item is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse(item.review.to_dict())

    async def _approve(self, request: Request) -> Response:
        review_id = request.path_params["id"]
        item = self.queue.get_review(review_id)
        if item is None:
            return JSONResponse({"error": "not found"}, status_code=404)

        body = await _read_json_body(request)
        if isinstance(body, Response):
            return body
        try:
            decision = ReviewDecision.from_approve_body(body)
        except (KeyError, TypeError, ValueError) as exc:
            return JSONResponse({"error": f"malformed body: {exc}"}, status_code=400)
        if not self.queue.decide(review_id, decision):
            return JSONResponse({"error": "already decided"}, status_code=409)
        return JSONResponse({"status": "approved"})

    async def _deny(self, request: Request) -> Response:
        review_id = request.path_params["id"]
        item = self.queue.get_review(review_id)
        if item is None:
            return JSONResponse({"error": "not found"}, status_code=404)

        body = await _read_json_body(request)
        if isinstance(body, Response):
            return body
        try:
            decision = ReviewDecision.from_deny_body(body)
        except (KeyError, TypeError, ValueError) as exc:
            return JSONResponse({"error": f"malformed body: {exc}"}, status_code=400)
        if not self.queue.decide(review_id, decision):
            return JSONResponse({"error": "already decided"}, status_code=409)
        return JSONResponse({"status": "denied"})

    async def _client_decision_sse(self, request: Request) -> Response:
        review_id = request.path_params["id"]
        item = self.queue.get_review(review_id)
        if item is None:
            return JSONResponse({"error": "not found"}, status_code=404)

        queue = self.queue

        async def stream() -> AsyncIterator[str]:
            decision_task: asyncio.Task[ReviewDecision] = asyncio.create_task(
                queue.wait_for_decision(review_id)
            )
            try:
                while True:
                    try:
                        decision = await asyncio.wait_for(
                            asyncio.shield(decision_task),
                            timeout=_SSE_KEEPALIVE_SECONDS,
                        )
                    except TimeoutError:
                        # Decision still pending. Emit a keepalive comment so
                        # the client (and any proxy in between) knows the
                        # connection is alive.
                        yield ": keepalive\n\n"
                        continue
                    payload = json.dumps(decision.to_dict())
                    yield f"event: decision\ndata: {payload}\n\n"
                    return
            except asyncio.CancelledError:
                decision_task.cancel()
                queue.mark_client_disconnected(review_id)
                raise
            except KeyError:
                yield 'event: error\ndata: {"error": "review reaped"}\n\n'

        return StreamingResponse(stream(), media_type="text/event-stream")

    async def _browser_events_sse(self, request: Request) -> Response:
        sub = self.queue.subscribe_browser()
        queue = self.queue

        async def stream() -> AsyncIterator[str]:
            try:
                yield ": connected\n\n"
                while True:
                    # Wait up to the keepalive interval for a real event;
                    # if nothing arrives, send a comment line so any proxy
                    # in the path doesn't treat the connection as idle.
                    try:
                        event = await asyncio.wait_for(
                            sub.get(), timeout=_SSE_KEEPALIVE_SECONDS
                        )
                    except TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    # Read non-destructively: ``_publish`` fans out the same
                    # dict reference to every subscriber, so popping here
                    # would corrupt later subscribers' view of the event.
                    event_name = event["event"]
                    data = {k: v for k, v in event.items() if k != "event"}
                    yield f"event: {event_name}\ndata: {json.dumps(data)}\n\n"
            finally:
                # try/finally so any exit path (CancelledError, server
                # shutdown, generator close) reliably unsubscribes.
                queue.unsubscribe_browser(sub)

        return StreamingResponse(stream(), media_type="text/event-stream")


_LOG_SIZE_CAP_BYTES = 10 * 1024 * 1024


def _rotate_log_if_oversized(path: Path) -> None:
    """Truncate the daemon log if it has grown past the size cap.

    Run once on daemon startup. Bounds disk use to roughly twice the cap
    over time (the daemon can grow it again during one run). For continuous
    rotation while running, an external tool like ``logrotate`` with
    ``copytruncate`` is the right answer.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return
    if size <= _LOG_SIZE_CAP_BYTES:
        return
    with contextlib.suppress(OSError), path.open("ab") as f:
        f.truncate(0)


def run_daemon(host: str, port: int) -> None:
    """Start the daemon process. Blocks until uvicorn exits.

    Refuses to start if a live PID file already claims this port. On clean
    shutdown the PID file is removed via ``atexit``; uvicorn handles SIGINT
    and SIGTERM itself.
    """
    import uvicorn

    existing = cache.read_pid_file(port)
    if existing is not None:
        print(
            f"daemon already running on port {port} (pid {existing})",
            file=sys.stderr,
        )
        sys.exit(1)

    _rotate_log_if_oversized(cache.log_file())
    cache.cleanup_stale_decisions()
    cache.write_pid_file(port, os.getpid())
    atexit.register(cache.remove_pid_file, port)

    server = DaemonServer(host, port)
    config = uvicorn.Config(server.app, host=host, port=port, log_level="warning")
    uvi = uvicorn.Server(config)
    asyncio.run(uvi.serve())


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(prog="python -m serve_review.daemon")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8567)
    args = parser.parse_args()
    run_daemon(host=args.host, port=args.port)
