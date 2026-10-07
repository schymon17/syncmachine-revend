"""Against a real MySQL: set REVEND_TEST_MYSQL=host:port[,host:port] (root/test)."""

from __future__ import annotations

import time

from revend_sync import catalog
from revend_sync.agent import Agent
from revend_sync.collectors import BinCollector, StatusCollector, TransactionCollector
from revend_sync.config import Config
from revend_sync.sender import Sender

from .fake_api import KEY_ID, MACHINE_ID, SECRET
from .mysql_support import insert_item, requires_mysql

pytestmark = requires_mysql


def config_for(api_base, db):
    return Config(
        MACHINE_ID,
        api_base + "/api/revend/machine/v2",
        api_base + "/api/revend/agent",
        KEY_ID,
        SECRET,
        db=db._settings,
    )


def test_a_transaction_is_queued_once_when_it_finishes(machine, store, api, client):
    db, _ = machine
    insert_item(db, None, int(time.time()) - 600, done=2, id=1)  # history before the agent
    collector = TransactionCollector(db, store, MACHINE_ID, api.base + "/api/revend/machine/v2/trans")
    collector.initialise()

    now = int(time.time())
    for i in range(3):
        insert_item(db, "2602530598507", now + i)  # in progress
    assert collector.poll() == 0

    db.execute("UPDATE user_transaction SET transactiondone = 2 WHERE print_barcode = '2602530598507'")
    assert collector.poll() == 1
    assert collector.poll() == 0
    assert collector.sweep() == 0  # fingerprint: unchanged, not queued again

    assert Sender(store, client).run_once() == 1
    sent = api.got("trans")[0]["data"]["transactions"]
    assert [t["couponId"] for t in sent] == ["2602530598507"]
    assert sent[0]["acceptedItemsCount"] == 3 and sent[0]["totalDepositAmount"] == 150


def test_a_transaction_without_a_coupon_until_the_end_is_not_skipped(machine, store, api):
    """Some machines fill print_barcode only when the coupon prints."""
    db, _ = machine
    collector = TransactionCollector(db, store, MACHINE_ID, api.base + "/trans")
    collector.initialise()
    now = int(time.time())
    insert_item(db, None, now)
    insert_item(db, None, now + 1)
    assert collector.poll() == 0
    insert_item(db, "OTHER-COUPON", now + 2, done=2)  # another transaction completes meanwhile
    assert collector.poll() == 1

    db.execute(
        "UPDATE user_transaction SET print_barcode = 'LATE-COUPON', transactiondone = 2 WHERE print_barcode IS NULL"
    )
    assert collector.poll() == 1


def test_history_before_the_agent_is_never_sent(machine, store, api):
    db, _ = machine
    insert_item(db, "HISTORY", int(time.time()) - 3600, done=2)
    collector = TransactionCollector(db, store, MACHINE_ID, api.base + "/trans")
    collector.initialise()
    assert collector.sweep() == 0
    assert collector.poll() == 0


def test_an_abandoned_transaction_does_not_hold_the_cursor_forever(machine, store, api):
    db, _ = machine
    collector = TransactionCollector(db, store, MACHINE_ID, api.base + "/trans")
    collector.initialise()
    insert_item(db, "STUCK", int(time.time()) - 7 * 3600)  # never finished
    insert_item(db, "FRESH", int(time.time()), done=2)
    assert collector.poll() == 1
    assert store.get(TransactionCollector.STATE_LOW) > 2


def test_bags_are_queued_one_per_seal_and_never_twice(machine, store, api):
    db, _ = machine
    db.execute(
        "INSERT INTO empty_record (dateline, bin_type, barcode) VALUES (1790000000, 'left', 'OLD-SEAL')"
    )
    bins = BinCollector(db, store, MACHINE_ID, api.base + "/bins")
    bins.initialise()
    db.execute(
        "INSERT INTO empty_record (dateline, bin_type, barcode) VALUES (1790000100, 'left', 'S1'), (1790000200, 'right', 'S2')"
    )
    assert bins.poll() == 2
    assert bins.poll() == 0
    assert store.stats()["pending"] == 2


