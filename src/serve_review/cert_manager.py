"""Tailscale TLS certificate detection, provisioning, and renewal.

This module handles automatic HTTPS certificate provisioning via Tailscale's
cert command when Tailscale is available. Certificates are stored in the
application cache and automatically renewed before expiry.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path  # noqa: TC003
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


class TailscaleDetector:
    """Detects Tailscale installation and connectivity."""

    def __init__(
        self, runner: Callable[..., subprocess.CompletedProcess[str]] | None = None
    ) -> None:
        self._installed: bool | None = None
        self._status_data: dict[str, Any] | None = None
        self._status_tried: bool = False
        self.runner = runner or subprocess.run

    def is_installed(self) -> bool:
        """Check if tailscale binary is in PATH."""
        if self._installed is not None:
            return self._installed

        self._installed = shutil.which("tailscale") is not None
        return self._installed

    def _get_status(self) -> dict[str, Any] | None:
        """Fetch tailscale status once and cache it."""
        if self._status_tried:
            return self._status_data

        self._status_tried = True

        if not self.is_installed():
            return None

        try:
            result = self.runner(
                ["tailscale", "status", "--self", "--json"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                self._status_data = json.loads(result.stdout)
                return self._status_data
        except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError, ValueError):
            pass

        return None

    def is_connected(self) -> bool:
        """Check if Tailscale is running and connected."""
        status = self._get_status()
        if not status:
            return False

        self_info = status.get("Self", {})
        return bool(self_info.get("DNSName"))

    def get_hostname(self) -> str | None:
        """Get the Tailscale hostname for this machine (e.g., 'myhost.ts.net').

        Returns None if Tailscale is not connected.
        """
        status = self._get_status()
        if not status:
            return None

        self_info = status.get("Self", {})
        if not isinstance(self_info, dict):
            return None

        dns_name = self_info.get("DNSName", "")
        if isinstance(dns_name, str) and dns_name:
            return str(dns_name.rstrip("."))

        return None


class CertificateManager:
    """Manages Tailscale TLS certificate provisioning and renewal.

    Provides automatic provisioning of TLS certificates via Tailscale's cert
    command, with renewal checking when certificates approach expiry.
    """

    def __init__(
        self,
        cache_dir: Path,
        disable: bool = False,
        detector: TailscaleDetector | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        renewal_days_threshold: int = 30,
    ) -> None:
        self.cache_dir = cache_dir
        self.disable = disable
        self.detector = detector or TailscaleDetector(runner=runner)
        self.certs_dir = cache_dir / "certs"
        self.renewal_days_threshold = renewal_days_threshold
        self.runner = runner or subprocess.run
        self._hostname: str | None = None

    def _cleanup_temp_files(self, hostname: str, pid: int) -> None:
        """Remove temporary cert and key files."""
        crt_temp = self.certs_dir / f"{hostname}.crt.tmp.{pid}"
        key_temp = self.certs_dir / f"{hostname}.key.tmp.{pid}"
        crt_temp.unlink(missing_ok=True)
        key_temp.unlink(missing_ok=True)

    def should_provision(self) -> bool:
        """Return True if we should attempt to provision certificates.

        This checks:
        1. Feature is enabled (not disabled)
        2. Tailscale is connected
        3. Certs don't exist, OR certs exist but need renewal
        """
        if self.disable:
            return False

        if not self.detector.is_connected():
            return False

        hostname = self.detector.get_hostname()
        if not hostname:
            return False

        crt_path = self.certs_dir / f"{hostname}.crt"
        key_path = self.certs_dir / f"{hostname}.key"

        if not (crt_path.exists() and key_path.exists()):
            return True

        return self.check_renewal_needed()

    def provision(self) -> bool:
        """Provision new TLS certificates using tailscale cert command.

        Returns True on success, False on failure. All errors are logged.
        """
        hostname = self.detector.get_hostname()
        if not hostname:
            logger.warning("Cannot provision certs: Tailscale not connected")
            return False

        self._hostname = hostname

        try:
            self.certs_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        except Exception as exc:
            logger.error("Failed to create certs directory: %s", exc, exc_info=True)
            return False

        crt_path = self.certs_dir / f"{hostname}.crt"
        key_path = self.certs_dir / f"{hostname}.key"

        pid = os.getpid()
        crt_temp = self.certs_dir / f"{hostname}.crt.tmp.{pid}"
        key_temp = self.certs_dir / f"{hostname}.key.tmp.{pid}"

        try:
            logger.info("Provisioning Tailscale certificate for %s", hostname)

            old_umask = os.umask(0o077)
            try:
                result = self.runner(
                    [
                        "tailscale",
                        "cert",
                        f"--cert-file={crt_temp}",
                        f"--key-file={key_temp}",
                        hostname,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
            finally:
                os.umask(old_umask)

            if result.returncode != 0:
                stderr_msg = result.stderr.strip() if result.stderr else f"rc={result.returncode}"
                logger.error("tailscale cert failed: %s", stderr_msg)
                self._cleanup_temp_files(hostname, pid)
                return False

            if not crt_temp.exists() or crt_temp.stat().st_size == 0:
                logger.error("Certificate file is empty or missing")
                self._cleanup_temp_files(hostname, pid)
                return False

            if not key_temp.exists() or key_temp.stat().st_size == 0:
                logger.error("Key file is empty or missing")
                self._cleanup_temp_files(hostname, pid)
                return False

            crt_temp.chmod(0o644)
            key_temp.chmod(0o600)

            crt_temp.replace(crt_path)
            key_temp.replace(key_path)

            # Ensure directory metadata is written to disk
            try:
                certs_fd = os.open(str(self.certs_dir), os.O_RDONLY)
                os.fsync(certs_fd)
                os.close(certs_fd)
            except OSError:
                pass

            logger.info("Successfully provisioned certificate for %s", hostname)
            return True

        except subprocess.TimeoutExpired:
            logger.error("tailscale cert command timed out")
            self._cleanup_temp_files(hostname, pid)
            return False
        except Exception:
            logger.exception("Error provisioning certificate")
            self._cleanup_temp_files(hostname, pid)
            return False

    def check_renewal_needed(self) -> bool:
        """Check if the current certificate needs renewal.

        Returns True if the cert expires within the renewal threshold
        (default 30 days). Returns False if the cert doesn't exist or
        cannot be read.
        """
        hostname = self._hostname or self.detector.get_hostname()
        if not hostname:
            return False

        crt_path = self.certs_dir / f"{hostname}.crt"
        if not crt_path.exists():
            return False

        try:
            # Import here to defer dependency check
            try:
                from cryptography import x509  # type: ignore[import-not-found]
                from cryptography.hazmat.backends import (  # type: ignore[import-not-found]
                    default_backend,
                )
            except ImportError as import_err:
                logger.error(
                    "cryptography library required for certificate renewal: %s", import_err
                )
                return False

            with open(crt_path, "rb") as f:
                cert_data = f.read()

            cert = x509.load_pem_x509_certificate(cert_data, default_backend())
            expiry = cert.not_valid_after_utc

            now = datetime.now(UTC)
            renewal_threshold = now + timedelta(days=self.renewal_days_threshold)

            needs_renewal: bool = expiry < renewal_threshold
            if needs_renewal:
                days_left = (expiry - now).days
                logger.info("Certificate expires in %d days, renewal needed", days_left)

            return bool(needs_renewal)

        except Exception:
            logger.exception("Error checking certificate expiry")
            return False

    def renew(self) -> bool:
        """Renew the certificate.

        Returns True on success, False on failure. Errors are logged.
        """
        logger.info("Attempting certificate renewal")
        return self.provision()

    def get_cert_paths(self) -> tuple[Path | None, Path | None]:
        """Return (cert_path, key_path) if they exist, otherwise (None, None).

        Scans the certs directory for any .crt file, allowing operation even
        if Tailscale is offline (e.g., on restart before network is up).
        Also caches the hostname to avoid re-querying Tailscale.
        """
        if not self.certs_dir.exists():
            return None, None

        for crt_path in self.certs_dir.glob("*.crt"):
            basename = crt_path.stem
            key_path = self.certs_dir / f"{basename}.key"
            if key_path.exists():
                self._hostname = basename
                return crt_path, key_path

        return None, None
