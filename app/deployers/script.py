"""Run your own script to deploy a certificate (the escape hatch for any system).

Drop the script into the ``scripts`` folder of the Samba share. The script is
run with the certificate files and metadata in environment variables:

    CERT_NAME          name of the certificate (e.g. srv)
    CERT_DOMAINS       comma separated domains
    CERT_FULLCHAIN     path to fullchain.pem
    CERT_PRIVKEY       path to privkey.pem
    CERT_CERT          path to cert.pem   (leaf only)
    CERT_CHAIN         path to chain.pem
    CERT_NOT_AFTER     expiry date (ISO 8601)
    TARGET_NAME        name of this target
    OPT_<KEY>          every other TARGET_<NAME>_<KEY> option of this target

The files only exist while the script runs. A non-zero exit code marks the
deploy as failed (it is retried in the next cycle).
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from ..config import ConfigError
from ..export import write_bundle
from ..issuer import CertBundle
from .base import Deployer, register

log = logging.getLogger("deploy.script")


@register("script")
class ScriptDeployer(Deployer):
    """Options:

    path      script file; relative paths are resolved in <SHARE_DIR>/scripts
    timeout   seconds before the script is killed (default 300)
    """

    required = ("path",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        scripts_dir = Path(os.environ.get("SHARE_DIR", "/share")) / "scripts"
        script = Path(self.options["path"])
        self.script = script if script.is_absolute() else scripts_dir / script
        # Resolve at deploy time so scripts can be added while the container runs,
        # but refuse paths that escape the scripts folder.
        if not script.is_absolute():
            try:
                self.script.resolve().relative_to(scripts_dir.resolve())
            except ValueError as exc:
                raise ConfigError(
                    f"Target '{self.name}': script path must stay inside {scripts_dir}"
                ) from exc

    def _command(self) -> list[str]:
        # Files copied from Windows via Samba usually lack the executable bit,
        # so pick an interpreter from the extension instead.
        suffix = self.script.suffix.lower()
        if suffix == ".py":
            return [sys.executable, str(self.script)]
        if suffix == ".sh":
            return ["bash", str(self.script)]
        if os.access(self.script, os.X_OK):
            return [str(self.script)]
        return ["bash", str(self.script)]

    def deploy(self, bundle: CertBundle) -> None:
        if not self.script.is_file():
            raise RuntimeError(f"script not found: {self.script}")

        extra = {
            f"OPT_{key.upper()}": value
            for key, value in self.options.items()
            if key not in ("path", "timeout")
        }
        with tempfile.TemporaryDirectory(prefix="certsync-") as tmp:
            files = write_bundle(bundle, Path(tmp))
            env = {
                **os.environ,
                **extra,
                "CERT_NAME": bundle.name,
                "CERT_DOMAINS": ",".join(bundle.domains),
                "CERT_FULLCHAIN": str(files["fullchain.pem"]),
                "CERT_PRIVKEY": str(files["privkey.pem"]),
                "CERT_CERT": str(files["cert.pem"]),
                "CERT_CHAIN": str(files["chain.pem"]),
                "CERT_NOT_AFTER": bundle.not_after.isoformat(),
                "TARGET_NAME": self.name,
            }
            # Do not leak the IONOS credentials into user scripts.
            for secret in ("IONOS_PREFIX", "IONOS_SECRET"):
                env.pop(secret, None)

            proc = subprocess.run(
                self._command(),
                env=env,
                cwd=str(self.script.parent),
                capture_output=True,
                text=True,
                timeout=int(self.opt("timeout", "300")),
            )
        for line in (proc.stdout + proc.stderr).splitlines():
            if line.strip():
                log.info("[%s] %s", self.name, line)
        if proc.returncode != 0:
            raise RuntimeError(f"{self.script.name} exited with code {proc.returncode}")
        log.info("[%s] %s finished successfully", self.name, self.script.name)
