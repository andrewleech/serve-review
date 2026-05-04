"""Tests for Tailscale certificate management."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest

from serve_review.cert_manager import CertificateManager, TailscaleDetector


class TestTailscaleDetector:
    """Tests for Tailscale detection."""

    def test_is_installed_returns_false_when_not_in_path(self) -> None:
        """Tailscale detection fails gracefully when not installed."""
        detector = TailscaleDetector()
        with patch("shutil.which", return_value=None):
            assert detector.is_installed() is False

    def test_is_installed_returns_true_when_in_path(self) -> None:
        """Tailscale detection succeeds when binary is found."""
        detector = TailscaleDetector()
        with patch("shutil.which", return_value="/usr/bin/tailscale"):
            assert detector.is_installed() is True

    def test_is_connected_returns_false_when_not_installed(self) -> None:
        """Connection check fails if Tailscale is not installed."""
        detector = TailscaleDetector()
        with patch.object(detector, "is_installed", return_value=False):
            assert detector.is_connected() is False

    def test_is_connected_returns_false_on_subprocess_error(self) -> None:
        """Connection check fails gracefully on subprocess error."""
        detector = TailscaleDetector(runner=Mock(side_effect=FileNotFoundError()))
        with patch.object(detector, "is_installed", return_value=True):
            assert detector.is_connected() is False

    def test_is_connected_returns_false_on_json_error(self) -> None:
        """Connection check fails gracefully on invalid JSON."""
        result = subprocess.CompletedProcess(
            ["tailscale", "status", "--self", "--json"], 0, "invalid json", ""
        )
        detector = TailscaleDetector(runner=Mock(return_value=result))
        with patch.object(detector, "is_installed", return_value=True):
            assert detector.is_connected() is False

    def test_is_connected_returns_false_when_no_dns_name(self) -> None:
        """Connection check fails if machine has no DNSName."""
        result = subprocess.CompletedProcess(
            ["tailscale", "status", "--self", "--json"],
            0,
            json.dumps({"Self": {}}),
            "",
        )
        detector = TailscaleDetector(runner=Mock(return_value=result))
        with patch.object(detector, "is_installed", return_value=True):
            assert detector.is_connected() is False

    def test_is_connected_returns_true_when_dns_name_present(self) -> None:
        """Connection check succeeds when DNSName is present."""
        result = subprocess.CompletedProcess(
            ["tailscale", "status", "--self", "--json"],
            0,
            json.dumps({"Self": {"DNSName": "machine.ts.net."}}),
            "",
        )
        detector = TailscaleDetector(runner=Mock(return_value=result))
        with patch.object(detector, "is_installed", return_value=True):
            assert detector.is_connected() is True

    def test_get_hostname_returns_hostname_without_trailing_dot(self) -> None:
        """Hostname is returned with trailing dot stripped."""
        result = subprocess.CompletedProcess(
            ["tailscale", "status", "--self", "--json"],
            0,
            json.dumps({"Self": {"DNSName": "myhost.ts.net."}}),
            "",
        )
        detector = TailscaleDetector(runner=Mock(return_value=result))
        with patch.object(detector, "is_installed", return_value=True):
            assert detector.get_hostname() == "myhost.ts.net"


class TestCertificateManager:
    """Tests for certificate management."""

    def test_should_provision_returns_false_when_disabled(self, tmp_path: Path) -> None:
        """Provisioning is skipped when disabled."""
        manager = CertificateManager(tmp_path, disable=True)
        with patch.object(manager.detector, "is_connected", return_value=True):
            assert manager.should_provision() is False

    def test_should_provision_returns_false_when_not_connected(self, tmp_path: Path) -> None:
        """Provisioning is skipped when Tailscale is not connected."""
        manager = CertificateManager(tmp_path)
        with patch.object(manager.detector, "is_connected", return_value=False):
            assert manager.should_provision() is False

    def test_should_provision_returns_true_when_certs_missing(self, tmp_path: Path) -> None:
        """Provisioning is triggered when certs don't exist."""
        manager = CertificateManager(tmp_path)
        with patch.object(manager.detector, "is_connected", return_value=True):
            with patch.object(manager.detector, "get_hostname", return_value="host.ts.net"):
                assert manager.should_provision() is True

    def test_should_provision_returns_false_when_certs_exist_and_valid(
        self, tmp_path: Path
    ) -> None:
        """Provisioning is skipped when valid certs exist."""
        certs_dir = tmp_path / "certs"
        certs_dir.mkdir()
        (certs_dir / "host.ts.net.crt").touch()
        (certs_dir / "host.ts.net.key").touch()

        manager = CertificateManager(tmp_path)
        with patch.object(manager.detector, "is_connected", return_value=True):
            with patch.object(manager.detector, "get_hostname", return_value="host.ts.net"):
                with patch.object(manager, "check_renewal_needed", return_value=False):
                    assert manager.should_provision() is False

    def test_get_cert_paths_returns_none_when_no_certs(self, tmp_path: Path) -> None:
        """get_cert_paths returns (None, None) when no certs exist."""
        manager = CertificateManager(tmp_path)
        assert manager.get_cert_paths() == (None, None)

    def test_get_cert_paths_returns_cert_paths_when_exist(self, tmp_path: Path) -> None:
        """get_cert_paths returns paths when certs exist."""
        certs_dir = tmp_path / "certs"
        certs_dir.mkdir()
        crt = certs_dir / "host.ts.net.crt"
        key = certs_dir / "host.ts.net.key"
        crt.touch()
        key.touch()

        manager = CertificateManager(tmp_path)
        crt_path, key_path = manager.get_cert_paths()
        assert crt_path == crt
        assert key_path == key

    def test_provision_returns_false_when_not_connected(self, tmp_path: Path) -> None:
        """Provision fails gracefully when Tailscale is not connected."""
        manager = CertificateManager(tmp_path)
        with patch.object(manager.detector, "get_hostname", return_value=None):
            assert manager.provision() is False

    def test_provision_returns_false_on_subprocess_error(self, tmp_path: Path) -> None:
        """Provision fails gracefully on subprocess error."""
        result = subprocess.CompletedProcess(
            ["tailscale", "cert"], 1, "", "permission denied"
        )
        manager = CertificateManager(tmp_path, runner=Mock(return_value=result))
        with patch.object(manager.detector, "get_hostname", return_value="host.ts.net"):
            assert manager.provision() is False

    def test_provision_returns_false_on_timeout(self, tmp_path: Path) -> None:
        """Provision fails gracefully on timeout."""
        manager = CertificateManager(
            tmp_path, runner=Mock(side_effect=subprocess.TimeoutExpired("cmd", 30))
        )
        with patch.object(manager.detector, "get_hostname", return_value="host.ts.net"):
            assert manager.provision() is False

    def test_check_renewal_needed_returns_false_when_no_cert(self, tmp_path: Path) -> None:
        """Renewal check returns False when cert doesn't exist."""
        manager = CertificateManager(tmp_path)
        with patch.object(manager.detector, "get_hostname", return_value="host.ts.net"):
            assert manager.check_renewal_needed() is False

    def test_check_renewal_needed_returns_false_on_import_error(self, tmp_path: Path) -> None:
        """Renewal check handles missing cryptography gracefully."""
        certs_dir = tmp_path / "certs"
        certs_dir.mkdir()
        (certs_dir / "host.ts.net.crt").write_text("fake cert")

        manager = CertificateManager(tmp_path)
        with patch.object(manager.detector, "get_hostname", return_value="host.ts.net"):
            with patch("builtins.__import__", side_effect=ImportError()):
                assert manager.check_renewal_needed() is False

    def test_renew_calls_provision(self, tmp_path: Path) -> None:
        """Renew delegates to provision."""
        manager = CertificateManager(tmp_path)
        with patch.object(manager, "provision", return_value=True) as mock_provision:
            assert manager.renew() is True
            mock_provision.assert_called_once()
