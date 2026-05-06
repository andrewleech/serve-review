"""Async web server for the review UI."""

from __future__ import annotations

import asyncio
import importlib.resources
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from starlette.requests import Request

from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from serve_review.models import (
    Decision,
    ReviewComment,
    ReviewDecision,
    ReviewRequest,
)


class ReviewServer:
    """Serves the review UI and blocks until a decision is made."""

    def __init__(
        self,
        review: ReviewRequest,
        refresh_fn: Callable[[], ReviewRequest] | None = None,
    ) -> None:
        self.review = review
        self._refresh_fn = refresh_fn
        self.decision: ReviewDecision | None = None
        self._decided = asyncio.Event()
        self.app = self._build_app()

    def _build_app(self) -> Starlette:
        routes = [
            Route("/", self._index),
            Route("/api/review", self._get_review),
            Route("/api/review/approve", self._approve, methods=["POST"]),
            Route("/api/review/deny", self._deny, methods=["POST"]),
            Mount(
                "/static",
                app=StaticFiles(
                    directory=str(importlib.resources.files("serve_review").joinpath("static"))
                ),
                name="static",
            ),
        ]
        return Starlette(routes=routes)

    async def _index(self, request: Request) -> Response:
        static_dir = importlib.resources.files("serve_review").joinpath("static")
        index_path = static_dir.joinpath("index.html")
        html = index_path.read_text(encoding="utf-8")
        return HTMLResponse(html)

    async def _get_review(self, request: Request) -> Response:
        if self._refresh_fn is not None:
            import contextlib

            with contextlib.suppress(Exception):
                self.review = self._refresh_fn()
        return JSONResponse(self.review.to_dict())

    async def _approve(self, request: Request) -> Response:
        if self.decision is not None:
            return JSONResponse({"error": "Decision already made"}, status_code=409)

        body = await request.json()
        overall = body.get("overall_comment", "")

        self.decision = ReviewDecision(
            decision=Decision.APPROVE,
            overall_comment=overall,
        )
        self._decided.set()
        return JSONResponse({"status": "approved"})

    async def _deny(self, request: Request) -> Response:
        if self.decision is not None:
            return JSONResponse({"error": "Decision already made"}, status_code=409)

        body = await request.json()
        comments = [
            ReviewComment(
                body=c.get("body", ""),
                file=c.get("file"),
                line=c.get("line"),
            )
            for c in body.get("comments", [])
        ]
        overall = body.get("overall_comment", "")

        self.decision = ReviewDecision(
            decision=Decision.DENY,
            comments=comments,
            overall_comment=overall,
        )
        self._decided.set()
        return JSONResponse({"status": "denied"})

    async def wait_for_decision(self) -> ReviewDecision:
        """Block until the reviewer makes a decision."""
        await self._decided.wait()
        assert self.decision is not None
        return self.decision


async def run_server(
    review: ReviewRequest,
    port: int = 8567,
    host: str = "127.0.0.1",
    refresh_fn: Callable[[], ReviewRequest] | None = None,
) -> ReviewDecision:
    """Start the review server, wait for a decision, then shut down."""
    import socket

    import uvicorn

    # Check port availability before starting uvicorn (clean error instead of traceback)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
    except OSError:
        owner = _identify_port_owner(port)
        msg = f"serve-review: port {port} is already in use"
        if owner:
            msg += f" by {owner}"
        msg += f"\n  Kill it:  fuser -k {port}/tcp\n  Or use:   serve-review --port <other>"
        raise OSError(msg) from None
    finally:
        sock.close()

    server = ReviewServer(review, refresh_fn=refresh_fn)
    config = uvicorn.Config(
        server.app,
        host=host,
        port=port,
        log_level="warning",
    )
    uvi = uvicorn.Server(config)

    # Run server and wait for decision concurrently
    server_task = asyncio.create_task(uvi.serve())
    decision = await server.wait_for_decision()

    # Give a moment for the response to be sent, then shut down
    await asyncio.sleep(0.5)
    uvi.should_exit = True
    await server_task

    return decision


def format_decision_json(decision: ReviewDecision) -> str:
    """Format the decision as JSON for stdout (machine-readable)."""
    return json.dumps(decision.to_dict(), indent=2)


def format_decision_human(decision: ReviewDecision) -> str:
    """Format the decision as human-readable text for stderr."""
    lines: list[str] = []

    if decision.decision == Decision.APPROVE:
        lines.append("APPROVED")
    else:
        lines.append("CHANGES REQUESTED")

    if decision.overall_comment:
        lines.append(f"\n{decision.overall_comment}")

    for comment in decision.comments:
        loc = ""
        if comment.file:
            loc = comment.file
            if comment.line is not None:
                loc += f":{comment.line}"
            loc += " - "
        lines.append(f"\n  {loc}{comment.body}")

    return "\n".join(lines)


def _identify_port_owner(port: int) -> str:
    """Try to identify the process using a port. Returns a description or empty string."""
    import subprocess

    # Try ss first (most Linux systems)
    for cmd in (
        ["ss", "-tlnp", f"sport = :{port}"],
        ["fuser", f"{port}/tcp"],
    ):
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
            if result.returncode == 0 and result.stdout.strip():
                # ss output contains pid and process name
                out = result.stdout.strip()
                # Extract pid from ss output like: users:(("serve-review",pid=12345,fd=6))
                import re

                m = re.search(r'"([^"]+)",pid=(\d+)', out)
                if m:
                    return f"pid {m.group(2)} ({m.group(1)})"
                # Extract pid from fuser output like: 12345
                m = re.search(r"(\d+)", out)
                if m:
                    pid = m.group(1)
                    # Try to get the command name
                    try:
                        ps = subprocess.run(
                            ["ps", "-p", pid, "-o", "comm="],
                            capture_output=True,
                            text=True,
                            timeout=2,
                        )
                        name = ps.stdout.strip()
                        if name:
                            return f"pid {pid} ({name})"
                    except (subprocess.TimeoutExpired, FileNotFoundError):
                        pass
                    return f"pid {pid}"
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue

    return ""
