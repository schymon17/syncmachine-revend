from __future__ import annotations

import hashlib
import json

from revend_sync import adverts

from .fake_api import MACHINE_ID
from .mysql_support import requires_mysql
from .test_machine_db import config_for


def advert_response(api, files):
    """The shape ApiAdvertsController returns: p1-p4 images, v1 video, placeholders for empty slots."""
    slots = {}
    for slot, (name, data, placeholder) in files.items():
        api.files[name] = data
        video = slot == "v1"
        slots[slot] = {
            "slot": slot,
            "url": f"{api.base}/storage/media/{name}",
            "type": "video/mp4" if video else "image/jpeg",
            "group": "video" if video else "image",
            "name": name,
            "size": len(data),
            "extension": name.rsplit(".", 1)[-1],
            "placeholder": placeholder,
            "field": {"p1": "p_down0", "p2": "p_down1", "p3": "p_down2", "p4": "p_down3", "v1": "v_top0"}[
                slot
            ],
        }
    return {
        "machineId": MACHINE_ID,
        "md5": hashlib.md5(json.dumps(slots).encode()).hexdigest(),
        "adverts": slots,
    }


def test_slots_map_to_the_old_agents_files_and_columns():
    md5, items = adverts.parse(
        {
            "md5": "abc",
            "adverts": {
                "p1": {
                    "slot": "p1",
                    "url": "https://x/storage/a.jpg",
                    "type": "image/jpeg",
                    "name": "Promocja jesień!.jpg",
                },
                "v1": {
                    "slot": "v1",
                    "url": "https://x/storage/b",
                    "group": "video",
                    "name": "film",
                    "extension": "mp4",
                },
                "p9": {"slot": "p9", "url": "https://x/c.jpg"},
            },
        }
    )
    assert md5 == "abc"
    assert [(a.slot, a.column, a.relative_path) for a in items] == [
        ("p1", "p_down0", "img/p1_Promocja_jesie__.jpg"),
        ("p5", "v_top0", "video/p5_film.mp4"),
    ]


@requires_mysql
def test_adverts_are_downloaded_into_the_web_folders_and_saved_in_the_machine_table(
    machine, store, api, client, tmp_path
):
    db, _ = machine
    db.execute("INSERT INTO machineinformation (mid, p_down0) VALUES (%s, 'img/old.jpg')", (MACHINE_ID,))
    config = config_for(api.base, db)
    config.adverts_image_dir = str(tmp_path / "img")
    config.adverts_video_dir = str(tmp_path / "video")
    api.adverts = advert_response(
        api,
        {
            "p1": ("promo.jpg", b"jpeg-1", False),
            "p2": ("p2.jpg", b"placeholder", True),
            "v1": ("spot.mp4", b"video-bytes" * 1000, False),
        },
    )

    assert adverts.sync_adverts(client, db, store, config) is True

    row = db.query(
        "SELECT p_down0, p_down1, p_down2, v_top0 FROM machineinformation WHERE mid = %s", (MACHINE_ID,)
    )[0]
    assert row == {
        "p_down0": "img/p1_promo.jpg",
        "p_down1": "img/p2_p2.jpg",
        "p_down2": None,
        "v_top0": "video/p5_spot.mp4",
    }
    assert (tmp_path / "img" / "p1_promo.jpg").read_bytes() == b"jpeg-1"
    assert (tmp_path / "video" / "p5_spot.mp4").stat().st_size == 11000
    assert not list(tmp_path.rglob("*.part"))

    # Unchanged and present: nothing downloaded again.
    downloads = len(api.downloads)
    assert adverts.sync_adverts(client, db, store, config) is False
    assert len(api.downloads) == downloads

    # A file deleted on the machine is fetched again.
    (tmp_path / "img" / "p1_promo.jpg").unlink()
    assert adverts.sync_adverts(client, db, store, config) is True
    assert (tmp_path / "img" / "p1_promo.jpg").exists()


@requires_mysql
def test_a_failed_download_keeps_the_current_file_of_that_slot(machine, store, api, client, tmp_path):
    db, _ = machine
    db.execute(
        "INSERT INTO machineinformation (mid, p_down0, p_down1) VALUES (%s, 'img/keep.jpg', 'img/keep2.jpg')",
        (MACHINE_ID,),
    )
    config = config_for(api.base, db)
    config.adverts_image_dir = str(tmp_path / "img")
    config.adverts_video_dir = str(tmp_path / "video")
    api.adverts = advert_response(
        api, {"p1": ("new.jpg", b"new", False), "p2": ("broken.jpg", b"full", False)}
    )
    api.files["broken.jpg"] = b"cut"  # shorter than the size the API reports

    assert adverts.sync_adverts(client, db, store, config) is True

    row = db.query("SELECT p_down0, p_down1 FROM machineinformation")[0]
    assert row == {"p_down0": "img/p1_new.jpg", "p_down1": "img/keep2.jpg"}
    assert not (tmp_path / "img" / "p2_broken.jpg").exists()
    assert store.get("adverts_md5") is None  # retried next time


@requires_mysql
def test_a_machine_table_without_the_machine_id_is_updated_only_when_it_has_one_row(
    machine, store, api, client, tmp_path
):
    db, _ = machine
    db.execute("INSERT INTO machineinformation (mid) VALUES ('LOCAL-NAME')")
    config = config_for(api.base, db)
    config.adverts_image_dir = str(tmp_path / "img")
    config.adverts_video_dir = str(tmp_path / "video")
    api.adverts = advert_response(api, {"p1": ("a.jpg", b"a", False)})

    assert adverts.sync_adverts(client, db, store, config) is True
    assert db.query("SELECT p_down0 FROM machineinformation")[0]["p_down0"] == "img/p1_a.jpg"
