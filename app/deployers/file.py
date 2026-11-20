"""Write the PEM files to a directory (mounted volume) for other services."""

from __future__ import annotations

import logging
from pathlib import Path

from ..export import write_bundle
from ..issuer import CertBundle
from .base import Deployer, register

log = logging.getLogger("deploy.file")


@register("file")
class FileDeployer(Deployer):
    """Options:

    dir       destination directory (mount it into the container)
    prefix    optional file name prefix, e.g. 'srv-' -> srv-fullchain.pem
    """

    required = ("dir",)

    def deploy(self, bundle: CertBundle) -> None:
        target = Path(self.options["dir"])
        write_bundle(bundle, target, self.opt("prefix", "") or "")
        log.info("[%s] wrote certificate files to %s", self.name, target)
