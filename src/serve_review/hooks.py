"""Hook installation for git pre-push and Claude Code."""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
from pathlib import Path

_SERVE_REVIEW_MARKER = "# serve-review pre-push hook"

# Standalone hook: runs serve-review then exits with its result.
_HOOK_STANDALONE = """\
#!/bin/sh
{marker}
serve-review --hook "$@"
"""

# Chaining hook: runs the original hook first, then serve-review for human review.
# Stdin (ref info) is saved to a temp file so both hooks can read it.
_HOOK_CHAINING = """\
#!/bin/sh
{marker}
_sr_stdin=$(mktemp)
cat > "$_sr_stdin"
# Run the original pre-push hook first (lint, format, etc).
"{original}" "$@" < "$_sr_stdin" || {{ rm -f "$_sr_stdin"; exit $?; }}
# Automated checks passed. Now run serve-review for human review.
serve-review --hook "$@" < "$_sr_stdin"
_sr_rc=$?
rm -f "$_sr_stdin"
exit $_sr_rc
"""

_PRE_COMMIT_MARKER = "pre-commit"


def get_git_hooks_dir() -> Path:
    """Get the git hooks directory for the current repo.

    Uses --git-common-dir so hooks are installed in the shared location,
    which is correct for worktrees (git resolves hooks from the common dir).
    """
    import subprocess

    result = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        capture_output=True,
        text=True,
        check=True,
    )
    git_dir = Path(result.stdout.strip())
    return git_dir / "hooks"


def _is_pre_commit_hook(path: Path) -> bool:
    """Check if a hook file was installed by the pre-commit framework."""
    try:
        content = path.read_text()
    except OSError:
        return False
    return _PRE_COMMIT_MARKER in content


def _is_serve_review_hook(path: Path) -> bool:
    """Check if a hook file was installed by serve-review."""
    try:
        content = path.read_text()
    except OSError:
        return False
    return _SERVE_REVIEW_MARKER in content


class HookInstallResult:
    """Result of a hook installation attempt."""

    def __init__(self, path: Path, chained: bool = False, message: str = "") -> None:
        self.path = path
        self.chained = chained
        self.message = message


def install_pre_push_hook(force: bool = False) -> HookInstallResult:
    """Install the pre-push hook script.

    If an existing hook is present:
    - If it's a serve-review hook: overwrite it.
    - If it's another hook and force=False: raise FileExistsError with guidance.
    - If it's another hook and force=True: back up the original and install a
      chaining hook that runs serve-review first, then the original.
    """
    hooks_dir = get_git_hooks_dir()
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook_path = hooks_dir / "pre-push"
    backup_path = hooks_dir / "pre-push.original"

    if hook_path.exists():
        if _is_serve_review_hook(hook_path):
            # Ours, safe to overwrite
            pass
        elif not force:
            hint = ""
            if _is_pre_commit_hook(hook_path):
                hint = (
                    "\n\nThis looks like a pre-commit framework hook. You can either:"
                    "\n  1. Use --force to chain serve-review before pre-commit"
                    "\n  2. Add serve-review as a local pre-commit hook instead"
                    "\n     (see: serve-review pre-commit-config)"
                )
            else:
                hint = "\n\nUse --force to chain serve-review before the existing hook."
            raise FileExistsError(f"Pre-push hook already exists at {hook_path}.{hint}")
        else:
            # Back up existing hook and create chaining wrapper
            shutil.copy2(hook_path, backup_path)
            backup_path.chmod(backup_path.stat().st_mode | stat.S_IEXEC)
            hook_content = _HOOK_CHAINING.format(
                marker=_SERVE_REVIEW_MARKER,
                original=backup_path,
            )
            hook_path.write_text(hook_content)
            hook_path.chmod(hook_path.stat().st_mode | stat.S_IEXEC)
            return HookInstallResult(
                path=hook_path,
                chained=True,
                message=f"Original hook backed up to {backup_path}",
            )

    # No existing hook (or it was ours): install standalone
    hook_content = _HOOK_STANDALONE.format(marker=_SERVE_REVIEW_MARKER)
    hook_path.write_text(hook_content)
    hook_path.chmod(hook_path.stat().st_mode | stat.S_IEXEC)
    return HookInstallResult(path=hook_path)


