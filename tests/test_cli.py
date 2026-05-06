"""Tests for the CLI.

Focus: the daemon subcommand group's wiring and the --standalone flag's
plumbing. End-to-end CLI flows that spawn real daemons are not exercised here;
the underlying client / cache / daemon modules have their own tests.
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from serve_review import cache, cli
from serve_review.models import compute_diff_hash

if TYPE_CHECKING:
    from serve_review.daemon import DaemonServer
    from serve_review.models import ReviewRequest


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect CACHE_DIR to a per-test tmp dir so tests can't see each other."""
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    return tmp_path


class TestCliStructure:
    """Static structural checks on the CLI."""

    def test_daemon_group_registered(self) -> None:
        assert "daemon" in cli.main.commands
        daemon_group = cli.main.commands["daemon"]
        # daemon is a click.Group with start/stop/status subcommands.
        assert hasattr(daemon_group, "commands")
        assert {"start", "stop", "status"}.issubset(set(daemon_group.commands))

    def test_run_review_accepts_standalone_kwarg(self) -> None:
        sig = inspect.signature(cli._run_review)
        assert "standalone" in sig.parameters
        assert sig.parameters["standalone"].default is False


class TestDaemonStatus:
    def test_no_daemons(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("serve_review.cache.list_daemons", lambda: [])
        runner = CliRunner()
        result = runner.invoke(cli.main, ["daemon", "status"])
        assert result.exit_code == 0, result.output
        assert "No daemons running." in result.output

    def test_lists_running_daemons(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("serve_review.cache.list_daemons", lambda: [(8567, 12345)])
        monkeypatch.setattr(cli, "_query_queue_depth", lambda port: 3)
        # Avoid network/subprocess calls in URL build.
        monkeypatch.setattr(cli, "_build_review_url", lambda host, port: f"http://h:{port}")
        runner = CliRunner()
        result = runner.invoke(cli.main, ["daemon", "status"])
        assert result.exit_code == 0, result.output
        assert "1 daemon(s) running:" in result.output
        assert "port 8567" in result.output
        assert "pid 12345" in result.output
        assert "3 review(s) queued" in result.output

    def test_queue_unavailable_when_query_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("serve_review.cache.list_daemons", lambda: [(9000, 99)])
        monkeypatch.setattr(cli, "_query_queue_depth", lambda port: None)
        monkeypatch.setattr(cli, "_build_review_url", lambda host, port: f"http://h:{port}")
        runner = CliRunner()
        result = runner.invoke(cli.main, ["daemon", "status"])
        assert result.exit_code == 0, result.output
        assert "queue unavailable" in result.output


class TestDaemonStop:
    def test_stop_specific_port(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("serve_review.cache.read_pid_file", lambda port: 12345)
        killed: list[int] = []
        monkeypatch.setattr(
            "serve_review.client.kill_daemon",
            lambda port: killed.append(port),
        )
        runner = CliRunner()
        result = runner.invoke(cli.main, ["daemon", "stop", "--port", "9000"])
        assert result.exit_code == 0, result.output
        assert killed == [9000]
        assert "Stopped daemon on port 9000." in result.output

    def test_stop_no_daemon(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("serve_review.cache.read_pid_file", lambda port: None)
        # kill_daemon must not be called when no daemon is running.
        called: list[int] = []
        monkeypatch.setattr(
            "serve_review.client.kill_daemon",
            lambda port: called.append(port),
        )
        runner = CliRunner()
        result = runner.invoke(cli.main, ["daemon", "stop"])
        assert result.exit_code == 1
        assert called == []
        assert "No daemon running on port" in result.output

    def test_stop_all(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "serve_review.cache.list_daemons",
            lambda: [(8567, 1), (9000, 2)],
        )
        killed: list[int] = []
        monkeypatch.setattr(
            "serve_review.client.kill_daemon",
            lambda port: killed.append(port),
        )
        runner = CliRunner()
        result = runner.invoke(cli.main, ["daemon", "stop", "--all"])
        assert result.exit_code == 0, result.output
        assert killed == [8567, 9000]
        assert "Stopped daemon on port 8567." in result.output
        assert "Stopped daemon on port 9000." in result.output

    def test_stop_all_no_daemons(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("serve_review.cache.list_daemons", lambda: [])
        called: list[int] = []
        monkeypatch.setattr(
            "serve_review.client.kill_daemon",
            lambda port: called.append(port),
        )
        runner = CliRunner()
        result = runner.invoke(cli.main, ["daemon", "stop", "--all"])
        assert result.exit_code == 0, result.output
        assert called == []
        assert "No daemons running." in result.output


class TestInstallHookCmd:
    def test_install_hook_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, object] = {}

        class FakeResult:
            path = Path("/tmp/fake/.git/hooks/pre-push")
            chained = False
            message = ""

        def fake_install(force: bool = False) -> FakeResult:
            captured["force"] = force
            return FakeResult()

        monkeypatch.setattr("serve_review.hooks.install_pre_push_hook", fake_install)
        runner = CliRunner()
        result = runner.invoke(cli.main, ["install-hook"])
        assert result.exit_code == 0, result.output
        assert captured["force"] is False
        assert "Installed pre-push hook at" in result.output
        assert str(FakeResult.path) in result.output

    def test_install_hook_force(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, object] = {}

        class FakeResult:
            path = Path("/tmp/fake/.git/hooks/pre-push")
            chained = True
            message = "Original hook backed up to /tmp/fake/.git/hooks/pre-push.original"

        def fake_install() -> FakeResult:
            captured["called"] = True
            return FakeResult()

        monkeypatch.setattr("serve_review.hooks.install_pre_push_hook", fake_install)
        runner = CliRunner()
        # --force is accepted as a deprecated no-op flag.
        result = runner.invoke(cli.main, ["install-hook", "--force"])
        assert result.exit_code == 0, result.output
        assert captured["called"] is True
        assert "Original hook backed up to" in result.output

    def test_install_hook_file_exists_error_exits_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_install() -> object:
            raise FileExistsError("Pre-push hook already exists at /tmp/x.")

        monkeypatch.setattr("serve_review.hooks.install_pre_push_hook", fake_install)
        runner = CliRunner()
        result = runner.invoke(cli.main, ["install-hook"])
        assert result.exit_code == 1
        # The error message goes to stderr (mix_stderr=True merges it into output by default).
        assert "Pre-push hook already exists" in result.output


class TestUninstallHookCmd:
    def test_uninstall_when_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("serve_review.hooks.uninstall_pre_push_hook", lambda: True)
        runner = CliRunner()
        result = runner.invoke(cli.main, ["uninstall-hook"])
        assert result.exit_code == 0, result.output
        assert "Removed pre-push hook." in result.output

    def test_uninstall_when_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("serve_review.hooks.uninstall_pre_push_hook", lambda: False)
        runner = CliRunner()
        result = runner.invoke(cli.main, ["uninstall-hook"])
        assert result.exit_code == 1
        assert "No serve-review pre-push hook found." in result.output


class TestPreCommitConfigCmd:
    def test_outputs_yaml_snippet(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli.main, ["pre-commit-config"])
        assert result.exit_code == 0, result.output
        assert "serve-review hook" in result.output
        # The CLI also prints follow-up guidance for installation.
        assert "pre-commit install" in result.output


class TestInstallClaudeHookCmd:
    def test_install_claude_hook_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, object] = {}

        def fake_install(port: int = 8567, global_: bool = False) -> Path:
            captured["port"] = port
            captured["global_"] = global_
            return Path("/fake/.claude/settings.json")

        monkeypatch.setattr("serve_review.hooks.install_claude_code_hook", fake_install)
        runner = CliRunner()
        result = runner.invoke(cli.main, ["install-claude-hook"])
        assert result.exit_code == 0, result.output
        assert captured == {"port": 8567, "global_": False}
        assert "/fake/.claude/settings.json" in result.output
        assert "(project)" in result.output

    def test_install_claude_hook_global(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, object] = {}

        def fake_install(port: int = 8567, global_: bool = False) -> Path:
            captured["port"] = port
            captured["global_"] = global_
            return Path("/home/u/.claude/settings.json")

        monkeypatch.setattr("serve_review.hooks.install_claude_code_hook", fake_install)
        runner = CliRunner()
        result = runner.invoke(cli.main, ["install-claude-hook", "--global"])
        assert result.exit_code == 0, result.output
        assert captured["global_"] is True
        assert captured["port"] == 8567
        assert "(global)" in result.output

    def test_install_claude_hook_custom_port(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, object] = {}

        def fake_install(port: int = 8567, global_: bool = False) -> Path:
            captured["port"] = port
            captured["global_"] = global_
            return Path("/fake/.claude/settings.json")

        monkeypatch.setattr("serve_review.hooks.install_claude_code_hook", fake_install)
        runner = CliRunner()
        result = runner.invoke(cli.main, ["install-claude-hook", "--port", "9000"])
        assert result.exit_code == 0, result.output
        assert captured["port"] == 9000
        assert captured["global_"] is False


class TestQueryQueueDepth:
    async def test_returns_one_when_review_queued(
        self,
        live_daemon: tuple[DaemonServer, int],
        sample_review: ReviewRequest,
    ) -> None:
        server, port = live_daemon
        # Submit a review through the daemon's queue directly so /api/health
        # reflects a non-zero queued count.
        diff_hash = compute_diff_hash(sample_review.files)
        server.queue.submit(sample_review, diff_hash)

        depth = await asyncio.get_event_loop().run_in_executor(None, cli._query_queue_depth, port)
        assert depth == 1

    def test_returns_none_when_daemon_unreachable(self) -> None:
        # Pick an unbound port; urlopen will fail and the helper returns None.
        import socket

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
        sock.close()

        assert cli._query_queue_depth(port) is None


class TestCertCli:
    """Tests for the cert subcommand group."""

    def test_cert_status_no_cert(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(cli.main, ["cert", "status"])
        assert result.exit_code == 0
        assert "No certificate provisioned." in result.output

    def test_cert_renew_fails_when_tailscale_disconnected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from serve_review.cert_manager import TailscaleDetector

        monkeypatch.setattr(TailscaleDetector, "is_connected", lambda self: False)

        runner = CliRunner()
        result = runner.invoke(cli.main, ["cert", "renew"])
        assert result.exit_code == 1
        assert "Tailscale is not connected" in result.output

    def test_cert_forget_no_cache(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(cli.main, ["cert", "forget"])
        assert result.exit_code == 0
        assert "No certificate cache found." in result.output

    def test_cert_forget_removes_files(self, tmp_path: Path) -> None:
        certs_dir = tmp_path / "certs"
        certs_dir.mkdir()
        (certs_dir / "host.ts.net.crt").write_text("dummy")
        (certs_dir / "host.ts.net.key").write_text("dummy")

        runner = CliRunner()
        result = runner.invoke(cli.main, ["cert", "forget"])
        assert result.exit_code == 0
        assert "Deleted certificate cache" in result.output
        assert not certs_dir.exists()

    def test_cert_forget_refuses_symlink(self, tmp_path: Path) -> None:
        # Create a symlink at the certs_dir path; cert forget should refuse.
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        certs_dir = tmp_path / "certs"
        certs_dir.symlink_to(elsewhere)

        runner = CliRunner()
        result = runner.invoke(cli.main, ["cert", "forget"])
        assert result.exit_code == 1
        assert "Refusing to follow symlink" in result.output


class TestDaemonStartFlag:
    """Tests for daemon start --disable-tailscale flag plumbing."""

    def test_disable_tailscale_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, object] = {}

        def fake_run_daemon(
            host: str, port: int, disable_tailscale: bool | None = None
        ) -> None:
            captured["disable_tailscale"] = disable_tailscale

        monkeypatch.setattr("serve_review.daemon.run_daemon", fake_run_daemon)
        runner = CliRunner()
        result = runner.invoke(
            cli.main, ["daemon", "start", "--disable-tailscale"]
        )
        assert result.exit_code == 0, result.output
        assert captured["disable_tailscale"] is True
