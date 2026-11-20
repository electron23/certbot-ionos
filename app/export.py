"""Write the PEM files of a certificate as real files into a directory."""

from __future__ import annotations

import os
from pathlib import Path

from .issuer import CertBundle


def write_bundle(bundle: CertBundle, target: Path, prefix: str = "") -> dict[str, Path]:
    """Atomically write fullchain/privkey/cert/chain.pem into ``target``.

    Returns the written paths by file name. The private key gets mode 0600,
    the public parts 0644.
    """
    target.mkdir(parents=True, exist_ok=True)
    files = {
        "fullchain.pem": (bundle.fullchain, 0o644),
        "privkey.pem": (bundle.privkey, 0o600),
        "cert.pem": (bundle.cert, 0o644),
        "chain.pem": (bundle.chain, 0o644),
    }
    written: dict[str, Path] = {}
    for name, (content, mode) in files.items():
        dest = target / f"{prefix}{name}"
        tmp = dest.with_name(dest.name + ".new")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "w") as fh:
            fh.write(content)
        os.chmod(tmp, mode)
        os.replace(tmp, dest)
        written[name] = dest
    return written
