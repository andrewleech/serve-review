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
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path  # noqa: TC003
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

# Re-query Tailscale connectivity at most every NEGATIVE_CACHE_TTL seconds when
# the previous result was "not connected". Avoids permanent stuck state when
# the daemon starts before Tailscale comes up.
_NEGATIVE_CACHE_TTL = 300.0


class TailscaleDetector:
    """Detects Tailscale installation and connectivity."""

    def __init__(
        self, runner: Callable[..., subprocess.CompletedProcess[str]] | None = None
    ) -> None:
        self._installed: bool | None = None
        self._status_data: dict[str, Any] | None = None
        self._status_last_attempt: float = 0.0
        self.runner = runner or subprocess.run

    def is_installed(self) -> bool:
        """Check if tailscale binary is in PATH."""
        if self._installed is not None:
            return self._installed

        self._installed = shutil.which("tailscale") is not None
        return self._installed

    def _get_status(self) -> dict[str, Any] | None:
        """Fetch tailscale status, caching positive results and TTL'ing negatives."""
        # Positive result: cache for the lifetime of the detector — Tailscale
        # connectivity rarely flaps once established.
        if self._status_data is not None:
            return self._status_data

        # Negative result: re-check after _NEGATIVE_CACHE_TTL seconds so a daemon
        # started before Tailscale comes up will eventually pick up connectivity.
        now = time.monotonic()
        if self._status_last_attempt and (now - self._status_last_attempt) < _NEGATIVE_CACHE_TTL:
            return None

        self._status_last_attempt = now

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
        certs_dir: Path,
        disable: bool = False,
        detector: TailscaleDetector | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        renewal_days_threshold: int = 30,
    ) -> None:
        self.certs_dir = certs_dir
        self.disable = disable
        self.detector = detector or TailscaleDetector(runner=runner)
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
        Validates the resulting cert/key parse correctly before installing
        atomically; a malformed payload leaves the previous valid pair intact.
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

            # umask 0o077 protects key/cert during the window between Tailscale
            # writing them and our chmod below. The cert is widened to 0o644
            # afterwards (public certs are world-readable); the key stays 0o600.
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

            if not _validate_pem_pair(crt_temp, key_temp):
                logger.error("Certificate or key failed to parse; refusing to install")
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

            self._cleanup_stale_certs(hostname)

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

    def _cleanup_stale_certs(self, current_hostname: str) -> None:
        """Remove any cert/key pairs that don't match the current hostname.

        Useful after a Tailscale hostname change so the daemon doesn't pick up
        an old cert via filesystem-order glob iteration.
        """
        for path in list(self.certs_dir.glob("*.crt")) + list(self.certs_dir.glob("*.key")):
            if path.stem != current_hostname:
                path.unlink(missing_ok=True)

    def check_renewal_needed(self) -> bool:
        """Check if the current certificate needs renewal.

        Returns True if the cert expires within the renewal threshold
        (default 30 days) or has already expired. Returns False if the cert
        doesn't exist or cannot be read.
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
                from cryptography import x509
                from cryptography.hazmat.backends import (
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
            if expiry <= now:
                logger.info("Certificate already expired; renewal needed")
                return True

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

        Prefers the cert matching the current Tailscale hostname when available;
        falls back to the first matching pair found via glob (allowing operation
        when Tailscale is offline). Caches the hostname so callers don't need to
        re-query Tailscale.
        """
        if not self.certs_dir.exists():
            return None, None

        # Prefer the current Tailscale hostname when reachable.
        hostname = self.detector.get_hostname()
        if hostname:
            crt_path = self.certs_dir / f"{hostname}.crt"
            key_path = self.certs_dir / f"{hostname}.key"
            if crt_path.exists() and key_path.exists():
                self._hostname = hostname
                return crt_path, key_path

        for crt_path in self.certs_dir.glob("*.crt"):
            basename = crt_path.stem
            key_path = self.certs_dir / f"{basename}.key"
            if key_path.exists():
                self._hostname = basename
                return crt_path, key_path

        return None, None


def _validate_pem_pair(crt_path: Path, key_path: Path) -> bool:
    """Best-effort parse of the cert and key. Returns False if cryptography
    is unavailable or either file fails to load."""
    try:
        from cryptography import x509
        from cryptography.hazmat.backends import (
            default_backend,
        )
        from cryptography.hazmat.primitives import (
            serialization,
        )
    except ImportError:
        # Without cryptography we can't validate; fall back to size checks.
        return True

    try:
        x509.load_pem_x509_certificate(crt_path.read_bytes(), default_backend())
        serialization.load_pem_private_key(
            key_path.read_bytes(), password=None, backend=default_backend()
        )
    except Exception:
        return False
    return True
