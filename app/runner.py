"""Main loop: issue/renew certificates and deploy them to all targets."""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import Config, ConfigError, load_config
from .deployers import Deployer, build_deployer
from .export import write_bundle
from .issuer import CertBundle, IssueError, issue, load_bundle

log = logging.getLogger("runner")


class State:
    """Remembers which certificate fingerprint was deployed to which target."""

    def __init__(self, data_dir: str):
        self.path = Path(data_dir) / "state.json"
        self.status_path = Path(data_dir) / "status.json"
        try:
            self.data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.data = {}

    @staticmethod
    def _key(cert: str, target: str) -> str:
        return f"{cert}::{target}"

    def deployed(self, cert: str, target: str) -> str | None:
        return self.data.get(self._key(cert, target))

    def mark(self, cert: str, target: str, fingerprint: str) -> None:
        self.data[self._key(cert, target)] = fingerprint
        self._write(self.path, self.data)

    def write_status(self, ok: bool) -> None:
        self._write(
            self.status_path,
            {"last_cycle": datetime.now(timezone.utc).isoformat(), "ok": ok},
        )

    @staticmethod
    def _write(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        os.replace(tmp, path)


def build_deployers(config: Config) -> dict[str, Deployer]:
    """Instantiate (and thereby validate) every referenced target."""
    return {name: build_deployer(t) for name, t in config.targets.items()}


def run_cycle(
    config: Config,
    deployers: dict[str, Deployer],
    state: State,
    *,
    only: str | None = None,
    force_deploy: bool = False,
    skip_issue: bool = False,
) -> bool:
    """One pass over all certificates. Returns True if everything succeeded."""
    ok = True
    for cert in config.certs:
        if only and cert.name != only:
            continue
        try:
            if not skip_issue:
                issue(cert, config.settings)
            bundle = load_bundle(cert, config.settings)
        except IssueError as exc:
            log.error("[%s] %s", cert.name, exc)
            ok = False
            continue

        log.info("[%s] valid until %s", cert.name, bundle.not_after.date())
        export_to_share(cert.name, bundle, config.settings.share_dir)
        for target_name in cert.targets:
            if not force_deploy and state.deployed(cert.name, target_name) == bundle.fingerprint:
                log.debug("[%s] %s is up to date", cert.name, target_name)
                continue
            log.info("[%s] deploying to %s", cert.name, target_name)
            try:
                deployers[target_name].deploy(bundle)
            except Exception as exc:  # one broken target must not block the others
                log.error("[%s] deploy to %s failed: %s", cert.name, target_name, exc)
                ok = False
                continue
            state.mark(cert.name, target_name, bundle.fingerprint)
    state.write_status(ok)
    return ok


def _config_mtime() -> float:
    path = os.environ.get("CONFIG_FILE", "/share/config/certsync.env")
    try:
        return os.stat(path).st_mtime
    except OSError:
        return 0.0


def _sleep_until_due_or_config_changed(
    seconds: float, baseline_mtime: float | None = None, poll: float = 30.0
) -> None:
    # ``baseline_mtime`` is the file's mtime from *before* the config was loaded, so a
    # write that finishes after a failed (half-written) read is still noticed.
    start_mtime = _config_mtime() if baseline_mtime is None else baseline_mtime
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        time.sleep(min(poll, max(deadline - time.monotonic(), 0)))
        if _config_mtime() != start_mtime:
            log.info("config file changed, running a cycle now")
            time.sleep(5)  # let the editor / Samba finish writing
            return


def export_to_share(name: str, bundle: CertBundle, share_dir: str) -> None:
    """Publish real PEM files to <share>/certs/<name>/ (browsable via Samba)."""
    try:
        write_bundle(bundle, Path(share_dir) / "certs" / name)
    except OSError as exc:
        log.warning("[%s] cannot export to share %s: %s", name, share_dir, exc)


def run_daemon() -> None:
    """Run forever. Never exits because of a missing or broken configuration:
    it waits for the config file on the share to be created or fixed.
    """
    config: Config | None = None
    deployers: dict[str, Deployer] = {}
    while True:
        loaded_mtime = _config_mtime()  # read before loading, see _sleep_until_...
        try:
            new_config = load_config()
            deployers = build_deployers(new_config)
            config = new_config
            log.info("configuration loaded")
        except ConfigError as exc:
            if config is None:
                log.error(
                    "configuration invalid: %s. Waiting for %s to be created or fixed.",
                    exc, os.environ.get("CONFIG_FILE", "/share/config/certsync.env"),
                )
                _sleep_until_due_or_config_changed(600, loaded_mtime)
                continue
            log.error("configuration reload failed, keeping the previous one: %s", exc)

        state = State(config.settings.data_dir)
        try:
            run_cycle(config, deployers, state)
        except Exception:  # keep the daemon alive
            log.exception("unexpected error in cycle")

        interval = max(config.settings.check_interval_hours, 0.1) * 3600
        log.info("next check in %.1f hours (or when the config file changes)", interval / 3600)
        _sleep_until_due_or_config_changed(interval, loaded_mtime)
