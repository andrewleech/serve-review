"""Tests for the on-disk cache (decision cache + PID files)."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

if TYPE_CHECKING:
    from pathlib import Path

    from serve_review.models import FileDiff

from serve_review import cache
from serve_review.models import (
    AttentionFlag,
    AttentionKind,
    Decision,
    DiffHunk,
    DiffLine,
    FileDiff,
    ReviewComment,
    ReviewDecision,
    compute_diff_hash,
)


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect CACHE_DIR to a per-test tmp dir so tests can't see each other."""
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def mock_serve_review_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock _pid_is_serve_review to return True so pytest process passes the check."""
    monkeypatch.setattr(cache, "_pid_is_serve_review", lambda pid: True)


def _make_files(*specs: tuple[str, list[tuple[str, str]]]) -> list[FileDiff]:
    """Build minimal FileDiff list. Each spec is (path, [(line_type, content), ...])."""
    files = []
    for path, lines in specs:
        files.append(
            FileDiff(
                old_path=path,
                new_path=path,
                is_new=False,
                is_deleted=False,
                is_rename=False,
                language="plaintext",
                hunks=[
                    DiffHunk(
                        header="@@ -1 +1 @@",
                        lines=[
                            DiffLine(line_type=lt, content=c, old_line_no=None, new_line_no=1)
                            for lt, c in lines
                        ],
                    )
                ],
            )
        )
    return files


class TestComputeDiffHash:
    def test_same_input_same_hash(self) -> None:
        files = _make_files(("a.py", [("+", "x = 1")]))
        assert compute_diff_hash(files) == compute_diff_hash(files)

    def test_different_path_different_hash(self) -> None:
        a = _make_files(("a.py", [("+", "x = 1")]))
        b = _make_files(("b.py", [("+", "x = 1")]))
        assert compute_diff_hash(a) != compute_diff_hash(b)

    def test_different_content_different_hash(self) -> None:
        a = _make_files(("a.py", [("+", "x = 1")]))
        b = _make_files(("a.py", [("+", "x = 2")]))
        assert compute_diff_hash(a) != compute_diff_hash(b)

    def test_separator_collision_avoided(self) -> None:
        # Without separators, "ab" + "c" hashes the same as "a" + "bc".
        # Two files where path + content would concat to the same bytes
        # must still hash differently.
        a = _make_files(("ab", [("+", "c")]))
        b = _make_files(("a", [("+", "bc")]))
        assert compute_diff_hash(a) != compute_diff_hash(b)

    def test_file_order_does_not_matter(self) -> None:
        forward = _make_files(
            ("a.py", [("+", "x")]),
            ("b.py", [("+", "y")]),
        )
        reversed_ = _make_files(
            ("b.py", [("+", "y")]),
            ("a.py", [("+", "x")]),
        )
        assert compute_diff_hash(forward) == compute_diff_hash(reversed_)

    def test_line_type_matters(self) -> None:
        added = _make_files(("a.py", [("+", "x = 1")]))
        removed = _make_files(("a.py", [("-", "x = 1")]))
        assert compute_diff_hash(added) != compute_diff_hash(removed)

    def test_unicode_content(self) -> None:
        files = _make_files(("a.py", [("+", "x = 'héllo'")]))
        # Should not raise and produce a valid hex string.
        result = compute_diff_hash(files)
        assert len(result) == 64
        assert all(c in "0123456789abcdef" for c in result)

    def test_known_value_pin(self) -> None:
        # Pin a minimal input to a known hash so any change to the hash
        # construction (separator bytes, field ordering, encoding) is loud.
        # If git_ops ever switches its path-prefix convention, every cache
        # entry instantly invalidates without warning unless this test fires.
        files = _make_files(("a.py", [("+", "x = 1")]))
        assert (
            compute_diff_hash(files)
            == "30c530cb32f4ad5ea52c0e1bd8998d4cc4165f52986a86ee64f2c92d72523661"
        )


class TestCachePaths:
    def test_decisions_dir_created_on_demand(self, isolated_cache: Path) -> None:
        path = cache.decisions_dir()
        assert path.exists()
        assert path == isolated_cache / "decisions"

    def test_pid_file_per_port(self, isolated_cache: Path) -> None:
        assert cache.pid_file(8567) == isolated_cache / "daemon-8567.pid"
        assert cache.pid_file(9000) == isolated_cache / "daemon-9000.pid"

    def test_log_file_shared(self, isolated_cache: Path) -> None:
        assert cache.log_file() == isolated_cache / "daemon.log"


class TestStoreLookupDecision:
    def _make_decision(self) -> ReviewDecision:
        return ReviewDecision(
            decision=Decision.APPROVE,
            overall_comment="LGTM",
            comments=[ReviewComment(body="nit", file="a.py", line=10)],
        )

    def test_round_trip(self) -> None:
        cache.store_decision("abc123", self._make_decision(), branch="main", remote="origin")
        cached = cache.lookup_cached_decision("abc123")
        assert cached is not None
        assert cached.diff_hash == "abc123"
        assert cached.decision.decision == Decision.APPROVE
        assert cached.branch == "main"
        assert cached.remote == "origin"
        assert cached.decision.comments[0].body == "nit"

    def test_lookup_missing_returns_none(self) -> None:
        assert cache.lookup_cached_decision("does-not-exist") is None

    def test_lookup_malformed_returns_none(self, isolated_cache: Path) -> None:
        cache.decisions_dir()  # ensure the directory exists
        (isolated_cache / "decisions" / "bad.json").write_text("not json")
        assert cache.lookup_cached_decision("bad") is None

    def test_lookup_stale_returns_none(self, isolated_cache: Path) -> None:
        cache.store_decision("stale", self._make_decision())
        # Backdate the timestamp by 50h.
        path = isolated_cache / "decisions" / "stale.json"
        data = json.loads(path.read_text())
        old = datetime.now(UTC) - timedelta(hours=50)
        data["timestamp"] = old.isoformat(timespec="seconds")
        path.write_text(json.dumps(data))

        assert cache.lookup_cached_decision("stale") is None

    def test_atomic_write_no_partial_files(self, isolated_cache: Path) -> None:
        cache.store_decision("atomic", self._make_decision())
        files = list((isolated_cache / "decisions").iterdir())
        # Only the final file should remain, no .tmp leftover.
        assert len(files) == 1
        assert files[0].name == "atomic.json"


class TestCleanupStaleDecisions:
    def _make(self) -> ReviewDecision:
        return ReviewDecision(decision=Decision.APPROVE)

    def test_keeps_fresh(self) -> None:
        cache.store_decision("fresh", self._make())
        deleted = cache.cleanup_stale_decisions()
        assert deleted == 0
        assert cache.lookup_cached_decision("fresh") is not None

    def test_deletes_stale(self, isolated_cache: Path) -> None:
        cache.store_decision("old", self._make())
        path = isolated_cache / "decisions" / "old.json"
        data = json.loads(path.read_text())
        data["timestamp"] = (datetime.now(UTC) - timedelta(hours=72)).isoformat(timespec="seconds")
        path.write_text(json.dumps(data))

        deleted = cache.cleanup_stale_decisions()
        assert deleted == 1
        assert not path.exists()

    def test_deletes_malformed(self, isolated_cache: Path) -> None:
        cache.decisions_dir()
        bad = isolated_cache / "decisions" / "bad.json"
        bad.write_text("garbage")
        deleted = cache.cleanup_stale_decisions()
        assert deleted == 1
        assert not bad.exists()

    def test_no_dir_returns_zero(self, isolated_cache: Path) -> None:
        # decisions_dir hasn't been called yet; cleanup should be a no-op.
        assert cache.cleanup_stale_decisions() == 0


class TestCacheAgeHuman:
    def _ago(self, **kwargs: int) -> str:
        return (datetime.now(UTC) - timedelta(**kwargs)).isoformat(timespec="seconds")

    def test_just_now(self) -> None:
        assert cache.cache_age_human(self._ago(seconds=10)) == "just now"

    def test_minutes(self) -> None:
        assert cache.cache_age_human(self._ago(minutes=5)) == "5m ago"

    def test_hours(self) -> None:
        assert cache.cache_age_human(self._ago(hours=3)) == "3h ago"

    def test_days(self) -> None:
        assert cache.cache_age_human(self._ago(days=2)) == "2d ago"

    def test_future_timestamp_just_now(self) -> None:
        future = (datetime.now(UTC) + timedelta(minutes=5)).isoformat(timespec="seconds")
        assert cache.cache_age_human(future) == "just now"

    def test_naive_timestamp_treated_as_utc(self) -> None:
        naive = (datetime.now(UTC) - timedelta(hours=2)).replace(tzinfo=None).isoformat()
        # Should not raise; should produce hours bucket.
        result = cache.cache_age_human(naive)
        assert result.endswith("h ago")

    def test_invalid_returns_unknown(self) -> None:
        assert cache.cache_age_human("not a timestamp") == "unknown age"


class TestPidFile:
    def test_round_trip_alive(self, mock_serve_review_process: None) -> None:
        cache.write_pid_file(8567, os.getpid())
        assert cache.read_pid_file(8567) == os.getpid()

    def test_dead_pid_returns_none_and_cleans_up(
        self, isolated_cache: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache.write_pid_file(8567, 99999)
        path = isolated_cache / "daemon-8567.pid"
        assert path.exists()

        # Force ProcessLookupError to simulate a dead PID.
        def fake_kill(pid: int, sig: int) -> None:
            raise ProcessLookupError

        with patch.object(os, "kill", fake_kill):
            assert cache.read_pid_file(8567) is None

        # The stale file should be gone.
        assert not path.exists()

    def test_corrupt_pid_returns_none_and_removes(self, isolated_cache: Path) -> None:
        path = isolated_cache / "daemon-8567.pid"
        path.write_text("not a number\n")
        assert cache.read_pid_file(8567) is None
        assert not path.exists()

    def test_missing_pid_file_returns_none(self) -> None:
        assert cache.read_pid_file(9999) is None

    def test_remove_pid_file(self, mock_serve_review_process: None) -> None:
        cache.write_pid_file(8567, os.getpid())
        cache.remove_pid_file(8567)
        assert cache.read_pid_file(8567) is None

    def test_remove_missing_is_noop(self) -> None:
        # Should not raise.
        cache.remove_pid_file(54321)

    def test_per_port_isolation(self, mock_serve_review_process: None) -> None:
        cache.write_pid_file(8567, os.getpid())
        cache.write_pid_file(9000, os.getpid())
        assert cache.read_pid_file(8567) == os.getpid()
        assert cache.read_pid_file(9000) == os.getpid()
        cache.remove_pid_file(8567)
        assert cache.read_pid_file(8567) is None
        assert cache.read_pid_file(9000) == os.getpid()


class TestListDaemons:
    def test_empty(self) -> None:
        assert cache.list_daemons() == []

    def test_lists_alive_in_port_order(self, mock_serve_review_process: None) -> None:
        cache.write_pid_file(9000, os.getpid())
        cache.write_pid_file(8567, os.getpid())
        result = cache.list_daemons()
        assert result == [(8567, os.getpid()), (9000, os.getpid())]

    def test_skips_dead(self, isolated_cache: Path, mock_serve_review_process: None) -> None:
        cache.write_pid_file(8567, os.getpid())
        cache.write_pid_file(9000, 99999)

        def fake_kill(pid: int, sig: int) -> None:
            if pid == 99999:
                raise ProcessLookupError

        with patch.object(os, "kill", fake_kill):
            result = cache.list_daemons()

        assert result == [(8567, os.getpid())]
        # And the dead one's file was cleaned up.
        assert not (isolated_cache / "daemon-9000.pid").exists()

    def test_ignores_unrelated_files(self, isolated_cache: Path, mock_serve_review_process: None) -> None:
        (isolated_cache / "not-a-pid.txt").write_text("hello")
        (isolated_cache / "daemon-abc.pid").write_text("123")  # non-numeric port
        cache.write_pid_file(8567, os.getpid())
        result = cache.list_daemons()
        assert result == [(8567, os.getpid())]


def test_attention_flag_round_trips_through_compute_hash() -> None:
    """Smoke test: hash works on a FileDiff with attention flags attached."""
    files = [
        FileDiff(
            old_path="x.c",
            new_path="x.c",
            is_new=False,
            is_deleted=False,
            is_rename=False,
            language="c",
            hunks=[
                DiffHunk(
                    header="@@ -1 +1 @@",
                    lines=[
                        DiffLine(
                            line_type="+",
                            content="// Copyright 2024",
                            old_line_no=None,
                            new_line_no=1,
                            flags=[
                                AttentionFlag(
                                    kind=AttentionKind.COPYRIGHT,
                                    start=3,
                                    end=18,
                                    text="Copyright 2024",
                                ),
                            ],
                        )
                    ],
                )
            ],
        )
    ]
    # Attention flags must not affect the hash (they're metadata, not content).
    files_no_flags = [
        FileDiff(
            old_path="x.c",
            new_path="x.c",
            is_new=False,
            is_deleted=False,
            is_rename=False,
            language="c",
            hunks=[
                DiffHunk(
                    header="@@ -1 +1 @@",
                    lines=[
                        DiffLine(
                            line_type="+",
                            content="// Copyright 2024",
                            old_line_no=None,
                            new_line_no=1,
                        ),
                    ],
                )
            ],
        )
    ]
    assert compute_diff_hash(files) == compute_diff_hash(files_no_flags)
