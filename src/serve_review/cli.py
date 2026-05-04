"""CLI entry point for serve-review."""

from __future__ import annotations

import asyncio
import json
import sys
import urllib.error
import urllib.request
from typing import TYPE_CHECKING

import click

from serve_review import cache
from serve_review.models import Decision

if TYPE_CHECKING:
    from serve_review.models import ReviewDecision, ReviewRequest

DEFAULT_PORT = 8567


@click.group(invoke_without_command=True)
@click.option("--port", "-p", default=DEFAULT_PORT, help="Port to serve the review UI on.")
@click.option("--host", default="0.0.0.0", help="Host to bind to.")
@click.option("--base", default=None, help="Base ref for manual diff (instead of hook stdin).")
@click.option("--head", default=None, help="Head ref for manual diff (defaults to HEAD).")
@click.option(
    "--standalone",
    is_flag=True,
    help="Bypass the daemon and run a one-shot standalone server.",
)
@click.pass_context
def main(
    ctx: click.Context,
    port: int,
    host: str,
    base: str | None,
    head: str | None,
    standalone: bool,
) -> None:
    """Pre-push review gate with mobile-friendly web UI."""
    if ctx.invoked_subcommand is not None:
        return

    try:
        _run_review(
            mode="manual",
            hook_args=(),
            base=base,
            head=head,
            host=host,
            port=port,
            standalone=standalone,
        )
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        click.echo(f"serve-review: error: {exc}", err=True)
        raise


