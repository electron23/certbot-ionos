"""Prepare the folders of the Samba share and seed example files."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

log = logging.getLogger("share")

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def init_share(share_dir: str) -> None:
    root = Path(share_dir)
    try:
        for sub in ("certs", "scripts", "config"):
            (root / sub).mkdir(parents=True, exist_ok=True)
        seeds = {
            "certsync.env.example": root / "config" / "certsync.env.example",
            "example-deploy.sh": root / "scripts" / "example-deploy.sh",
            "README.txt": root / "README.txt",
        }
        for name, dest in seeds.items():
            source = EXAMPLES / name
            if source.is_file() and not dest.exists():
                shutil.copyfile(source, dest)
    except OSError as exc:
        log.warning("cannot prepare share %s: %s", share_dir, exc)
