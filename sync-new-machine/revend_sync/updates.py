"""Agent self-update: find, download and verify a newer release.

Installing it (stop service, swap files, start, roll back on failure) is
done by the service wrapper, which looks for ``updates/ready.json``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path

import requests

from . import __version__
from .api import Client, encode_body
from .config import Config

log = logging.getLogger(__name__)


def check(
    client: Client, config: Config, updates_dir: Path, session: requests.Session | None = None
) -> str | None:
    """Download a newer release if there is one. Returns its version when ready to install."""
    response = client.post(
        config.agent_url("release"),
        encode_body({"machineId": config.machine_id, "currentVersion": __version__}),
    )
    data = response.data.get("data", {}) if isinstance(response.data, dict) else {}
    release = data.get("release")
    if not data.get("updateAvailable") or not isinstance(release, dict):
        return None

    version = str(release["version"])
    ready = updates_dir / "ready.json"
    if ready.exists() and json.loads(ready.read_text(encoding="utf-8")).get("version") == version:
        return version

    updates_dir.mkdir(parents=True, exist_ok=True)
    target = updates_dir / f"revend-sync-{version}.zip"
    partial = target.with_suffix(".part")
    digest = hashlib.sha256()

    with (session or requests).get(
        release["downloadUrl"], stream=True, timeout=(10, 120), allow_redirects=False
    ) as download:
        download.raise_for_status()
        with open(partial, "wb") as handle:
            for chunk in download.iter_content(chunk_size=1024 * 256):
                handle.write(chunk)
                digest.update(chunk)

    if digest.hexdigest() != release["sha256"]:
        partial.unlink(missing_ok=True)
        log.error("Downloaded update %s has a wrong SHA-256; discarded", version)
        return None

    os.replace(partial, target)
    ready.write_text(
        json.dumps({"version": version, "file": target.name, "sha256": release["sha256"]}), encoding="utf-8"
    )
    log.info("Update %s downloaded and verified; it will be installed by the service", version)
    return version
