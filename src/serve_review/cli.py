"""CLI entry point for serve-review."""

from __future__ import annotations

import asyncio
import sys
from typing import TYPE_CHECKING

import click

from serve_review.models import Decision

if TYPE_CHECKING:
    from serve_review.models import ReviewRequest

DEFAULT_PORT = 8567


@click.group(invoke_without_command=True, context_settings={"ignore_unknown_options": True})
@click.option("--port", "-p", default=DEFAULT_PORT, help="Port to serve the review UI on.")
@click.option("--host", default="0.0.0.0", help="Host to bind to.")
@click.option("--base", default=None, help="Base ref for manual diff (instead of hook stdin).")
@click.option("--head", default=None, help="Head ref for manual diff (defaults to HEAD).")
@click.option("--hook", is_flag=True, hidden=True, help="Running as git pre-push hook.")
@click.option("--claude-hook", is_flag=True, hidden=True, help="Running as Claude Code hook.")
@click.argument("hook_args", nargs=-1, type=click.UNPROCESSED)
@click.pass_context
def main(
    ctx: click.Context,
    port: int,
    host: str,
    base: str | None,
    head: str | None,
    hook: bool,
    claude_hook: bool,
    hook_args: tuple[str, ...],
) -> None:
    """Pre-push review gate with mobile-friendly web UI."""
    if ctx.invoked_subcommand is not None:
        return

    is_hook = hook or claude_hook
    try:
        _run_review(
            hook=is_hook,
            hook_args=hook_args,
            base=base,
            head=head,
            host=host,
            port=port,
        )
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        if is_hook:
            # In hook mode, a crash must not block the push. Warn and allow.
            click.echo(f"serve-review: crashed, allowing push: {exc}", err=True)
            sys.exit(0)
        raise


def _run_review(
    *,
    hook: bool,
    hook_args: tuple[str, ...],
    base: str | None,
    head: str | None,
    host: str,
    port: int,
) -> None:
    from serve_review.git_ops import build_review_from_refs, build_review_request, parse_push_stdin
    from serve_review.server import format_decision_human, format_decision_json, run_server

    # Determine how we were invoked and build a refresh function
    refresh_fn = None
    if hook:
        # Git passes <remote-name> <remote-url> as positional args to pre-push hooks
        remote_name = hook_args[0] if len(hook_args) > 0 else "origin"
        remote_url = hook_args[1] if len(hook_args) > 1 else ""
        pushes = parse_push_stdin(sys.stdin, remote_name=remote_name, remote_url=remote_url)
        if not pushes:
            # Nothing to review (e.g. branch delete)
            sys.exit(0)
        push_info = pushes[0]
        review = build_review_request(push_info)
        refresh_fn = lambda: build_review_request(push_info)  # noqa: E731
    elif base is not None:
        resolved_base = base
        resolved_head = head or "HEAD"
        review = build_review_from_refs(resolved_base, resolved_head)
        refresh_fn = lambda: build_review_from_refs(resolved_base, resolved_head)  # noqa: E731
    else:
        review = _build_default_review()
        refresh_fn = _build_default_review

    if not review.commits and not review.files:
        click.echo("Nothing to review: no commits or changes between base and HEAD.", err=True)
        sys.exit(0)

    url = _build_review_url(host, port)
    click.echo(f"Review: {url}", err=True)
    click.echo(
        f"  {len(review.commits)} commit(s), {len(review.files)} file(s)"
        f"{', FORCE PUSH' if review.push_info.is_force_push else ''}",
        err=True,
    )
    click.echo("Blocking until review is approved or denied at the URL above", err=True)

    try:
        decision = asyncio.run(run_server(review, port=port, host=host, refresh_fn=refresh_fn))
    except OSError as exc:
        click.echo(str(exc), err=True)
        sys.exit(1)

    # Output results
    click.echo(format_decision_human(decision), err=True)

    if decision.decision == Decision.DENY:
        # Machine-readable JSON to stdout for AI agents
        click.echo(format_decision_json(decision))
        sys.exit(1)


