"""Main loop: issue/renew certificates and deploy them to all targets."""

from __future__ import annotations

import json
import logging
import os
import queue
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import Config, ConfigError, load_config
from .deployers import Deployer, build_deployer
from .export import write_bundle
from .issuer import CertBundle, IssueError, issue, load_bundle
from .status import build_snapshot

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

    def result(self, cert: str, target: str) -> dict | None:
        return self.data.get("result::" + self._key(cert, target))

    def set_result(self, cert: str, target: str, fingerprint: str, error: str | None) -> None:
        self.data["result::" + self._key(cert, target)] = {
            "ok": error is None,
            "error": error,
            "fingerprint": fingerprint,
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        self._write(self.path, self.data)

    def issue_error(self, cert: str) -> str | None:
        return self.data.get("issue_error::" + cert)

    def set_issue_error(self, cert: str, error: str | None) -> None:
        key = "issue_error::" + cert
        if error is None:
            if self.data.pop(key, None) is not None:
                self._write(self.path, self.data)
        elif self.data.get(key) != error:
            self.data[key] = error
            self._write(self.path, self.data)

    def last_cycle(self) -> datetime | None:
        try:
            return datetime.fromisoformat(json.loads(self.status_path.read_text())["last_cycle"])
        except (OSError, ValueError, KeyError):
            return None

    def attempted(self, cert: str, target: str) -> str | None:
        return self.data.get("attempt::" + self._key(cert, target))

    def mark_attempt(self, cert: str, target: str, fingerprint: str) -> None:
        self.data["attempt::" + self._key(cert, target)] = fingerprint
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
    in_window: bool | None = None,
    events: list[dict] | None = None,
    force_issue: bool = False,
) -> bool:
    """One pass over all certificates. Returns True if everything succeeded.

    ``in_window`` defaults to "is now inside DEPLOY_WINDOW" (always True without one).
    ``events`` collects notable happenings ("issued", "deployed") for Home Assistant.
    """
    window = config.settings.deploy_window
    if in_window is None:
        in_window = window is None or window.contains(datetime.now(timezone.utc))
    events = [] if events is None else events
    ok = True
    for cert in config.certs:
        if only and cert.name != only:
            continue
        try:
            issued = False if skip_issue else issue(cert, config.settings, force=force_issue)
            bundle = load_bundle(cert, config.settings)
            state.set_issue_error(cert.name, None)
        except IssueError as exc:
            log.error("[%s] %s", cert.name, exc)
            state.set_issue_error(cert.name, str(exc))
            ok = False
            continue
        if issued:
            events.append({
                "event_type": "issued",
                "cert": cert.name,
                "domains": ", ".join(cert.domains),
                "valid_until": bundle.not_after.isoformat(timespec="seconds"),
                "deploy_window": str(window) if window else "jederzeit",
            })
        deployed_now: list[str] = []
        failed_now: list[str] = []

        log.info("[%s] valid until %s", cert.name, bundle.not_after.date())
        export_to_share(cert.name, bundle, config.settings.share_dir)
        # Restart-only targets (Home Assistant apps) depend on files written by other
        # targets, so they always run last regardless of their position in the list.
        ordered = sorted(cert.targets, key=lambda n: getattr(deployers[n], "uploads", True) is False)
        for target_name in ordered:
            deployed = state.deployed(cert.name, target_name)
            if not force_deploy and deployed == bundle.fingerprint:
                log.debug("[%s] %s is up to date", cert.name, target_name)
                continue
            # Deploying usually restarts the target. Outside the window only the very
            # first attempt for a newly configured target runs right away; renewals and
            # retries after a failure wait for the window.
            first_try = deployed is None and state.attempted(cert.name, target_name) is None
            if not (force_deploy or in_window or first_try):
                log.info(
                    "[%s] deploy to %s pending, deferred to the deploy window (%s)",
                    cert.name, target_name, config.settings.deploy_window,
                )
                continue
            log.info("[%s] deploying to %s", cert.name, target_name)
            state.mark_attempt(cert.name, target_name, bundle.fingerprint)
            try:
                deployers[target_name].deploy(bundle)
            except Exception as exc:  # one broken target must not block the others
                log.error("[%s] deploy to %s failed: %s", cert.name, target_name, exc)
                state.set_result(cert.name, target_name, bundle.fingerprint, str(exc)[:300])
                failed_now.append(target_name)
                ok = False
                continue
            state.mark(cert.name, target_name, bundle.fingerprint)
            state.set_result(cert.name, target_name, bundle.fingerprint, None)
            deployed_now.append(target_name)
        if deployed_now:
            events.append({
                "event_type": "deployed",
                "cert": cert.name,
                "domains": ", ".join(cert.domains),
                "valid_until": bundle.not_after.isoformat(timespec="seconds"),
                "targets": ", ".join(deployed_now),
                "failed": ", ".join(failed_now),
            })
    state.write_status(ok)
    return ok


