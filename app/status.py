"""Build a status snapshot of all certificates and targets (for MQTT / Home Assistant)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from cryptography import x509

from .config import Config
from .issuer import IssueError, _is_staging_cert, load_bundle, renewal_due_at

if TYPE_CHECKING:
    from .runner import State

#: A certificate that is still not renewed this shortly before expiry is a problem
EXPIRY_WARNING = timedelta(days=14)

OK = "ok"
PENDING = "ausstehend"
ERROR = "fehler"


def _iso(moment: datetime | None) -> str | None:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds") if moment else None


def target_status(state: "State", cert: str, target: str, fingerprint: str | None) -> tuple[str, str | None]:
    """(status, error) of one target for the current certificate."""
    if fingerprint and state.deployed(cert, target) == fingerprint:
        return OK, None
    result = state.result(cert, target)
    if result and not result.get("ok") and result.get("fingerprint") == fingerprint:
        return ERROR, result.get("error")
    return PENDING, None


def build_snapshot(config: Config, state: "State", now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    certs: dict[str, Any] = {}
    problems: list[str] = []

    for cert in config.certs:
        entry: dict[str, Any] = {
            "domains": cert.domains,
            "valid_until": None,
            "issued": None,
            "next_renewal": None,
            "days_left": None,
            "issuer": None,
            "staging": None,
            "targets": {},
            "error": state.issue_error(cert.name),
        }
        fingerprint = None
        try:
            bundle = load_bundle(cert, config.settings)
            parsed = x509.load_pem_x509_certificate(bundle.cert.encode())
            fingerprint = bundle.fingerprint
            remaining = parsed.not_valid_after_utc - now
            entry.update(
                valid_until=_iso(parsed.not_valid_after_utc),
                issued=_iso(parsed.not_valid_before_utc),
                next_renewal=_iso(renewal_due_at(parsed, config.settings)),
                days_left=max(int(remaining.total_seconds() // 86400), 0),
                issuer=parsed.issuer.rfc4514_string(),
                staging=_is_staging_cert(parsed),
            )
            if remaining <= EXPIRY_WARNING:
                problems.append(f"{cert.name}: läuft in {entry['days_left']} Tagen ab")
        except IssueError:
            if not entry["error"]:
                entry["error"] = "noch kein Zertifikat vorhanden"

        if entry["error"]:
            problems.append(f"{cert.name}: {entry['error']}")

        worst = OK
        for target in cert.targets:
            status, error = target_status(state, cert.name, target, fingerprint)
            entry["targets"][target] = status if not error else f"{status}: {error}"
            if status == ERROR:
                problems.append(f"{cert.name} → {target}: {error}")
                worst = ERROR
            elif status == PENDING and worst == OK:
                worst = PENDING
        entry["status"] = ERROR if entry["error"] else worst
        certs[cert.name] = entry

    window = config.settings.deploy_window
    return {
        "last_check": _iso(state.last_cycle()) or _iso(now),
        "deploy_window": str(window) if window else "jederzeit",
        "problem": bool(problems),
        "problems": problems,
        "certs": certs,
    }