@main.command()
@click.option("--force", is_flag=True, help="Chain with existing hook (backs up original).")
def install_hook(force: bool) -> None:
    """Install git pre-push hook in the current repository."""
    from serve_review.hooks import install_pre_push_hook

    try:
        result = install_pre_push_hook(force=force)
        click.echo(f"Installed pre-push hook at {result.path}")
        if result.chained:
            click.echo(f"  {result.message}")
            click.echo("  Original hook runs first, then serve-review for human review.")
    except FileExistsError as e:
        click.echo(str(e), err=True)
        sys.exit(1)


@main.command()
def uninstall_hook() -> None:
    """Remove the git pre-push hook and restore any backed-up original."""
    from serve_review.hooks import uninstall_pre_push_hook

    if uninstall_pre_push_hook():
        click.echo("Removed pre-push hook.")
    else:
        click.echo("No serve-review pre-push hook found.", err=True)
        sys.exit(1)


@main.command()
def pre_commit_config() -> None:
    """Print a .pre-commit-config.yaml snippet for serve-review."""
    from serve_review.hooks import generate_pre_commit_config

    click.echo("Add this to your .pre-commit-config.yaml under 'repos:':\n")
    click.echo(generate_pre_commit_config())
    click.echo("Then run: pre-commit install --hook-type pre-push")


@main.command()
@click.option("--port", "-p", default=DEFAULT_PORT, help="Port for the review UI.")
@click.option(
    "--global", "global_", is_flag=True, help="Install user-wide (~/.claude/settings.json)."
)
def install_claude_hook(port: int, global_: bool) -> None:
    """Install Claude Code PreToolUse hook.

    By default installs to the project (.claude/settings.json).
    Use --global for all projects (~/.claude/settings.json).
    """
    from serve_review.hooks import install_claude_code_hook

    path = install_claude_code_hook(port=port, global_=global_)
    scope = "global" if global_ else "project"
    click.echo(f"Installed Claude Code hook ({scope}) in {path}")


def _build_default_review() -> ReviewRequest:
    """Build a review of the current branch vs its fork point from the default branch."""
    import subprocess

    from serve_review.git_ops import build_review_from_refs, run_git

    try:
        run_git("symbolic-ref", "--short", "HEAD")
    except subprocess.CalledProcessError:
        click.echo("Not on a branch. Use --base and --head to specify refs.", err=True)
        sys.exit(1)

    base = _find_default_branch()
    click.echo(f"Reviewing current branch against {base}", err=True)
    return build_review_from_refs(base, "HEAD")


def _find_default_branch() -> str:
    """Find the best base ref to diff against.

    Tries upstream remotes first (common in fork workflows), then origin.
    """
    import subprocess

    from serve_review.git_ops import run_git

    # Try common base refs in priority order.
    # upstream/* first (fork workflow), then origin/* (direct clone).
    candidates = [
        "upstream/master",
        "upstream/main",
        "origin/master",
        "origin/main",
    ]

    # Also try origin/HEAD (set by git clone)
    try:
        ref = run_git("symbolic-ref", "refs/remotes/origin/HEAD")
        candidates.insert(0, ref.replace("refs/remotes/", ""))
    except subprocess.CalledProcessError:
        pass

    for candidate in candidates:
        try:
            run_git("rev-parse", "--verify", candidate)
            return candidate
        except subprocess.CalledProcessError:
            continue

    click.echo(
        "Could not determine default branch. Use --base to specify.",
        err=True,
    )
    sys.exit(1)


def _build_review_url(host: str, port: int) -> str:
    """Build a useful review URL, preferring the Tailscale FQDN."""
    import socket
    import subprocess

    # If bound to a specific non-wildcard address, use that
    if host not in ("0.0.0.0", "::"):
        return f"http://{host}:{port}"

    # Try Tailscale FQDN first
    try:
        result = subprocess.run(
            ["tailscale", "status", "--self", "--json"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode == 0:
            import json

            data = json.loads(result.stdout)
            dns_name = data.get("Self", {}).get("DNSName", "")
            if dns_name:
                # DNSName has a trailing dot, strip it
                return f"http://{dns_name.rstrip('.')}:{port}"
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
        pass

    # Fall back to hostname
    hostname = socket.getfqdn() or socket.gethostname()
    return f"http://{hostname}:{port}"
