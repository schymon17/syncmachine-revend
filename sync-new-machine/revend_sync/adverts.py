"""Advertising media for the machine screen (``/adverts``).

Same result on the machine as the old PHP agent: files in the phpStudy web
folders (``downadpic/img``, ``advideo/video``) named ``<slot>_<name>``, and
their relative paths in ``machineinformation.p_down0..p_down4`` (images) and
``v_top0`` (video). Slots without media get ReVend's placeholder.

Improvements over the old agent: TLS is verified, files are streamed
(videos never sit in memory), written to a temp file and swapped in (the
screen never shows half a file), and checked against the size the API
reports. Unchanged adverts (same md5) are not downloaded again while the
files are still there.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

import requests

from .api import Client, encode_body
from .config import Config
from .machine_db import MachineDb
from .store import Store

log = logging.getLogger(__name__)

DEFAULT_IMAGE_DIR = r"C:\phpStudy\PHPTutorial\WWW\downadpic\img"
DEFAULT_VIDEO_DIR = r"C:\phpStudy\PHPTutorial\WWW\advideo\video"
COLUMNS = ("p_down0", "p_down1", "p_down2", "p_down3", "p_down4", "v_top0")
# The API names the video slot v1; the old agent stored it as p5 in file names.
SLOT_LABELS = {"p1": "p1", "p2": "p2", "p3": "p3", "p4": "p4", "p5": "p5", "v1": "p5"}
IMAGE_COLUMNS = {"p1": "p_down0", "p2": "p_down1", "p3": "p_down2", "p4": "p_down3", "p5": "p_down4"}
VIDEO_EXTENSIONS = {"mp4", "webm", "mov", "avi", "m4v", "mkv", "wmv"}


@dataclass
class Advert:
    slot: str
    url: str
    video: bool
    file_name: str
    size: int | None

    def target(self, image_dir: Path, video_dir: Path) -> Path:
        return (video_dir if self.video else image_dir) / self.file_name

    @property
    def relative_path(self) -> str:
        return ("video/" if self.video else "img/") + self.file_name

    @property
    def column(self) -> str:
        return "v_top0" if self.video else IMAGE_COLUMNS[self.slot]


def parse(response: Any) -> tuple[str | None, list[Advert]]:
    """(md5, adverts) from the /adverts response."""
    if not isinstance(response, dict):
        return None, []
    md5 = response.get("md5") if isinstance(response.get("md5"), str) else None
    items = response.get("adverts")
    if isinstance(items, dict):
        items = list(items.items())
    elif isinstance(items, list):
        items = [(item.get("slot"), item) for item in items if isinstance(item, dict)]
    else:
        return md5, []

    adverts = []
    for key, item in items:
        if not isinstance(item, dict):
            continue
        slot = SLOT_LABELS.get(str(item.get("slot") or key or "").strip().lower())
        url = str(item.get("url") or "").strip()
        if slot is None or not url:
            continue
        video = _is_video(item, url)
        name = str(item.get("name") or "").strip() or PurePosixPath(urlsplit(url).path).name or f"{slot}.bin"
        size = item.get("size")
        adverts.append(
            Advert(
                slot=slot,
                url=url,
                video=video,
                file_name=f"{slot}_{_file_name(name, item.get('extension'), video)}",
                size=int(size) if isinstance(size, (int, float)) and size > 0 else None,
            )
        )
    return md5, adverts


def sync_adverts(
    client: Client, db: MachineDb, store: Store, config: Config, session: requests.Session | None = None
) -> bool:
    if not set(COLUMNS) <= db.table_columns("machineinformation"):
        log.warning("machineinformation has no advert columns; adverts are not synchronised")
        return False

    response = client.post(config.url("adverts"), encode_body({"machineId": config.machine_id}))
    md5, adverts = parse(response.data)
    if not adverts:
        return False

    image_dir, video_dir = Path(config.adverts_image_dir), Path(config.adverts_video_dir)
    present = all(_complete(a.target(image_dir, video_dir), a.size) for a in adverts)
    if md5 and store.get("adverts_md5") == md5 and present:
        return False

    image_dir.mkdir(parents=True, exist_ok=True)
    video_dir.mkdir(parents=True, exist_ok=True)

    paths: dict[str, str] = {}
    failures = 0
    for advert in adverts:
        try:
            _download(advert, advert.target(image_dir, video_dir), session or requests.Session())
        except Exception as error:  # noqa: BLE001 - one bad file must not block the others
            failures += 1
            log.warning(
                "Advert %s could not be downloaded",
                advert.slot,
                extra={"context": {"url": advert.url, "error": str(error)[:300]}},
            )
            continue
        paths[advert.column] = advert.relative_path

    if not paths:
        raise RuntimeError("no advert could be downloaded; the machine keeps its current adverts")

    _save_paths(db, config.machine_id, paths)
    if failures == 0 and md5:
        store.set("adverts_md5", md5)
    log.info("Adverts updated", extra={"context": {"paths": paths, "failed": failures}})
    return True


def _save_paths(db: MachineDb, machine_id: str, paths: dict[str, str]) -> None:
    """Only the slots that were downloaded change; the others keep their file."""
    assignments = ", ".join(f"`{column}` = %s" for column in paths)
    values = list(paths.values())
    # Checked separately: UPDATE reports changed rows, 0 when the paths are the same.
    if db.query("SELECT COUNT(*) AS n FROM machineinformation WHERE mid = %s", (machine_id,))[0]["n"]:
        db.execute(f"UPDATE machineinformation SET {assignments} WHERE mid = %s", [*values, machine_id])
        return
    # The machine's own table may not know the ReVend machine ID. The old
    # agent then updated an arbitrary row; do it only when there is exactly one.
    rows = db.query("SELECT COUNT(*) AS n FROM machineinformation")[0]["n"]
    if rows == 1:
        db.execute(f"UPDATE machineinformation SET {assignments}", values)
        log.warning("machineinformation has no row for %s; updated its only row", machine_id)
    else:
        raise RuntimeError(
            f"machineinformation has no row for {machine_id} ({rows} rows); advert paths not saved"
        )


def _download(advert: Advert, target: Path, session: requests.Session) -> None:
    partial = target.with_name(target.name + ".part")
    written = 0
    with session.get(advert.url, stream=True, timeout=(10, 120)) as response:
        response.raise_for_status()
        with open(partial, "wb") as handle:
            for chunk in response.iter_content(chunk_size=256 * 1024):
                handle.write(chunk)
                written += len(chunk)
    if written == 0 or (advert.size is not None and written != advert.size):
        partial.unlink(missing_ok=True)
        raise ValueError(f"downloaded {written} bytes, expected {advert.size or 'more than 0'}")
    os.replace(partial, target)


def _complete(path: Path, size: int | None) -> bool:
    try:
        actual = path.stat().st_size
    except OSError:
        return False
    return actual > 0 and (size is None or actual == size)


def _is_video(item: dict[str, Any], url: str) -> bool:
    for key in ("group", "type", "mediaType", "mime", "kind"):
        value = str(item.get(key) or "").lower()
        if "video" in value or "mp4" in value:
            return True
        if any(word in value for word in ("image", "img", "photo")):
            return False
    for candidate in (item.get("filename"), item.get("name"), urlsplit(url).path):
        extension = PurePosixPath(str(candidate or "")).suffix.lstrip(".").lower()
        if extension:
            return extension in VIDEO_EXTENSIONS
    return False


def _file_name(name: str, extension: Any, video: bool) -> str:
    """The old agent's rule: safe characters only, extension guaranteed."""
    clean = re.sub(r"[^A-Za-z0-9._-]", "_", name).strip("._-") or "file.bin"
    if re.search(r"\.[A-Za-z0-9]{2,5}$", clean):
        return clean
    ext = str(extension or "").strip().lstrip(".").lower()
    if not re.fullmatch(r"[a-z0-9]{2,5}", ext):
        ext = "mp4" if video else "jpg"
    return f"{clean}.{ext}"
