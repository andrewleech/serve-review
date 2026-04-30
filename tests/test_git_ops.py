"""Tests for git_ops: upstream discovery and merge-base diff resolution.

These are integration tests that exercise real git repositories. They cover
the production-critical paths in build_review_request that determine which
diff range a force-pushed branch is reviewed against.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

from serve_review.git_ops import build_review_request, find_upstream_default
from serve_review.models import PushInfo


def _git(cwd: Path, *args: str) -> str:
    """Run git in ``cwd`` and return stripped stdout."""
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _init_repo(parent: Path, name: str = "repo") -> Path:
    """Initialise a fresh repo under ``parent`` with one initial commit."""
    repo = parent / name
    repo.mkdir()
    _git(repo, "-c", "init.defaultBranch=main", "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "commit", "--allow-empty", "-q", "-m", "initial")
    return repo


def _commit(repo: Path, filename: str, content: str, message: str) -> str:
    """Write ``content`` to ``filename``, commit with ``message``, return SHA."""
    (repo / filename).write_text(content)
    _git(repo, "add", filename)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


class TestFindUpstreamDefault:
    def test_returns_none_when_no_remotes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _init_repo(tmp_path)
        monkeypatch.chdir(repo)
        assert find_upstream_default() is None

    def test_resolves_origin_head_after_clone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        upstream = _init_repo(tmp_path, "upstream")
        clone = tmp_path / "clone"
        _git(tmp_path, "clone", "-q", str(upstream), str(clone))
        monkeypatch.chdir(clone)
        # git clone sets refs/remotes/origin/HEAD -> refs/remotes/origin/main
        assert find_upstream_default() == "origin/main"

    def test_prefers_upstream_remote_over_origin(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        upstream = _init_repo(tmp_path, "upstream")
        fork = _init_repo(tmp_path, "fork")
        clone = tmp_path / "clone"
        _git(tmp_path, "clone", "-q", str(fork), str(clone))
        _git(clone, "remote", "add", "upstream", str(upstream))
        _git(clone, "fetch", "-q", "upstream")
        _git(clone, "remote", "set-head", "upstream", "--auto")
        monkeypatch.chdir(clone)
        # When both upstream and origin have HEAD set, upstream wins
        # (matches the fork-workflow convention).
        assert find_upstream_default() == "upstream/main"

    def test_config_overrides_symbolic_ref(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        upstream = _init_repo(tmp_path, "upstream")
        clone = tmp_path / "clone"
        _git(tmp_path, "clone", "-q", str(upstream), str(clone))
        _git(clone, "config", "serve-review.upstreamRef", "origin/custom-branch")
        monkeypatch.chdir(clone)
        assert find_upstream_default() == "origin/custom-branch"


class TestBuildReviewRequest:
    def _push(self, remote: str, local: str) -> PushInfo:
        return PushInfo(
            local_ref="refs/heads/feature",
            local_sha=local,
            remote_ref="refs/heads/feature",
            remote_sha=remote,
            remote_name="origin",
            remote_url="",
            is_force_push=False,
        )

    def test_rebase_force_push_uses_merge_base_not_remote_sha(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The original bug: after rebase onto an advanced upstream, using
        remote_sha as the diff base shows upstream churn alongside branch
        commits. merge-base resolution must filter out the upstream churn.
        """
        # Build an "origin" repo whose main has advanced twice past the
        # branching point. Main and feature touch different files so the
        # rebase below is conflict-free.
        origin = _init_repo(tmp_path, "origin")
        _commit(origin, "main.txt", "v1\n", "advance main 1")
        _commit(origin, "main.txt", "v2\n", "advance main 2")

        # Clone, then create a feature branch from before the advances.
        clone = tmp_path / "clone"
        _git(tmp_path, "clone", "-q", str(origin), str(clone))
        monkeypatch.chdir(clone)

        _git(clone, "checkout", "-q", "-b", "feature", "main~2")
        old_tip = _commit(clone, "branch.txt", "branch\n", "branch commit")

        # Rebase onto current main: replays "branch commit" on top of v2.
        _git(clone, "rebase", "-q", "main")
        new_tip = _git(clone, "rev-parse", "HEAD")

        push = self._push(remote=old_tip, local=new_tip)
        review = build_review_request(push)

        # If the implementation used remote_sha (old_tip), the commits
        # list would include the two upstream advance commits, so len
        # would be 3. With merge-base resolution against origin/main,
        # only the rebased branch commit should appear.
        assert len(review.commits) == 1
        assert review.commits[0].message == "branch commit"

    def test_falls_back_to_remote_sha_with_no_upstream(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _init_repo(tmp_path)
        a = _commit(repo, "file.txt", "a\n", "first")
        b = _commit(repo, "file.txt", "b\n", "second")
        monkeypatch.chdir(repo)

        push = self._push(remote=a, local=b)
        review = build_review_request(push)

        # No upstream discoverable, so merge-base step is skipped and
        # base falls back to remote_sha. Commits between a and b is just
        # the second commit.
        assert len(review.commits) == 1
        assert review.commits[0].message == "second"