@main.command(
    "hook",
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.option("--port", "-p", default=DEFAULT_PORT, help="Port to serve the review UI on.")
@click.option("--host", default="0.0.0.0", help="Host to bind to.")
@click.option(
    "--standalone",
    is_flag=True,
    help="Bypass the daemon and run a one-shot standalone server.",
)
@click.argument("hook_args", nargs=-1, type=click.UNPROCESSED)
def hook_cmd(
    port: int,
    host: str,
    standalone: bool,
    hook_args: tuple[str, ...],
) -> None:
    """Run as a git pre-push hook (reads stdin per the pre-push protocol).

    Git invokes pre-push hooks with two positional arguments: the remote name
    and the remote URL. They are forwarded here as HOOK_ARGS.
    """
    try:
        _run_review(
            mode="hook",
            hook_args=hook_args,
            base=None,
            head=None,
            host=host,
            port=port,
            standalone=standalone,
        )
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        # In hook mode, a crash must not block the push. Warn and allow.
        click.echo(f"serve-review: crashed, allowing push: {exc}", err=True)
        sys.exit(0)


@main.command(
    "claude-hook",
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.option("--port", "-p", default=DEFAULT_PORT, help="Port to serve the review UI on.")
@click.option("--host", default="0.0.0.0", help="Host to bind to.")
@click.option(
    "--standalone",
    is_flag=True,
    help="Bypass the daemon and run a one-shot standalone server.",
)
@click.argument("hook_args", nargs=-1, type=click.UNPROCESSED)
def claude_hook_cmd(
    port: int,
    host: str,
    standalone: bool,
    hook_args: tuple[str, ...],
) -> None:
    """Run as a Claude Code PreToolUse hook (intercepts git push)."""
    try:
        _run_review(
            mode="claude-hook",
            hook_args=hook_args,
            base=None,
            head=None,
            host=host,
            port=port,
            standalone=standalone,
        )
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        click.echo(f"serve-review: crashed, allowing push: {exc}", err=True)
        sys.exit(0)


def _print_review_banner(host: str, port: int, review: ReviewRequest) -> None:
    """Print the review URL and a one-line summary to stderr."""
    url = _build_review_url(host, port)
    click.echo(f"Review: {url}", err=True)
    click.echo(
        f"  {len(review.commits)} commit(s), {len(review.files)} file(s)"
        f"{', FORCE PUSH' if review.push_info.is_force_push else ''}",
        err=True,
    )
    click.echo("Blocking until review is approved or denied at the URL above", err=True)


def _run_review(
    *,
    mode: str,
    hook_args: tuple[str, ...],
    base: str | None,
    head: str | None,
    host: str,
    port: int,
    standalone: bool = False,
) -> None:
    from serve_review.cache import cache_age_human
    from serve_review.client import (
        DaemonError,
        ensure_daemon,
        submit_review,
        wait_for_decision,
    )
    from serve_review.git_ops import build_review_from_refs, build_review_request, parse_push_stdin
    from serve_review.models import compute_diff_hash
    from serve_review.server import format_decision_human, format_decision_json

    is_hook = mode in ("hook", "claude-hook")

    refresh_fn = None
    if mode == "hook":
        # Git passes <remote-name> <remote-url> as positional args to pre-push hooks
        remote_name = hook_args[0] if len(hook_args) > 0 else "origin"
        remote_url = hook_args[1] if len(hook_args) > 1 else ""
        pushes = parse_push_stdin(sys.stdin, remote_name=remote_name, remote_url=remote_url)
        if not pushes:
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
        # mode == "manual" with no base, or mode == "claude-hook"
        review = _build_default_review()
        refresh_fn = _build_default_review

    if not review.commits and not review.files:
        click.echo("Nothing to review: no commits or changes between base and HEAD.", err=True)
        sys.exit(0)

    if standalone:
        decision = _run_standalone(review, host, port, refresh_fn, is_hook)
    else:
        # Track whether the daemon was successfully reached. If we already
        # spawned/connected to a daemon and a later step (submit/wait) fails,
        # falling back to standalone on the SAME port would just collide. In
        # that case, surface the daemon error rather than retrying.
        daemon_ready = False
        try:
            ensure_daemon(host, port)
            daemon_ready = True
            diff_hash = compute_diff_hash(review.files)
            review_id, cached_decision, cached_at = submit_review(port, review, diff_hash)
            if cached_decision is not None:
                age = cache_age_human(cached_at) if cached_at else "unknown age"
                click.echo(
                    f"serve-review: decision served from cache ({age})",
                    err=True,
                )
                decision = cached_decision
            else:
                _print_review_banner(host, port, review)
                decision = wait_for_decision(port, review_id)
        except DaemonError as exc:
            if daemon_ready:
                # Daemon was healthy when we started but the protocol failed
                # mid-flight. Standalone on the same port would collide; just
                # report the error.
                click.echo(f"serve-review: daemon error: {exc}", err=True)
                if is_hook:
                    sys.exit(0)
                sys.exit(1)
            click.echo(
                f"serve-review: daemon unavailable ({exc}), using standalone mode",
                err=True,
            )
            decision = _run_standalone(review, host, port, refresh_fn, is_hook)

    click.echo(format_decision_human(decision), err=True)

    if decision.decision == Decision.DENY:
        click.echo(format_decision_json(decision))
        sys.exit(1)


def _run_standalone(
    review: ReviewRequest,
    host: str,
    port: int,
    refresh_fn: object,
    is_hook: bool,
) -> ReviewDecision:
    """Run the standalone server fallback. Returns the ReviewDecision.

    If ``port`` is already bound (most likely by something unrelated, since
    callers only invoke standalone when the daemon path failed), pick a free
    ephemeral port instead of failing twice.
    """
    from serve_review.server import run_server

    actual_port = _resolve_standalone_port(host, port)
    _print_review_banner(host, actual_port, review)
    try:
        return asyncio.run(
            run_server(review, port=actual_port, host=host, refresh_fn=refresh_fn)  # type: ignore[arg-type]
        )
    except OSError as exc:
        click.echo(str(exc), err=True)
        if is_hook:
            sys.exit(0)
        sys.exit(1)


def _resolve_standalone_port(host: str, preferred: int) -> int:
    """Return ``preferred`` if free; otherwise an OS-assigned ephemeral port.

    Avoids the double-failure where the daemon path failed because the port
    is taken and the standalone retry then trips the same bind error.
    """
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        try:
            sock.bind((host, preferred))
            return preferred
        except OSError:
            pass
        # Preferred is taken. Bind to port 0 to let the OS pick.
        sock.bind((host, 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


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


@main.group()
def daemon() -> None:
    """Manage the review daemon."""


@daemon.command("start")
@click.option("--port", "-p", default=DEFAULT_PORT, help="Port to bind the daemon to.")
@click.option("--host", default="0.0.0.0", help="Host to bind the daemon to.")
@click.option(
    "--disable-tailscale",
    is_flag=True,
    help="Disable automatic Tailscale certificate provisioning.",
)
def daemon_start(port: int, host: str, disable_tailscale: bool) -> None:
    """Start the daemon in foreground (use for debugging)."""
    from serve_review.daemon import run_daemon

    run_daemon(host=host, port=port, disable_tailscale=disable_tailscale)


@daemon.command("stop")
@click.option("--port", "-p", default=DEFAULT_PORT, help="Port of the daemon to stop.")
@click.option("--all", "all_", is_flag=True, help="Stop every running daemon.")
def daemon_stop(port: int, all_: bool) -> None:
    """Stop a running daemon."""
    from serve_review.cache import list_daemons
    from serve_review.client import kill_daemon

    if all_:
        running = list_daemons()
        if not running:
            click.echo("No daemons running.")
            return
        for daemon_port, _pid in running:
            kill_daemon(daemon_port)
            click.echo(f"Stopped daemon on port {daemon_port}.")
        return

    pid = cache.read_pid_file(port)
    if pid is None:
        click.echo(f"No daemon running on port {port}.", err=True)
        sys.exit(1)
    kill_daemon(port)
    click.echo(f"Stopped daemon on port {port}.")


@daemon.command("status")
def daemon_status() -> None:
    """Show all running daemons and their queue depths."""
    from serve_review.cache import list_daemons

    running = list_daemons()
    if not running:
        click.echo("No daemons running.")
        return

    click.echo(f"{len(running)} daemon(s) running:")
    for port, pid in running:
        url = _build_review_url("0.0.0.0", port)
        queued = _query_queue_depth(port)
        queued_str = f"{queued} review(s) queued" if queued is not None else "queue unavailable"
        click.echo(f"  port {port}  pid {pid}  {queued_str}  url {url}")


def _query_queue_depth(port: int) -> int | None:
    """Return the daemon's queued-review count via /api/health, or None on error."""
    url = f"http://127.0.0.1:{port}/api/health"
    try:
        with urllib.request.urlopen(url, timeout=2.0) as resp:
            if resp.status != 200:
                return None
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError):
        return None
    queued = data.get("queued")
    if isinstance(queued, int):
        return queued
    return None


@main.group()
def cert() -> None:
    """Manage TLS certificates."""


@cert.command("status")
@click.option("--port", "-p", default=DEFAULT_PORT, help="Port of the daemon.")
def cert_status(port: int) -> None:
    """Show TLS certificate status."""
    from datetime import UTC, datetime

    from serve_review import cache
    from serve_review.cert_manager import CertificateManager

    cert_manager = CertificateManager(cache.CACHE_DIR)
    crt_path, key_path = cert_manager.get_cert_paths()

    if not crt_path or not key_path:
        click.echo("No certificate provisioned.")
        return

    try:
        from cryptography import x509  # type: ignore[import-not-found]
        from cryptography.hazmat.backends import default_backend  # type: ignore[import-not-found]

        with open(crt_path, "rb") as f:
            cert_data = f.read()
        cert_obj = x509.load_pem_x509_certificate(cert_data, default_backend())
        expiry = cert_obj.not_valid_after_utc

        now = datetime.now(UTC)
        days_left = (expiry - now).days
        hours_left = int(((expiry - now).total_seconds() % 86400) / 3600)

        click.echo(f"Certificate path: {crt_path}")
        click.echo(f"Key path: {key_path}")
        click.echo(f"Expires: {expiry.strftime('%Y-%m-%d %H:%M:%S UTC')}")
        click.echo(f"Time remaining: {days_left}d {hours_left}h")

        if days_left < 7:
            click.echo("warning: Renewal imminent (< 7 days)")
        elif days_left < 30:
            click.echo("note: Renewal will occur within 30 days")

    except ImportError:
        click.echo("cryptography not available; cannot read certificate details")
        click.echo(f"Certificate: {crt_path}")
        click.echo(f"Key: {key_path}")
    except Exception as exc:
        click.echo(f"Error reading certificate: {exc}", err=True)


@cert.command("renew")
def cert_renew() -> None:
    """Manually trigger certificate renewal."""
    from serve_review import cache
    from serve_review.cert_manager import CertificateManager

    cert_manager = CertificateManager(cache.CACHE_DIR)

    if cert_manager.renew():
        click.echo("Certificate renewed successfully.")
    else:
        click.echo("Certificate renewal failed. Check daemon logs.", err=True)


@cert.command("forget")
def cert_forget() -> None:
    """Delete cached certificates. Next daemon start will re-provision."""
    import shutil

    from serve_review import cache

    certs_dir = cache.certs_dir()
    try:
        if certs_dir.exists():
            shutil.rmtree(certs_dir)
            click.echo(f"Deleted certificate cache at {certs_dir}")
        else:
            click.echo("No certificate cache found.")
    except Exception as exc:
        click.echo(f"Error deleting certificate cache: {exc}", err=True)


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

    candidates = [
        "upstream/master",
        "upstream/main",
        "origin/master",
        "origin/main",
    ]

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
    """Build a useful review URL, preferring the Tailscale FQDN.

    Queries /api/health to determine the actual scheme (http vs https).
    """
    import socket
    import subprocess

    from serve_review.client import DaemonError, get_health

    # Query daemon for actual scheme
    scheme = "http"
    try:
        health = get_health(port)
        scheme = health.get("scheme", "http")
    except DaemonError:
        # Daemon not reachable yet, use http
        pass

    if host not in ("0.0.0.0", "::"):
        return f"{scheme}://{host}:{port}"

    try:
        result = subprocess.run(
            ["tailscale", "status", "--self", "--json"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            dns_name = data.get("Self", {}).get("DNSName", "")
            if dns_name:
                return f"{scheme}://{dns_name.rstrip('.')}:{port}"
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
        pass

    hostname = socket.getfqdn() or socket.gethostname()
    return f"{scheme}://{hostname}:{port}"