def test_status_is_queued_on_change_only(machine, store, api):
    db, _ = machine
    db.execute(
        "INSERT INTO command (storage, storageplastic, storagecan, errorcode) VALUES (80, 70, 60, '0')"
    )
    db.columns["command"] = db.table_columns("command")
    status = StatusCollector(db, store, MACHINE_ID, api.base + "/status")
    assert status.poll() is True
    assert status.poll() is False
    db.execute(
        "INSERT INTO command (storage, storageplastic, storagecan, errorcode) VALUES (80, 70, 60, 'E12')"
    )
    assert status.poll() is True
    assert store.stats()["pending"] == 1  # the older unsent status was replaced


def test_the_catalogue_is_swapped_atomically_and_never_emptied(machine, store, api, client):
    db, _ = machine
    db.execute("INSERT INTO barcode (barcode, brand) VALUES ('1111111111111', 'Old')")
    config = config_for(api.base, db)

    api.eans = [
        {"barcode": f"590000000{i:04d}", "brand": "B" * 200, "metal": True, "value": "0.5"}
        for i in range(1200)
    ]
    assert catalog.sync_eans(client, db, store, config) is True
    rows = db.query("SELECT COUNT(*) AS n, MAX(LENGTH(brand)) AS brand_len FROM barcode")[0]
    assert rows["n"] == 1200 and rows["brand_len"] == 200
    assert db.query("SHOW TABLES LIKE 'barcode_revend_%%'") == []

    assert catalog.sync_eans(client, db, store, config) is False  # unchanged

    api.eans = api.eans[:100]  # a broken, much smaller response
    assert catalog.sync_eans(client, db, store, config) is False
    assert db.query("SELECT COUNT(*) AS n FROM barcode")[0]["n"] == 1200

    api.eans = []
    assert catalog.sync_eans(client, db, store, config) is False
    assert db.query("SELECT COUNT(*) AS n FROM barcode")[0]["n"] == 1200


def test_the_coupon_pool_is_topped_up_without_used_numbers(machine, api, client):
    db, _ = machine
    db.execute("INSERT INTO printer_barcode (barcode) VALUES ('C-QUEUED')")
    db.execute("INSERT INTO command (printer_barcode) VALUES ('C-PRINTING')")
    insert_item(db, "C-USED", 1790000000, done=2)
    api.coupons = ["C-QUEUED", "C-PRINTING", "C-USED"] + [f"C-{i}" for i in range(60)]

    added = catalog.top_up_coupons(client, db, config_for(api.base, db))
    assert added == 49
    codes = {r["barcode"] for r in db.query("SELECT barcode FROM printer_barcode")}
    assert len(codes) == 50 and not codes & {"C-PRINTING", "C-USED"}


def test_end_to_end_offline_then_online(machine, store, api, tmp_path):
    """The whole agent: a transaction made while the API is down arrives once it is back."""
    db, _ = machine
    config = config_for(api.base, db)
    config.eans_enabled = config.coupons_enabled = False
    agent = Agent(config, store, db=db, data_dir=tmp_path)

    def run(seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            agent.tick()
            time.sleep(0.05)

    for task in agent.tasks:
        task.next_at = 0
    run(1)
    assert agent._ready

    api.fail_with = 503
    now = int(time.time())
    insert_item(db, "OFFLINE-1", now, done=2)
    insert_item(db, "OFFLINE-1", now + 1, done=2, metal="1")
    db.execute(
        "INSERT INTO empty_record (dateline, bin_type, barcode) VALUES (%s, 'right', 'SEAL-OFFLINE')", (now,)
    )
    for task in agent.tasks:
        task.next_at = 0
    run(2)
    assert api.got("trans") == []
    assert store.stats()["pending"] + store.stats()["failed"] >= 2

    api.fail_with = None
    for task in agent.tasks:
        task.next_at = 0
    store._db.execute("UPDATE outbox SET next_attempt_at = 0")
    agent.sender._paused_until = 0
    run(3)

    assert [t["couponId"] for b in api.got("trans") for t in b["data"]["transactions"]] == ["OFFLINE-1"]
    assert api.got("bins")[0]["data"]["empty_records"][0] == {
        "barcode": "SEAL-OFFLINE",
        "bin_type": "can",
        "dateline": now,
    }
    heartbeat = api.got("heartbeat")[-1]
    assert heartbeat["agent"]["machineDbConnected"] is True
    assert store.stats()["pending"] == 0
