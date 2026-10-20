from __future__ import annotations

from revend_sync import commands

from .fake_api import MACHINE_ID
from .mysql_support import requires_mysql
from .test_machine_db import config_for


def seed_pool(db, codes, in_command="0"):
    db.executemany("INSERT INTO printer_barcode (barcode) VALUES (%s)", [[c] for c in codes])
    db.execute("INSERT INTO command (printer_barcode) VALUES (%s)", [in_command])


@requires_mysql
def test_the_pool_is_reported_without_commands(machine, api, client):
    db, _ = machine
    seed_pool(db, ["9939176205029", "9939152660071"])

    assert commands.poll(client, db, config_for(api.base, db)) == 0

    report = api.got("agent/commands")[0]
    assert report["machineId"] == MACHINE_ID
    assert report["couponPool"]["count"] == 2
    assert sorted(report["couponPool"]["sample"]) == ["9939152660071", "9939176205029"]
    assert db.query("SELECT COUNT(*) AS n FROM printer_barcode")[0]["n"] == 2


@requires_mysql
def test_clearing_the_pool_drops_copied_codes_and_fetches_new_ones(machine, api, client):
    db, _ = machine
    old = [f"99391762{i:05d}" for i in range(30)]  # above COUPON_MIN: never topped up by itself
    seed_pool(db, old, in_command=old[0])
    api.coupons = [f"48435408{i:05d}" for i in range(60)]
    api.commands = [{"id": 7, "type": "clear_coupon_pool"}]

    assert commands.poll(client, db, config_for(api.base, db)) == 1

    left = {r["barcode"] for r in db.query("SELECT barcode FROM printer_barcode")}
    assert not left & set(old)
    assert len(left) == 50
    assert db.query("SELECT printer_barcode FROM command")[0]["printer_barcode"] == "0"

    result = api.got("agent/result")[0]
    assert result["status"] == "done"
    assert result["result"] == {"removed": 30, "added": 50, "count": 50}
    assert result["error"] is None


@requires_mysql
def test_an_unknown_command_is_reported_as_failed(machine, api, client):
    db, _ = machine
    seed_pool(db, [])
    api.commands = [{"id": 8, "type": "format_disk"}]

    commands.poll(client, db, config_for(api.base, db))

    result = api.got("agent/result")[0]
    assert result["status"] == "failed"
    assert "format_disk" in result["error"]
