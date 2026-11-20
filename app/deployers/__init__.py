from __future__ import annotations

from ..config import ConfigError, TargetConfig
from . import adguard, file, proxmox, script, ssh  # noqa: F401  (registers the plugins)
from .base import REGISTRY, Deployer


def build_deployer(target: TargetConfig) -> Deployer:
    cls = REGISTRY.get(target.type)
    if cls is None:
        known = ", ".join(sorted(REGISTRY))
        raise ConfigError(
            f"Target '{target.name}': unknown type '{target.type}' (available: {known})"
        )
    return cls(target.name, target.type, dict(target.options))
