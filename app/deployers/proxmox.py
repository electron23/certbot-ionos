"""Proxmox VE: upload the custom certificate via the API (restarts pveproxy)."""

from __future__ import annotations

import logging

import requests

from ..config import ConfigError, split_list
from ..issuer import CertBundle
from .base import Deployer, register

log = logging.getLogger("deploy.proxmox")


@register("proxmox")
class ProxmoxDeployer(Deployer):
    """Options:

    url         https://pve.example.net:8006
    token       user@realm!tokenid=secret   (needs Sys.Modify on /nodes/<node>)
    nodes       comma separated node names (also accepts 'node')
    verify_tls  default true
    """

    required = ("url", "token")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        nodes = self.opt("nodes") or self.opt("node")
        if not nodes:
            raise ConfigError(f"Target '{self.name}' (proxmox): missing nodes")
        self.nodes = split_list(nodes)

    def deploy(self, bundle: CertBundle) -> None:
        base = self.options["url"].rstrip("/")
        headers = {"Authorization": f"PVEAPIToken={self.options['token']}"}
        verify = self.flag("verify_tls", True)

        for node in self.nodes:
            url = f"{base}/api2/json/nodes/{node}/certificates/custom"
            resp = requests.post(
                url,
                headers=headers,
                verify=verify,
                timeout=60,
                data={
                    "certificates": bundle.fullchain,
                    "key": bundle.privkey,
                    "force": 1,
                    "restart": 1,
                },
            )
            if not resp.ok:
                raise RuntimeError(
                    f"Proxmox node {node}: HTTP {resp.status_code} {resp.text[:300]}"
                )
            log.info("[%s] certificate installed on node %s (pveproxy restarted)", self.name, node)
