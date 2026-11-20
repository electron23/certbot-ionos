"""Generic SSH deployer plus presets for Home Assistant and UniFi OS.

Files are written through the SSH exec channel (``cat > file``), so no SFTP
subsystem is required on the target (the Home Assistant SSH add-on ships with
SFTP disabled by default).
"""

from __future__ import annotations

import logging
import os
import shlex
from pathlib import Path

import paramiko

from ..config import ConfigError
from ..issuer import CertBundle
from .base import Deployer, register

log = logging.getLogger("deploy.ssh")

# option key -> (attribute of CertBundle, file mode)
_FILES = {
    "fullchain_path": ("fullchain", "644"),
    "privkey_path": ("privkey", "600"),
    "cert_path": ("cert", "644"),
    "chain_path": ("chain", "644"),
}


@register("ssh")
class SshDeployer(Deployer):
    """Options:

    host, port (22), user (root)
    password | key_file (path to a private key mounted into the container)
    key_passphrase           optional
    fullchain_path           destination of fullchain.pem  (cert + chain)
    privkey_path             destination of privkey.pem
    cert_path, chain_path    optional, leaf only / chain only
    command                  shell command run after upload (activates the cert)
    known_hosts              file to store host keys (default <data>/known_hosts)
    """

    required = ("host",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not any(self.options.get(k) for k in _FILES):
            raise ConfigError(
                f"Target '{self.name}' ({self.type}): set at least one of "
                + ", ".join(_FILES)
            )
        if not (self.options.get("password") or self.options.get("key_file")):
            raise ConfigError(
                f"Target '{self.name}' ({self.type}): set either password or key_file"
            )

    # -- helpers ---------------------------------------------------------
    def _connect(self) -> paramiko.SSHClient:
        client = paramiko.SSHClient()
        known_hosts = Path(
            self.opt("known_hosts") or Path(os.environ.get("DATA_DIR", "/data")) / "known_hosts"
        )
        known_hosts.parent.mkdir(parents=True, exist_ok=True)
        if known_hosts.exists():
            client.load_host_keys(str(known_hosts))
        # Trust on first use; a *changed* host key is still rejected by paramiko.
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        client.connect(
            hostname=self.options["host"],
            port=int(self.opt("port", "22")),
            username=self.opt("user", "root"),
            password=self.opt("password"),
            key_filename=self.opt("key_file"),
            passphrase=self.opt("key_passphrase"),
            look_for_keys=False,
            allow_agent=False,
            timeout=20,
            banner_timeout=20,
            auth_timeout=20,
        )
        known_hosts.touch(exist_ok=True)
        client.save_host_keys(str(known_hosts))
        return client

    @staticmethod
    def _run(client: paramiko.SSHClient, command: str, data: bytes | None = None) -> str:
        stdin, stdout, stderr = client.exec_command(command, timeout=120)
        if data is not None:
            stdin.write(data)
            stdin.flush()
            stdin.channel.shutdown_write()
        out = stdout.read().decode(errors="replace")
        err = stderr.read().decode(errors="replace")
        code = stdout.channel.recv_exit_status()
        if code != 0:
            raise RuntimeError(f"`{command[:80]}` exited with {code}: {(err or out).strip()[:300]}")
        return out

    def _upload(self, client: paramiko.SSHClient, path: str, content: str, mode: str) -> None:
        quoted = shlex.quote(path)
        tmp = shlex.quote(path + ".new")
        directory = shlex.quote(str(Path(path).parent.as_posix()))
        self._run(
            client,
            f"mkdir -p {directory} && umask 077 && cat > {tmp} && chmod {mode} {tmp} && mv -f {tmp} {quoted}",
            content.encode(),
        )

    # -- deploy ----------------------------------------------------------
    def deploy(self, bundle: CertBundle) -> None:
        client = self._connect()
        try:
            for key, (attr, mode) in _FILES.items():
                path = self.opt(key)
                if path:
                    self._upload(client, path, getattr(bundle, attr), mode)
                    log.info("[%s] wrote %s", self.name, path)
            command = self.opt("command")
            if command:
                self._run(client, command)
                log.info("[%s] ran: %s", self.name, command)
        finally:
            client.close()


@register("homeassistant")
class HomeAssistantDeployer(SshDeployer):
    """Home Assistant OS via the SSH add-on. Also used for 'Home Assistant Apps'
    (add-ons): they read the same /ssl share, only ``command`` differs, e.g.
    ``ha addons restart core_nginx_proxy`` (newer versions may call it
    ``ha apps``, check ``ha --help`` on your system).

    Home Assistant itself only picks up new certificates after a restart.
    """

    defaults = {
        "fullchain_path": "/ssl/fullchain.pem",
        "privkey_path": "/ssl/privkey.pem",
        "command": "ha core restart",
        "port": "22",
    }


@register("unifi")
class UnifiOsDeployer(SshDeployer):
    """UniFi OS console (UDM Pro etc.). Requires SSH enabled on the console.

    Writes the certificate where UniFi OS reads its web certificate from and
    restarts the unifi-core service.
    """

    defaults = {
        "fullchain_path": "/data/unifi-core/config/unifi-core.crt",
        "privkey_path": "/data/unifi-core/config/unifi-core.key",
        "command": "systemctl restart unifi-core",
        "user": "root",
    }
