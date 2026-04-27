"""Shared test fixtures."""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
from typing import TYPE_CHECKING

import pytest
import uvicorn

from serve_review import cache
from serve_review.daemon import DaemonServer
from serve_review.models import (
    AttentionFlag,
    AttentionKind,
    CommitInfo,
    DiffHunk,
    DiffLine,
    FileDiff,
    PushInfo,
    ReviewRequest,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@pytest.fixture
def sample_push_info() -> PushInfo:
    return PushInfo(
        local_ref="refs/heads/feature",
        local_sha="a" * 40,
        remote_ref="refs/heads/feature",
        remote_sha="b" * 40,
        remote_name="origin",
        remote_url="git@github.com:user/repo.git",
        is_force_push=True,
    )


@pytest.fixture
def sample_review(sample_push_info: PushInfo) -> ReviewRequest:
    return ReviewRequest(
        push_info=sample_push_info,
        commits=[
            CommitInfo(
                sha="a" * 40,
                message="Add new feature",
                author="Test User",
                date="2024-01-15 10:30:00 +0000",
                body="This adds a new header file.\n\nSigned-off-by: Test User",
                files=["lib/header.h"],
            ),
        ],
        files=[
            FileDiff(
                old_path="a/lib/header.h",
                new_path="b/lib/header.h",
                is_new=True,
                is_deleted=False,
                is_rename=False,
                language="c",
                hunks=[
                    DiffHunk(
                        header="@@ -0,0 +1,5 @@",
                        lines=[
                            DiffLine(
                                line_type="+",
                                content="// Copyright (c) 2024 Fake Name <fake@example.com>",
                                old_line_no=None,
                                new_line_no=1,
                                flags=[
                                    AttentionFlag(
                                        kind=AttentionKind.COPYRIGHT,
                                        start=3,
                                        end=50,
                                        text="Copyright (c) 2024 Fake Name <fake@example.com>",
                                    ),
                                    AttentionFlag(
                                        kind=AttentionKind.EMAIL,
                                        start=34,
                                        end=50,
                                        text="fake@example.com",
                                    ),
                                ],
                            ),
                            DiffLine(
                                line_type="+",
                                content="#ifndef HEADER_H",
                                old_line_no=None,
                                new_line_no=2,
                                flags=[],
                            ),
                            DiffLine(
                                line_type="+",
                                content="#define HEADER_H",
                                old_line_no=None,
                                new_line_no=3,
                                flags=[],
                            ),
                            DiffLine(
                                line_type="+",
                                content="",
                                old_line_no=None,
                                new_line_no=4,
                                flags=[],
                            ),
                            DiffLine(
                                line_type="+",
                                content="#endif",
                                old_line_no=None,
                                new_line_no=5,
                                flags=[],
                            ),
                        ],
                    ),
                ],
            ),
        ],
        has_attention_flags=True,
    )


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


@pytest.fixture
async def live_daemon() -> AsyncIterator[tuple[DaemonServer, int]]:
    """Run a real uvicorn server in-process. Yields (server, port).

    The client speaks HTTP over a real loopback socket, so an ASGI transport
    won't do; we need a port the urllib client can connect to.
    """
    port = _free_port()
    server = DaemonServer(host="127.0.0.1", port=port)
    config = uvicorn.Config(server.app, host="127.0.0.1", port=port, log_level="error")
    uvi = uvicorn.Server(config)
    serve_task = asyncio.create_task(uvi.serve())

    deadline = asyncio.get_event_loop().time() + 5.0
    while not uvi.started:
        if asyncio.get_event_loop().time() > deadline:
            raise RuntimeError("uvicorn failed to start")
        await asyncio.sleep(0.01)

    # Mirror the real daemon's lifecycle: write a PID file so daemon_is_running
    # can find us. Use the test process's own PID, which is guaranteed alive
    # for the test duration and works in containers without an init at PID 1.
    cache.write_pid_file(port, os.getpid())

    try:
        yield server, port
    finally:
        cache.remove_pid_file(port)
        uvi.should_exit = True
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(serve_task, timeout=5.0)
