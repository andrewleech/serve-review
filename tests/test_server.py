"""Tests for the review server."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
from httpx import ASGITransport, AsyncClient

from serve_review.models import Decision, ReviewRequest
from serve_review.server import ReviewServer, format_decision_human, format_decision_json


@pytest.fixture
def server(sample_review: ReviewRequest) -> ReviewServer:
    return ReviewServer(sample_review)


@pytest.fixture
async def client(server: ReviewServer) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=server.app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class TestReviewEndpoint:
    async def test_get_review(self, client: AsyncClient) -> None:
        resp = await client.get("/api/review")
        assert resp.status_code == 200
        data = resp.json()
        assert data["push_info"]["is_force_push"] is True
        assert len(data["commits"]) == 1
        assert len(data["files"]) == 1
        assert data["has_attention_flags"] is True

    async def test_review_contains_flags(self, client: AsyncClient) -> None:
        resp = await client.get("/api/review")
        data = resp.json()
        first_line = data["files"][0]["hunks"][0]["lines"][0]
        assert len(first_line["flags"]) > 0
        assert first_line["flags"][0]["kind"] == "copyright"


class TestApprove:
    async def test_approve(self, client: AsyncClient, server: ReviewServer) -> None:
        resp = await client.post(
            "/api/review/approve",
            json={"overall_comment": "Looks good"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "approved"

        decision = await asyncio.wait_for(server.wait_for_decision(), timeout=1.0)
        assert decision.decision == Decision.APPROVE
        assert decision.overall_comment == "Looks good"

    async def test_double_submit_rejected(self, client: AsyncClient, server: ReviewServer) -> None:
        await client.post("/api/review/approve", json={})
        resp = await client.post("/api/review/approve", json={})
        assert resp.status_code == 409


class TestDeny:
    async def test_deny_with_comments(self, client: AsyncClient, server: ReviewServer) -> None:
        resp = await client.post(
            "/api/review/deny",
            json={
                "overall_comment": "Fix copyright",
                "comments": [
                    {"body": "Wrong name here", "file": "header.h", "line": 1},
                ],
            },
        )
        assert resp.status_code == 200

        decision = await asyncio.wait_for(server.wait_for_decision(), timeout=1.0)
        assert decision.decision == Decision.DENY
        assert len(decision.comments) == 1
        assert decision.comments[0].body == "Wrong name here"

    async def test_deny_empty_comments(self, client: AsyncClient, server: ReviewServer) -> None:
        resp = await client.post(
            "/api/review/deny",
            json={"overall_comment": "No good", "comments": []},
        )
        assert resp.status_code == 200
        decision = await asyncio.wait_for(server.wait_for_decision(), timeout=1.0)
        assert decision.decision == Decision.DENY


class TestIndex:
    async def test_serves_html(self, client: AsyncClient) -> None:
        resp = await client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "serve-review" in resp.text


class TestFormatters:
    def test_format_json_approve(self) -> None:
        from serve_review.models import ReviewDecision

        d = ReviewDecision(decision=Decision.APPROVE)
        result = format_decision_json(d)
        assert '"approve"' in result

    def test_format_human_deny(self) -> None:
        from serve_review.models import ReviewComment, ReviewDecision

        d = ReviewDecision(
            decision=Decision.DENY,
            overall_comment="Fix it",
            comments=[ReviewComment(body="Bad", file="f.py", line=10)],
        )
        result = format_decision_human(d)
        assert "CHANGES REQUESTED" in result
        assert "f.py:10" in result
        assert "Bad" in result