def _config_mtime() -> float:
    path = os.environ.get("CONFIG_FILE", "/share/config/certsync.env")
    try:
        return os.stat(path).st_mtime
    except OSError:
        return 0.0


def _sleep_until_due_or_config_changed(
    seconds: float, baseline_mtime: float | None = None, poll: float = 5.0,
    on_tick=None, tick_seconds: float = 3600.0, commands: "queue.Queue | None" = None,
) -> None:
    # ``baseline_mtime`` is the file's mtime from *before* the config was loaded, so a
    # write that finishes after a failed (half-written) read is still noticed.
    start_mtime = _config_mtime() if baseline_mtime is None else baseline_mtime
    deadline = time.monotonic() + seconds
    next_tick = time.monotonic() + tick_seconds
    while time.monotonic() < deadline:
        time.sleep(min(poll, max(deadline - time.monotonic(), 0)))
        if on_tick is not None and time.monotonic() >= next_tick:
            next_tick = time.monotonic() + tick_seconds
            try:
                on_tick()
            except Exception:  # noqa: BLE001 - status refresh must not stop the daemon
                log.exception("status refresh failed")
        if commands is not None and not commands.empty():
            return
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
    publisher = None
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

        publisher = _ensure_publisher(publisher, config)
        state = State(config.settings.data_dir)
        events: list[dict] = []
        commands = _drain(publisher.commands) if publisher is not None else []
        try:
            if commands:
                _run_commands(config, deployers, state, commands, events)
            else:
                run_cycle(config, deployers, state, events=events)
        except Exception:  # keep the daemon alive
            log.exception("unexpected error in cycle")

        def refresh(cfg=config) -> None:
            if publisher is not None:
                publisher.update(cfg, build_snapshot(cfg, State(cfg.settings.data_dir)))

        refresh()
        if publisher is not None:
            for event in events:
                publisher.event(event)

        interval = max(config.settings.check_interval_hours, 0.1) * 3600
        window = config.settings.deploy_window
        if window is not None:
            # Always wake up shortly after the window opens, so pending deploys run there
            # even if the regular check interval would skip over the window.
            interval = min(interval, window.seconds_until_start(datetime.now(timezone.utc)) + 60)
        log.info("next check in %.1f hours (or when the config file changes)", interval / 3600)
        # hourly status refresh keeps "days left" current in Home Assistant
        _sleep_until_due_or_config_changed(
            interval, loaded_mtime, on_tick=refresh,
            commands=publisher.commands if publisher is not None else None,
        )


def _drain(q: "queue.Queue") -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    while True:
        try:
            item = q.get_nowait()
        except queue.Empty:
            return items
        if item not in items:  # pressing a button twice runs it once
            items.append(item)


def _run_commands(
    config: Config, deployers: dict[str, Deployer], state: State,
    commands: list[tuple[str, str]], events: list[dict],
) -> None:
    """Manual requests from Home Assistant. They ignore the deploy window."""
    known = {c.name for c in config.certs}
    for cert, action in commands:
        if cert not in known:
            log.warning("manual %s for unknown certificate %r ignored", action, cert)
            continue
        log.info("[%s] manual request from Home Assistant: %s", cert, action)
        run_cycle(
            config, deployers, state, only=cert, in_window=True, force_deploy=True,
            force_issue=action == "renew", events=events,
        )


def _ensure_publisher(current, config: Config):
    """Create, keep or replace the MQTT publisher to match the configuration."""
    wanted = config.settings.mqtt
    if current is not None and current.s == wanted:
        return current
    if current is not None:
        current.close()
    if wanted is None:
        return None
    try:
        from .mqtt import Publisher

        log.info("Home Assistant integration via MQTT %s:%s enabled", wanted.host, wanted.port)
        return Publisher(wanted, config.settings.data_dir)
    except Exception:  # noqa: BLE001 - MQTT is optional
        log.exception("cannot start MQTT publisher")
        return None
