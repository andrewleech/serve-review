"""Tests for Tailscale certificate management."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import Mock, patch

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
        manager = CertificateManager(tmp_path / "certs", disable=True)
        with patch.object(manager.detector, "is_connected", return_value=True):
            assert manager.should_provision() is False

    def test_should_provision_returns_false_when_not_connected(self, tmp_path: Path) -> None:
        """Provisioning is skipped when Tailscale is not connected."""
        manager = CertificateManager(tmp_path / "certs")
        with patch.object(manager.detector, "is_connected", return_value=False):
            assert manager.should_provision() is False

    def test_should_provision_returns_true_when_certs_missing(self, tmp_path: Path) -> None:
        """Provisioning is triggered when certs don't exist."""
        manager = CertificateManager(tmp_path / "certs")
        with (
            patch.object(manager.detector, "is_connected", return_value=True),
            patch.object(manager.detector, "get_hostname", return_value="host.ts.net"),
        ):
            assert manager.should_provision() is True

    def test_should_provision_returns_false_when_certs_exist_and_valid(
        self, tmp_path: Path
    ) -> None:
        """Provisioning is skipped when valid certs exist."""
        certs_dir = tmp_path / "certs"
        certs_dir.mkdir()
        (certs_dir / "host.ts.net.crt").touch()
        (certs_dir / "host.ts.net.key").touch()

        manager = CertificateManager(tmp_path / "certs")
        with (
            patch.object(manager.detector, "is_connected", return_value=True),
            patch.object(manager.detector, "get_hostname", return_value="host.ts.net"),
            patch.object(manager, "check_renewal_needed", return_value=False),
        ):
            assert manager.should_provision() is False

    def test_get_cert_paths_returns_none_when_no_certs(self, tmp_path: Path) -> None:
        """get_cert_paths returns (None, None) when no certs exist."""
        manager = CertificateManager(tmp_path / "certs")
        assert manager.get_cert_paths() == (None, None)

    def test_get_cert_paths_returns_cert_paths_when_exist(self, tmp_path: Path) -> None:
        """get_cert_paths returns paths when certs exist."""
        certs_dir = tmp_path / "certs"
        certs_dir.mkdir()
        crt = certs_dir / "host.ts.net.crt"
        key = certs_dir / "host.ts.net.key"
        crt.touch()
        key.touch()

        manager = CertificateManager(tmp_path / "certs")
        crt_path, key_path = manager.get_cert_paths()
        assert crt_path == crt
        assert key_path == key

    def test_provision_returns_false_when_not_connected(self, tmp_path: Path) -> None:
        """Provision fails gracefully when Tailscale is not connected."""
        manager = CertificateManager(tmp_path / "certs")
        with patch.object(manager.detector, "get_hostname", return_value=None):
            assert manager.provision() is False

    def test_provision_returns_false_on_subprocess_error(self, tmp_path: Path) -> None:
        """Provision fails gracefully on subprocess error."""
        result = subprocess.CompletedProcess(
            ["tailscale", "cert"], 1, "", "permission denied"
        )
        manager = CertificateManager(tmp_path / "certs", runner=Mock(return_value=result))
        with patch.object(manager.detector, "get_hostname", return_value="host.ts.net"):
            assert manager.provision() is False

    def test_provision_returns_false_on_timeout(self, tmp_path: Path) -> None:
        """Provision fails gracefully on timeout."""
        manager = CertificateManager(
            tmp_path / "certs",
            runner=Mock(side_effect=subprocess.TimeoutExpired("cmd", 30)),
        )
        with patch.object(manager.detector, "get_hostname", return_value="host.ts.net"):
            assert manager.provision() is False

    def test_check_renewal_needed_returns_false_when_no_cert(self, tmp_path: Path) -> None:
        """Renewal check returns False when cert doesn't exist."""
        manager = CertificateManager(tmp_path / "certs")
        with patch.object(manager.detector, "get_hostname", return_value="host.ts.net"):
            assert manager.check_renewal_needed() is False

    def test_check_renewal_needed_returns_false_on_import_error(self, tmp_path: Path) -> None:
        """Renewal check handles missing cryptography gracefully."""
        import sys

        certs_dir = tmp_path / "certs"
        certs_dir.mkdir()
        (certs_dir / "host.ts.net.crt").write_text("fake cert")

        manager = CertificateManager(tmp_path / "certs")
        with (
            patch.object(manager.detector, "get_hostname", return_value="host.ts.net"),
            patch.dict(sys.modules, {"cryptography.x509": None}),
        ):
            assert manager.check_renewal_needed() is False

    def test_renew_calls_provision(self, tmp_path: Path) -> None:
        """Renew delegates to provision."""
        manager = CertificateManager(tmp_path / "certs")
        with patch.object(manager, "provision", return_value=True) as mock_provision:
            assert manager.renew() is True
            mock_provision.assert_called_once()

    def test_provision_happy_path_atomic_install(self, tmp_path: Path) -> None:
        """Provision atomically installs cert/key with correct modes when tailscale succeeds."""
        certs_dir = tmp_path / "certs"
        crt_pem, key_pem = _generate_self_signed_pair("host.ts.net")

        def fake_runner(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            # Tailscale writes the cert/key to the temp paths supplied via flags.
            crt_arg = next(a for a in cmd if a.startswith("--cert-file="))
            key_arg = next(a for a in cmd if a.startswith("--key-file="))
            Path(crt_arg.split("=", 1)[1]).write_bytes(crt_pem)
            Path(key_arg.split("=", 1)[1]).write_bytes(key_pem)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        manager = CertificateManager(certs_dir, runner=fake_runner)
        with patch.object(manager.detector, "get_hostname", return_value="host.ts.net"):
            assert manager.provision() is True

        crt_path = certs_dir / "host.ts.net.crt"
        key_path = certs_dir / "host.ts.net.key"
        assert crt_path.exists()
        assert key_path.exists()
        assert crt_path.read_bytes() == crt_pem
        assert key_path.read_bytes() == key_pem
        # File modes: cert public-readable, key owner-only.
        assert (crt_path.stat().st_mode & 0o777) == 0o644
        assert (key_path.stat().st_mode & 0o777) == 0o600
        # No leftover temp files.
        assert not list(certs_dir.glob("*.tmp.*"))

    def test_provision_rejects_unparseable_pem(self, tmp_path: Path) -> None:
        """Provision refuses to install a cert/key pair that won't parse."""
        certs_dir = tmp_path / "certs"

        def fake_runner(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            crt_arg = next(a for a in cmd if a.startswith("--cert-file="))
            key_arg = next(a for a in cmd if a.startswith("--key-file="))
            Path(crt_arg.split("=", 1)[1]).write_text("not a real cert")
            Path(key_arg.split("=", 1)[1]).write_text("not a real key")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        manager = CertificateManager(certs_dir, runner=fake_runner)
        with patch.object(manager.detector, "get_hostname", return_value="host.ts.net"):
            assert manager.provision() is False
        # No partial install on parse failure.
        assert not (certs_dir / "host.ts.net.crt").exists()
        assert not (certs_dir / "host.ts.net.key").exists()

    def test_renewal_threshold_boundary_29_days(self, tmp_path: Path) -> None:
        """A cert expiring 29 days from now triggers renewal (within 30-day threshold)."""
        self._renewal_boundary_test(tmp_path, days_until_expiry=29, expected=True)

    def test_renewal_threshold_boundary_31_days(self, tmp_path: Path) -> None:
        """A cert expiring 31 days from now does not trigger renewal."""
        self._renewal_boundary_test(tmp_path, days_until_expiry=31, expected=False)

    def test_renewal_already_expired(self, tmp_path: Path) -> None:
        """An already-expired cert triggers renewal."""
        self._renewal_boundary_test(tmp_path, days_until_expiry=-1, expected=True)

    @staticmethod
    def _renewal_boundary_test(tmp_path: Path, days_until_expiry: int, expected: bool) -> None:
        certs_dir = tmp_path / "certs"
        certs_dir.mkdir()
        crt_pem, _ = _generate_self_signed_pair(
            "host.ts.net", days_valid=days_until_expiry
        )
        (certs_dir / "host.ts.net.crt").write_bytes(crt_pem)

        manager = CertificateManager(certs_dir)
        with patch.object(manager.detector, "get_hostname", return_value="host.ts.net"):
            assert manager.check_renewal_needed() is expected


def _generate_self_signed_pair(
    common_name: str, days_valid: int = 90
) -> tuple[bytes, bytes]:
    """Generate a self-signed cert/key pair as PEM bytes.

    Used by happy-path tests that need real cert content the manager will accept.
    """
    from datetime import UTC, datetime, timedelta

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, common_name)]
    )
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=days_valid))
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    crt_pem = cert.public_bytes(serialization.Encoding.PEM)
    return crt_pem, key_pem
