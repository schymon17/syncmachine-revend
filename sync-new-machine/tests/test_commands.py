from __future__ import annotations

from revend_sync import catalog, commands

from .fake_api import MACHINE_ID
from .mysql_support import requires_mysql
from .test_machine_db import config_for


def seed_pool(db, codes, in_command="0"):
    db.executemany("INSERT INTO printer_barcode (barcode) VALUES (%s)", [[c] for c in codes])
    db.execute("INSERT INTO command (printer_barcode) VALUES (%s)", [in_command])


@requires_mysql
def test_the_pool_is_reported_without_commands(machine, api, client, store):
    db, _ = machine
    seed_pool(db, ["9939176205029", "9939152660071"])

    assert commands.poll(client, db, config_for(api.base, db), store) == 0

    report = api.got("agent/commands")[0]
    assert report["machineId"] == MACHINE_ID
    assert report["couponPool"]["count"] == 2
    assert sorted(report["couponPool"]["sample"]) == ["9939152660071", "9939176205029"]
    assert db.query("SELECT COUNT(*) AS n FROM printer_barcode")[0]["n"] == 2


@requires_mysql
def test_clearing_the_pool_drops_copied_codes_and_fetches_new_ones(machine, api, client, store):
    db, _ = machine
    old = [f"99391762{i:05d}" for i in range(30)]  # above COUPON_MIN: never topped up by itself
    seed_pool(db, old, in_command=old[0])
    api.coupons = [f"48435408{i:05d}" for i in range(60)]
    api.commands = [{"id": 7, "type": "clear_coupon_pool"}]

    assert commands.poll(client, db, config_for(api.base, db), store) == 1

    left = {r["barcode"] for r in db.query("SELECT barcode FROM printer_barcode")}
    assert not left & set(old)
    assert len(left) == 50
    assert db.query("SELECT printer_barcode FROM command")[0]["printer_barcode"] == "0"

    result = api.got("agent/result")[0]
    assert result["status"] == "done"
    assert result["result"] == {"removed": 30, "added": 50, "count": 50}
    assert result["error"] is None


@requires_mysql
def test_an_unknown_command_is_reported_as_failed(machine, api, client, store):
    db, _ = machine
    seed_pool(db, [])
    api.commands = [{"id": 8, "type": "format_disk"}]

    commands.poll(client, db, config_for(api.base, db), store)

    result = api.got("agent/result")[0]
    assert result["status"] == "failed"
    assert "format_disk" in result["error"]


@requires_mysql
def test_refreshing_eans_imports_the_catalogue_even_when_unchanged(machine, api, client, store):
    db, _ = machine
    seed_pool(db, [])
    config = config_for(api.base, db)
    api.eans = [{"barcode": f"590000000{i:04d}", "value": "0.5"} for i in range(20)]
    assert catalog.sync_eans(client, db, store, config) is True
    db.execute("DELETE FROM barcode WHERE barcode LIKE '5900000000%'")  # edited by hand on the machine

    api.commands = [{"id": 9, "type": "refresh_eans"}]
    assert commands.poll(client, db, config, store) == 1

    assert db.query("SELECT COUNT(*) AS n FROM barcode")[0]["n"] == 20
    result = api.got("agent/result")[0]
    assert (result["status"], result["result"], result["error"]) == ("done", {"count": 20}, None)


@requires_mysql
def test_a_refresh_with_an_empty_catalogue_fails_and_keeps_the_current_one(machine, api, client, store):
    db, _ = machine
    seed_pool(db, [])
    config = config_for(api.base, db)
    api.eans = [{"barcode": f"590000000{i:04d}", "value": "0.5"} for i in range(20)]
    catalog.sync_eans(client, db, store, config)

    api.eans = []
    api.commands = [{"id": 10, "type": "refresh_eans"}]
    commands.poll(client, db, config, store)

    assert db.query("SELECT COUNT(*) AS n FROM barcode")[0]["n"] == 20
    result = api.got("agent/result")[0]
    assert result["status"] == "failed" and "not imported" in result["error"]


@requires_mysql
def test_rejected_messages_are_reported_and_resent_on_command(machine, api, client, store, tmp_path):
    from revend_sync.agent import Agent

    db, _ = machine
    seed_pool(db, [])
    store.enqueue("bins", api.base + "/api/revend/machine/v2/bins", b"{}")
    message_id = store.due()[0].id
    store.reject(message_id, "HTTP 409 seal_already_used " + "x" * 1000)

    config = config_for(api.base, db)
    Agent(config, store, db=db, client=client, data_dir=tmp_path).heartbeat()
    agent_state = api.got("heartbeat")[0]["agent"]
    assert agent_state["queueDeadLetter"] == 1
    [dead] = agent_state["deadLetters"]
    assert (dead["id"], dead["kind"]) == (message_id, "bins")
    assert dead["error"].startswith("HTTP 409 seal_already_used") and len(dead["error"]) == 300

    api.commands = [{"id": 9, "type": "requeue_dead"}]
    assert commands.poll(client, db, config, store) == 1

    result = api.got("agent/result")[0]
    assert result["status"] == "done" and result["result"] == {"requeued": 1}
    assert store.stats()["dead"] == 0 and [m.id for m in store.due()] == [message_id]
