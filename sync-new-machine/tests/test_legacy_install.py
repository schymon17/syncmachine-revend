"""Taking over from the PHP agent, against a real MySQL (REVEND_TEST_MYSQL)."""

from __future__ import annotations

import json
import time

from revend_sync import config, legacy
from revend_sync.installer import InstallOptions, install

from .fake_api import KEY_ID, MACHINE_ID, SECRET
from .mysql_support import insert_item, requires_mysql

pytestmark = requires_mysql


def make_legacy(tmp_path, db, snapshot=None, queue=None):
    root = tmp_path / "syncmachine-revend"
    (root / "bin").mkdir(parents=True)
    (root / "data").mkdir()
    (root / "bin" / "sync.php").write_text("<?php")
    (root / "daemon.bat").write_text("@echo off")
    s = db._settings
    (root / "data" / "app.config.json").write_text(
        json.dumps(
            {
                "machineId": MACHINE_ID,
                "db": {
                    "driver": "mysql",
                    "host": s.host,
                    "port": s.port,
                    "database": s.database,
                    "username": "root",
                    "password": s.password,
                },
                "paths": {
                    "snapshot": "data/snapshot.json",
                    "queue": "data/offline-queue.json",
                    "log": "data/log.json",
                    "advertsDir": "D:\\www\\img",
                    "advertsVideoDir": "D:\\www\\video",
                },
            }
        )
    )
    (root / "data" / "snapshot.json").write_text(json.dumps(snapshot or {}))
    if queue:
        (root / "data" / "offline-queue.json").write_text("\n".join(json.dumps(q) for q in queue))
    return root


def test_the_new_agent_starts_where_the_old_one_stopped(machine, tmp_path):
    db, _ = machine
    now = int(time.time())
    for i, age in enumerate([7200, 5000, 3000, 600]):
        insert_item(db, f"C{i}", now - age, done=2, id=i + 1)
    for i in range(1, 6):
        db.execute(
            "INSERT INTO empty_record (id, dateline, bin_type, barcode) VALUES (%s, %s, 'left', %s)",
            (i, now, f"S{i}"),
        )

    root = make_legacy(
        tmp_path, db, snapshot={"user_transaction_lastSync": now - 1000, "empty_records_last_id": 3}
    )
    old = legacy.find([root / "bin"])
    assert old is not None and old.root == root
    assert old.db_settings().password == db._settings.password

    # One hour of overlap before the old cursor; bags right after its last id.
    assert legacy.cursors(old, db, now=now) == (3, 4)  # rows from now - 1000 - 3600 on


def test_transactions_the_old_agent_queued_offline_are_resent(machine, tmp_path):
    db, _ = machine
    now = int(time.time())
    insert_item(db, "OLD", now - 20000, done=2, id=1)
    insert_item(db, "QUEUED", now - 9000, done=2, id=2)
    insert_item(db, "NEW", now - 100, done=2, id=3)
    queued_at = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now - 8000))
    root = make_legacy(
        tmp_path,
        db,
        snapshot={"user_transaction_lastSync": now - 100},
        queue=[{"endpoint": "/trans", "queuedAt": queued_at, "kind": "transactions"}],
    )

    assert legacy.cursors(legacy.load(root), db, now=now)[0] == 2


def test_only_the_old_agents_outbox_triggers_are_dropped(machine):
    db, _ = machine
    db.execute("CREATE TABLE sync_outbox (id INT AUTO_INCREMENT PRIMARY KEY, source_pk VARCHAR(64))")
    db.execute("CREATE TABLE machine_audit (id INT AUTO_INCREMENT PRIMARY KEY, note VARCHAR(64))")
    db.execute(
        "CREATE TRIGGER sync_user_transaction_ai AFTER INSERT ON user_transaction FOR EACH ROW "
        "INSERT INTO sync_outbox (source_pk) VALUES (NEW.print_barcode)"
    )
    db.execute(
        "CREATE TRIGGER sync_machine_audit AFTER UPDATE ON user_transaction FOR EACH ROW "
        "INSERT INTO machine_audit (note) VALUES ('changed')"
    )

    assert legacy.drop_triggers(db) == ["sync_user_transaction_ai"]
    names = {
        r["TRIGGER_NAME"]
        for r in db.query(
            "SELECT TRIGGER_NAME FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = DATABASE()"
        )
    }
    assert names == {"sync_machine_audit"}


def test_install_takes_the_machine_over_from_the_old_agent(machine, tmp_path):
    db, _ = machine
    now = int(time.time())
    insert_item(db, "C1", now - 600, done=2, id=10)
    db.execute("CREATE TABLE sync_outbox (id INT AUTO_INCREMENT PRIMARY KEY, source_pk VARCHAR(64))")
    db.execute(
        "CREATE TRIGGER sync_user_transaction_au AFTER UPDATE ON user_transaction FOR EACH ROW "
        "INSERT INTO sync_outbox (source_pk) VALUES (NEW.print_barcode)"
    )
    root = make_legacy(
        tmp_path, db, snapshot={"user_transaction_lastSync": now - 300, "empty_records_last_id": 0}
    )
    enrollment = tmp_path / "enrollment.json"
    enrollment.write_text(
        json.dumps(
            {
                "data": {
                    "machineId": MACHINE_ID,
                    "integration": "kaucja",
                    "keyId": KEY_ID,
                    "secret": SECRET,
                    "apiBaseUrl": "https://panel.example/api/revend/machine/v2",
                    "agentBaseUrl": "https://panel.example/api/revend/agent",
                }
            }
        )
    )
    data_dir = tmp_path / "data"
    messages = []

    install(
        InstallOptions(
            enrollment_file=enrollment, legacy_dir=root, install_root=tmp_path / "Sync", data_dir=data_dir
        ),
        say=messages.append,
        ask_password=lambda _: "never asked",
    )

    saved = config.load(data_dir)
    assert (saved.machine_id, saved.key_id, saved.secret) == (MACHINE_ID, KEY_ID, SECRET)
    assert saved.db.password == db._settings.password and saved.db.database == db._settings.database
    assert (saved.transactions_from_id, saved.bins_from_id) == (10, 1)
    assert (saved.adverts_image_dir, saved.adverts_video_dir) == ("D:\\www\\img", "D:\\www\\video")
    assert any("triggery" in m for m in messages)
    assert (
        db.query("SELECT COUNT(*) AS n FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = DATABASE()")[
            0
        ]["n"]
        == 0
    )
