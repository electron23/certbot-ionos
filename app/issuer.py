"""Obtain / renew certificates with certbot (dns-ionos plugin)."""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509

from .config import CertConfig, Settings

log = logging.getLogger("issuer")

STAGING_URL = "https://acme-staging-v02.api.letsencrypt.org/directory"


class IssueError(Exception):
    pass


@dataclass(frozen=True)
class CertBundle:
    """PEM material of an issued certificate."""

    name: str
    domains: list[str]
    fullchain: str
    privkey: str
    cert: str
    chain: str

    @property
    def fingerprint(self) -> str:
        """Identifies this exact certificate + key (used to detect renewals)."""
        digest = hashlib.sha256()
        digest.update(self.fullchain.encode())
        digest.update(self.privkey.encode())
        return digest.hexdigest()

    @property
    def not_after(self):
        return x509.load_pem_x509_certificate(self.cert.encode()).not_valid_after_utc


def _dirs(settings: Settings) -> dict[str, Path]:
    base = Path(settings.data_dir)
    return {
        "config": base / "letsencrypt",
        "work": base / "work",
        "logs": base / "logs",
        "secrets": base / "secrets",
    }


def _write_credentials(settings: Settings) -> Path:
    secrets = _dirs(settings)["secrets"]
    secrets.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = secrets / "ionos.ini"
    content = (
        f"dns_ionos_prefix = {settings.ionos_prefix}\n"
        f"dns_ionos_secret = {settings.ionos_secret}\n"
        f"dns_ionos_endpoint = {settings.ionos_endpoint}\n"
    )
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(content)
    return path


def _issue_reason(cert: CertConfig, settings: Settings) -> str | None:
    """Return why (re-)issuance is needed, or None if the current cert is fine."""
    try:
        bundle = load_bundle(cert, settings)
    except IssueError:
        return "no certificate yet"

    parsed = x509.load_pem_x509_certificate(bundle.cert.encode())
    try:
        san = parsed.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        present = {n.lower() for n in san.value.get_values_for_type(x509.DNSName)}
    except x509.ExtensionNotFound:
        present = set()
    missing = {d.lower() for d in cert.domains} - present
    if missing:
        return f"domains not covered yet: {', '.join(sorted(missing))}"

    remaining = parsed.not_valid_after_utc - datetime.now(timezone.utc)
    if remaining <= timedelta(days=settings.renew_days_before_expiry):
        return f"expires in {remaining.days} days"
    log.info("[%s] valid for %d more days, no renewal needed", cert.name, remaining.days)
    return None


def issue(cert: CertConfig, settings: Settings) -> None:
    """Request the certificate, or renew it when close to expiry."""
    reason = _issue_reason(cert, settings)
    if reason is None:
        return
    log.info("[%s] requesting certificate: %s", cert.name, reason)

    dirs = _dirs(settings)
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    creds = _write_credentials(settings)

    cmd = [
        "certbot", "certonly",
        "--non-interactive", "--agree-tos",
        "--email", settings.acme_email,
        "--authenticator", "dns-ionos",
        "--dns-ionos-credentials", str(creds),
        "--dns-ionos-propagation-seconds", str(settings.propagation_seconds),
        "--cert-name", cert.name,
        "--config-dir", str(dirs["config"]),
        "--work-dir", str(dirs["work"]),
        "--logs-dir", str(dirs["logs"]),
        "--key-type", settings.key_type,
        "--force-renewal", "--expand", "--no-eff-email",
    ]
    if settings.acme_server:
        cmd += ["--server", settings.acme_server]
    elif settings.acme_staging:
        cmd += ["--server", STAGING_URL]
    for domain in cert.domains:
        cmd += ["-d", domain]

    log.info("[%s] running certbot for %s", cert.name, ", ".join(cert.domains))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    for line in (proc.stdout + proc.stderr).splitlines():
        if line.strip():
            log.info("[%s] certbot: %s", cert.name, line)
    if proc.returncode != 0:
        raise IssueError(f"certbot exited with code {proc.returncode}")


def load_bundle(cert: CertConfig, settings: Settings) -> CertBundle:
    live = _dirs(settings)["config"] / "live" / cert.name
    try:
        read = lambda f: (live / f).read_text()  # noqa: E731
        return CertBundle(
            name=cert.name,
            domains=cert.domains,
            fullchain=read("fullchain.pem"),
            privkey=read("privkey.pem"),
            cert=read("cert.pem"),
            chain=read("chain.pem"),
        )
    except OSError as exc:
        raise IssueError(f"Certificate files for '{cert.name}' not found: {exc}") from exc
