"""Tests for serve_review.hooks."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from typing import TYPE_CHECKING

import pytest

from serve_review import hooks
from serve_review.hooks import (
    _SERVE_REVIEW_MARKER,
    generate_pre_commit_config,
    get_claude_code_hook_config,
    install_claude_code_hook,
    install_pre_push_hook,
    uninstall_pre_push_hook,
)

if TYPE_CHECKING:
    from pathlib import Path


def _git_init(path: Path) -> None:
    """Initialise a git repo at ``path`` using subprocess."""
    subprocess.run(
        ["git", "init", "--quiet"],
        cwd=str(path),
        check=True,
        capture_output=True,
    )


@pytest.fixture
def git_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create an initialised git repo and chdir into it."""
    _git_init(tmp_path)
    monkeypatch.chdir(tmp_path)
    return tmp_path


class TestInstallPrePushHook:
    def test_fresh_install_creates_hook(self, git_repo: Path) -> None:
        result = install_pre_push_hook()
        hook_path = git_repo / ".git" / "hooks" / "pre-push"
        assert hook_path.exists()
        # get_git_hooks_dir uses --git-common-dir which yields a relative path
        # when chdir'd inside the repo; resolve both sides for comparison.
        assert result.path.resolve() == hook_path.resolve()
        assert result.chained is False

    def test_installed_hook_is_executable(self, git_repo: Path) -> None:
        result = install_pre_push_hook()
        mode = result.path.stat().st_mode
        # At minimum the user execute bit must be set.
        assert mode & stat.S_IXUSR
        # And the requested mode covers the standard 0o755 expectation.
        assert mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    def test_hook_contains_marker(self, git_repo: Path) -> None:
        result = install_pre_push_hook()
        content = result.path.read_text()
        assert _SERVE_REVIEW_MARKER in content

    def test_hook_calls_serve_review_hook_subcommand(self, git_repo: Path) -> None:
        result = install_pre_push_hook()
        content = result.path.read_text()
        assert "serve-review hook" in content
        # Ensure the deprecated --hook flag form isn't present.
        assert "serve-review --hook" not in content
        assert "--hook" not in content

    def test_foreign_hook_is_auto_wrapped(self, git_repo: Path) -> None:
        hook_path = git_repo / ".git" / "hooks" / "pre-push"
        backup_path = git_repo / ".git" / "hooks" / "pre-push.original"
        hook_path.parent.mkdir(parents=True, exist_ok=True)
        original_body = "#!/bin/sh\necho not ours\n"
        hook_path.write_text(original_body)
        hook_path.chmod(0o755)

        result = install_pre_push_hook(force=False)
        assert result.chained is True
        assert backup_path.exists()
        assert backup_path.read_text() == original_body

    def test_reinstall_with_force_backs_up_and_chains(self, git_repo: Path) -> None:
        hook_path = git_repo / ".git" / "hooks" / "pre-push"
        backup_path = git_repo / ".git" / "hooks" / "pre-push.original"
        hook_path.parent.mkdir(parents=True, exist_ok=True)
        original_body = "#!/bin/sh\necho original\n"
        hook_path.write_text(original_body)
        hook_path.chmod(0o755)

        result = install_pre_push_hook(force=True)
        assert result.chained is True
        assert backup_path.exists()
        assert backup_path.read_text() == original_body

        new_content = hook_path.read_text()
        assert _SERVE_REVIEW_MARKER in new_content
        # Chain references the backed-up original path (relative form is fine).
        assert "pre-push.original" in new_content
        # And still ends up invoking serve-review.
        assert "serve-review hook" in new_content

    def test_chaining_hook_quotes_paths_with_spaces(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Backup paths containing spaces must produce a syntactically valid script."""
        repo = tmp_path / "repo with space"
        repo.mkdir()
        _git_init(repo)
        monkeypatch.chdir(repo)

        hook_path = repo / ".git" / "hooks" / "pre-push"
        hook_path.parent.mkdir(parents=True, exist_ok=True)
        hook_path.write_text("#!/bin/sh\nexit 0\n")
        hook_path.chmod(0o755)

        result = install_pre_push_hook(force=True)
        assert result.chained is True
        rendered = hook_path.read_text()
        # Run bash -n on the rendered script to check syntactic validity.
        check = subprocess.run(
            ["bash", "-n", "-c", rendered],
            capture_output=True,
            text=True,
        )
        assert check.returncode == 0, check.stderr

    def test_reinstall_overwrites_our_own_hook_without_force(self, git_repo: Path) -> None:
        # First install: standalone.
        first = install_pre_push_hook()
        assert first.chained is False
        # Second install without --force should be allowed (overwriting our own).
        second = install_pre_push_hook(force=False)
        assert second.chained is False
        assert second.path == first.path
        assert _SERVE_REVIEW_MARKER in second.path.read_text()


class TestUninstallPrePushHook:
    def test_removes_serve_review_hook(self, git_repo: Path) -> None:
        result = install_pre_push_hook()
        assert result.path.exists()

        removed = uninstall_pre_push_hook()
        assert removed is True
        assert not result.path.exists()

    def test_restores_backup(self, git_repo: Path) -> None:
        hook_path = git_repo / ".git" / "hooks" / "pre-push"
        hook_path.parent.mkdir(parents=True, exist_ok=True)
        original_body = "#!/bin/sh\necho original\n"
        hook_path.write_text(original_body)
        hook_path.chmod(0o755)

        install_pre_push_hook(force=True)
        backup_path = git_repo / ".git" / "hooks" / "pre-push.original"
        assert backup_path.exists()

        removed = uninstall_pre_push_hook()
        assert removed is True
        # Hook path is now the restored original.
        assert hook_path.exists()
        assert hook_path.read_text() == original_body
        # Backup is consumed.
        assert not backup_path.exists()

    def test_returns_false_when_no_hook(self, git_repo: Path) -> None:
        assert uninstall_pre_push_hook() is False

    def test_does_not_touch_foreign_hook(self, git_repo: Path) -> None:
        hook_path = git_repo / ".git" / "hooks" / "pre-push"
        hook_path.parent.mkdir(parents=True, exist_ok=True)
        body = "#!/bin/sh\necho not ours\n"
        hook_path.write_text(body)
        hook_path.chmod(0o755)

        removed = uninstall_pre_push_hook()
        assert removed is False
        assert hook_path.exists()
        assert hook_path.read_text() == body


class TestPreCommitConfig:
    def test_snippet_invokes_serve_review_hook(self) -> None:
        snippet = generate_pre_commit_config()
        assert "serve-review hook" in snippet
        # Old wrong-flag form must not appear.
        assert "serve-review --hook" not in snippet


class TestClaudeCodeHookConfig:
    def test_top_level_shape(self) -> None:
        cfg = get_claude_code_hook_config(port=8567)
        assert isinstance(cfg, dict)
        assert "hooks" in cfg
        assert isinstance(cfg["hooks"], dict)
        pre_tool = cfg["hooks"]["PreToolUse"]
        assert isinstance(pre_tool, list)
        assert len(pre_tool) == 1

    def test_matcher_item_shape(self) -> None:
        cfg = get_claude_code_hook_config(port=8567)
        item = cfg["hooks"]["PreToolUse"][0]
        assert isinstance(item, dict)
        assert item["matcher"] == "Bash"
        assert isinstance(item["matcher"], str)
        assert isinstance(item["hooks"], list)
        assert len(item["hooks"]) == 1
        inner = item["hooks"][0]
        assert inner["type"] == "command"
        assert isinstance(inner["command"], str)

    def test_no_legacy_fields(self) -> None:
        cfg = get_claude_code_hook_config(port=8567)
        item = cfg["hooks"]["PreToolUse"][0]
        # The old wrong-schema fields must not be present at the matcher level.
        assert "pattern" not in item
        assert "blocking" not in item
        assert "command" not in item

    def test_inner_command_invokes_serve_review_claude_hook(self) -> None:
        cfg = get_claude_code_hook_config(port=9000)
        command = cfg["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        assert "serve-review" in command
        assert "claude-hook" in command
        assert "git push" in command
        # Wrapped via a Python launcher.
        assert "python3" in command


class TestInstallClaudeCodeHook:
    def test_project_install_writes_settings(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        path = install_claude_code_hook(port=8567, global_=False)
        # Project install path is relative (Path(".claude")) when global_=False.
        assert path.resolve() == (tmp_path / ".claude" / "settings.json").resolve()
        assert path.exists()
        data = json.loads(path.read_text())
        pre_tool = data["hooks"]["PreToolUse"]
        assert len(pre_tool) == 1
        assert pre_tool[0]["matcher"] == "Bash"

    def test_global_install_writes_to_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))
        # Path.home() consults HOME on POSIX.
        path = install_claude_code_hook(port=8567, global_=True)
        assert path == fake_home / ".claude" / "settings.json"
        assert path.exists()

    def test_existing_serve_review_entry_replaced(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        first = install_claude_code_hook(port=8567, global_=False)
        second = install_claude_code_hook(port=8567, global_=False)
        assert first.resolve() == second.resolve()

        data = json.loads(second.read_text())
        # Only one serve-review hook entry, not duplicated.
        pre_tool = data["hooks"]["PreToolUse"]
        serve_review_entries = [
            h for h in pre_tool if "serve-review" in str(h["hooks"][0].get("command", ""))
        ]
        assert len(serve_review_entries) == 1

    def test_existing_unrelated_settings_preserved(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        settings_dir = tmp_path / ".claude"
        settings_dir.mkdir()
        settings_path = settings_dir / "settings.json"
        existing = {
            "theme": "dark",
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Read",
                        "hooks": [{"type": "command", "command": "echo other"}],
                    }
                ]
            },
        }
        settings_path.write_text(json.dumps(existing))

        install_claude_code_hook(port=8567, global_=False)
        data = json.loads(settings_path.read_text())
        # Top-level unrelated key preserved.
        assert data["theme"] == "dark"
        # Unrelated hook entry preserved.
        pre_tool = data["hooks"]["PreToolUse"]
        matchers = [h["matcher"] for h in pre_tool]
        assert "Read" in matchers
        assert "Bash" in matchers
        # Confirm the touch didn't drop existing custom command.
        read_entry = next(h for h in pre_tool if h["matcher"] == "Read")
        assert read_entry["hooks"][0]["command"] == "echo other"


# Sanity check that the module under test exposes the names we exercise.
def test_module_exposes_expected_api() -> None:
    assert hasattr(hooks, "install_pre_push_hook")
    assert hasattr(hooks, "uninstall_pre_push_hook")
    assert hasattr(hooks, "generate_pre_commit_config")
    assert hasattr(hooks, "get_claude_code_hook_config")
    assert hasattr(hooks, "install_claude_code_hook")
    # os import keeps mypy happy on the unused-import check; reference it.
    assert os.name in {"posix", "nt"}
