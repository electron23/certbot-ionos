"""Base class and registry for deploy targets.

To support a new system, add a module in this package, subclass ``Deployer``
and decorate it with ``@register("mytype")``. Options come from the
``TARGET_<NAME>_<KEY>`` environment variables (keys are lower-cased).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

from ..config import ConfigError, env_key, parse_bool
from ..issuer import CertBundle

REGISTRY: dict[str, type["Deployer"]] = {}


def register(*types: str):
    def decorator(cls: type["Deployer"]) -> type["Deployer"]:
        for t in types:
            REGISTRY[t] = cls
        return cls

    return decorator


class Deployer(ABC):
    #: option keys that must be present
    required: ClassVar[tuple[str, ...]] = ()
    #: defaults applied before validation (e.g. presets for a system type)
    defaults: ClassVar[dict[str, str]] = {}

    def __init__(self, name: str, type_: str, options: dict[str, str]):
        self.name = name
        self.type = type_
        self.options = {**self.defaults, **options}
        missing = [k for k in self.required if not self.options.get(k, "").strip()]
        if missing:
            keys = ", ".join(f"TARGET_{env_key(name)}_{k.upper()}" for k in missing)
            raise ConfigError(f"Target '{name}' ({type_}): missing {keys}")

    def opt(self, key: str, default: str | None = None) -> str | None:
        value = self.options.get(key)
        return default if value is None or value == "" else value

    def flag(self, key: str, default: bool) -> bool:
        return parse_bool(self.options.get(key), default)

    @abstractmethod
    def deploy(self, bundle: CertBundle) -> None:
        """Push the certificate to the target and activate it. Raise on failure."""
