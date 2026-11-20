"""AdGuard Home: set the TLS certificate through the control API."""

from __future__ import annotations

import base64
import logging

import requests

from ..issuer import CertBundle
from .base import Deployer, register

log = logging.getLogger("deploy.adguard")


@register("adguard", "adguardhome")
class AdGuardDeployer(Deployer):
    """Options:

    url         http(s)://adguard.example.net:3000
    username    web UI user
    password    web UI password
    verify_tls  default true
    server_name optional, overrides the TLS server name
    """

    required = ("url", "username", "password")

    def deploy(self, bundle: CertBundle) -> None:
        base = self.options["url"].rstrip("/")
        auth = (self.options["username"], self.options["password"])
        verify = self.flag("verify_tls", True)

        status = requests.get(f"{base}/control/tls/status", auth=auth, verify=verify, timeout=30)
        status.raise_for_status()
        tls = status.json()

        # Keep every existing TLS setting, only swap the certificate material.
        tls["certificate_chain"] = base64.b64encode(bundle.fullchain.encode()).decode()
        tls["private_key"] = base64.b64encode(bundle.privkey.encode()).decode()
        # The status reports private_key_saved=true when a key is stored. Sent back
        # as-is, AdGuard ignores our new key and keeps the old one (key mismatch).
        tls["private_key_saved"] = False
        tls["certificate_path"] = ""
        tls["private_key_path"] = ""
        tls["enabled"] = True
        server_name = self.opt("server_name")
        if server_name:
            tls["server_name"] = server_name

        resp = requests.post(
            f"{base}/control/tls/configure", json=tls, auth=auth, verify=verify, timeout=60
        )
        if not resp.ok:
            raise RuntimeError(f"AdGuard: HTTP {resp.status_code} {resp.text[:300]}")
        try:
            result = resp.json()
        except ValueError:
            result = {}
        warning = result.get("warning_validation") or ""
        # warning_validation also carries harmless hints (e.g. "certificate has no IP
        # addresses"), so judge the result by the validity flags instead.
        invalid = [k for k in ("valid_cert", "valid_key", "valid_pair", "valid_chain")
                   if result.get(k) is False]
        if invalid:
            raise RuntimeError(
                f"AdGuard rejected the certificate ({', '.join(invalid)} false): {warning}"
            )
        if warning:
            log.info("[%s] AdGuard note: %s", self.name, warning)
        log.info("[%s] certificate installed in AdGuard Home", self.name)
