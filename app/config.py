"""Configuration loading from environment variables.

Layout::

    CERTS=srv,smart                          # names of certificates
    CERT_SRV_DOMAINS=srv.example.net,*.srv.example.net
    CERT_SRV_TARGETS=proxmox,adguard         # names of deploy targets

    TARGET_PROXMOX_TYPE=proxmox              # selects the deployer plugin
    TARGET_PROXMOX_URL=https://...           # every further TARGET_<NAME>_<KEY>
                                             # is passed to the plugin as <key>
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Mapping

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}

_DOMAIN_RE = re.compile(
    r"^(\*\.)?([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$", re.IGNORECASE
)


class ConfigError(Exception):
    """Raised when the environment configuration is invalid."""


def env_key(name: str) -> str:
    """Convert a free-form name ('home-assistant') to its env form ('HOME_ASSISTANT')."""
    return re.sub(r"[^A-Za-z0-9]", "_", name).upper()


def split_list(value: str) -> list[str]:
    return [item for item in re.split(r"[,\s]+", value.strip()) if item]


def parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    v = value.strip().lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    raise ConfigError(f"Cannot interpret {value!r} as boolean")


@dataclass(frozen=True)
class TargetConfig:
    name: str
    type: str
    options: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class CertConfig:
    name: str
    domains: list[str]
    targets: list[str]


@dataclass(frozen=True)
class Settings:
    data_dir: str
    share_dir: str
    acme_email: str
    acme_staging: bool
    acme_server: str | None
    key_type: str
    ionos_prefix: str
    ionos_secret: str
    ionos_endpoint: str
    propagation_seconds: int
    check_interval_hours: float
    renew_days_before_expiry: int


@dataclass(frozen=True)
class Config:
    settings: Settings
    certs: list[CertConfig]
    targets: dict[str, TargetConfig]


def _required(env: Mapping[str, str], key: str) -> str:
    value = env.get(key, "").strip()
    if not value:
        raise ConfigError(f"Missing required environment variable {key}")
    return value


def _number(env: Mapping[str, str], key: str, default: float) -> float:
    raw = env.get(key)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be a number, got {raw!r}") from exc


def _load_settings(env: Mapping[str, str]) -> Settings:
    key_type = (env.get("KEY_TYPE") or "rsa").strip().lower()
    if key_type not in ("rsa", "ecdsa"):
        raise ConfigError("KEY_TYPE must be 'rsa' or 'ecdsa'")
    return Settings(
        data_dir=env.get("DATA_DIR") or "/data",
        share_dir=env.get("SHARE_DIR") or "/share",
        acme_email=_required(env, "ACME_EMAIL"),
        acme_staging=parse_bool(env.get("ACME_STAGING"), False),
        acme_server=(env.get("ACME_SERVER") or "").strip() or None,
        key_type=key_type,
        ionos_prefix=_required(env, "IONOS_PREFIX"),
        ionos_secret=_required(env, "IONOS_SECRET"),
        ionos_endpoint=(env.get("IONOS_ENDPOINT") or "https://api.hosting.ionos.com").strip(),
        propagation_seconds=int(_number(env, "DNS_PROPAGATION_SECONDS", 120)),
        check_interval_hours=_number(env, "CHECK_INTERVAL_HOURS", 12),
        renew_days_before_expiry=int(_number(env, "RENEW_DAYS_BEFORE_EXPIRY", 30)),
    )


def _load_targets(env: Mapping[str, str], names: list[str]) -> dict[str, TargetConfig]:
    """Collect TARGET_<NAME>_<KEY> variables.

    A variable is assigned to the *longest* matching target name, so that
    targets such as HA and HA_APPS do not swallow each other's settings.
    """
    prefixes = {name: f"TARGET_{env_key(name)}_" for name in names}
    collected: dict[str, dict[str, str]] = {name: {} for name in names}

    for var, value in env.items():
        if not var.startswith("TARGET_"):
            continue
        matches = [n for n, p in prefixes.items() if var.startswith(p)]
        if not matches:
            continue
        owner = max(matches, key=lambda n: len(prefixes[n]))
        key = var[len(prefixes[owner]):].lower()
        collected[owner][key] = value

    targets: dict[str, TargetConfig] = {}
    for name in names:
        options = collected[name]
        ttype = options.pop("type", "").strip().lower()
        if not ttype:
            raise ConfigError(
                f"Target '{name}' has no type (set TARGET_{env_key(name)}_TYPE)"
            )
        targets[name] = TargetConfig(name=name, type=ttype, options=options)
    return targets


def parse_env_file(path: str) -> dict[str, str]:
    """Parse a simple KEY=VALUE file (comments with #, optional quotes)."""
    values: dict[str, str] = {}
    try:
        lines = open(path, encoding="utf-8-sig").read().splitlines()
    except FileNotFoundError:
        return values
    except OSError as exc:
        raise ConfigError(f"Cannot read config file {path}: {exc}") from exc
    for number, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ConfigError(f"{path}:{number}: expected KEY=VALUE")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def load_env() -> dict[str, str]:
    """Process environment, overlaid with the optional config file.

    The file (default ``/share/config/certsync.env``, override with
    ``CONFIG_FILE``) wins over the environment, so that editing it on the
    Samba share always has the expected effect. It is re-read every cycle.
    """
    env = dict(os.environ)
    path = env.get("CONFIG_FILE", "/share/config/certsync.env")
    env.update(parse_env_file(path))
    return env


def load_config(env: Mapping[str, str] | None = None) -> Config:
    env = load_env() if env is None else env
    settings = _load_settings(env)

    cert_names = split_list(_required(env, "CERTS"))
    if len(set(cert_names)) != len(cert_names):
        raise ConfigError("CERTS contains duplicate names")

    certs: list[CertConfig] = []
    target_names: list[str] = []
    for name in cert_names:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
            raise ConfigError(f"Invalid certificate name {name!r}")
        prefix = f"CERT_{env_key(name)}_"
        domains = split_list(_required(env, prefix + "DOMAINS"))
        for domain in domains:
            if not _DOMAIN_RE.match(domain):
                raise ConfigError(f"Invalid domain {domain!r} in {prefix}DOMAINS")
        targets = split_list(env.get(prefix + "TARGETS", ""))
        certs.append(CertConfig(name=name, domains=domains, targets=targets))
        for t in targets:
            if t not in target_names:
                target_names.append(t)

    return Config(
        settings=settings,
        certs=certs,
        targets=_load_targets(env, target_names),
    )