def uninstall_pre_push_hook() -> bool:
    """Remove the pre-push hook if it was installed by serve-review.

    If a backup of the original hook exists, restore it.
    Returns True if a serve-review hook was removed.
    """
    hooks_dir = get_git_hooks_dir()
    hook_path = hooks_dir / "pre-push"
    backup_path = hooks_dir / "pre-push.original"

    if not hook_path.exists():
        return False

    if not _is_serve_review_hook(hook_path):
        return False

    hook_path.unlink()

    # Restore backed-up original hook if present
    if backup_path.exists():
        shutil.move(str(backup_path), str(hook_path))
        return True

    return True


def generate_pre_commit_config() -> str:
    """Generate a .pre-commit-config.yaml snippet for serve-review.

    This can be added to an existing pre-commit config to run serve-review
    as part of the pre-commit framework's pre-push hooks.
    """
    return """\
  - repo: local
    hooks:
      - id: serve-review
        name: serve-review
        entry: serve-review --hook
        language: system
        always_run: true
        stages: [pre-push]
        pass_filenames: false
"""


def get_claude_code_hook_config(port: int = 8567) -> dict[str, object]:
    """Generate Claude Code PreToolUse hook configuration."""
    serve_review_path = _find_serve_review_executable()
    return {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "pattern": "git\\s+push",
                    "command": f"{serve_review_path} --claude-hook --port {port}",
                    "blocking": True,
                }
            ]
        }
    }


def install_claude_code_hook(port: int = 8567, global_: bool = False) -> Path:
    """Install Claude Code PreToolUse hook.

    Args:
        port: Port for the review server.
        global_: If True, install to ~/.claude/settings.json (user-wide).
                 If False, install to .claude/settings.json (project-level).

    Returns the path to the settings file that was modified.
    """
    settings_dir = Path.home() / ".claude" if global_ else Path(".claude")

    settings_dir.mkdir(parents=True, exist_ok=True)
    settings_path = settings_dir / "settings.json"

    settings: dict[str, object] = {}
    if settings_path.exists():
        settings = json.loads(settings_path.read_text())

    hook_config = get_claude_code_hook_config(port)

    # Merge hooks into existing settings
    existing_hooks = settings.get("hooks", {})
    if not isinstance(existing_hooks, dict):
        existing_hooks = {}

    pre_tool = existing_hooks.get("PreToolUse", [])
    if not isinstance(pre_tool, list):
        pre_tool = []

    # Remove any existing serve-review hooks
    pre_tool = [h for h in pre_tool if "serve-review" not in str(h.get("command", ""))]

    # Add ours
    new_hooks = hook_config["hooks"]
    assert isinstance(new_hooks, dict)
    new_pre_tool = new_hooks.get("PreToolUse", [])
    assert isinstance(new_pre_tool, list)
    pre_tool.extend(new_pre_tool)

    existing_hooks["PreToolUse"] = pre_tool
    settings["hooks"] = existing_hooks

    settings_path.write_text(json.dumps(settings, indent=2) + "\n")
    return settings_path


def _find_serve_review_executable() -> str:
    """Find the serve-review executable path."""
    # Check if we're in a venv
    venv = os.environ.get("VIRTUAL_ENV")
    if venv:
        candidate = os.path.join(venv, "bin", "serve-review")
        if os.path.exists(candidate):
            return candidate

    # Fall back to the Python executable's directory
    bin_dir = os.path.dirname(sys.executable)
    candidate = os.path.join(bin_dir, "serve-review")
    if os.path.exists(candidate):
        return candidate

    # Last resort
    return "serve-review"
