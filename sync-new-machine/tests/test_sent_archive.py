"""Sent transactions and seals stay on the machine for 12 weeks (operators' RVM recommendations, 11.8)."""

from __future__ import annotations

import json
import time

from revend_sync import cli
from revend_sync.api import encode_body
from revend_sync.sender import Sender
from revend_sync.store import ARCHIVE_RETENTION_SECONDS, DEAD_RETENTION_SECONDS, Store

from .fake_api import MACHINE_ID


def trans_body(coupon: str) -> bytes:
    return encode_body(
        {
            "machineId": MACHINE_ID,
            "data": {
                "transactions": {coupon: {"details": [{"barcode": "5900000000001", "recognitionstatus": 1}]}}
            },
        }
    )


def test_sent_transactions_are_archived_with_their_exact_body_and_status_is_not(store):
    first = store.enqueue("trans", "http://api/trans", trans_body("COUPON-1"))
    status = store.enqueue("status", "http://api/status", encode_body({"machineId": MACHINE_ID}))

    store.delivered(first)
    store.delivered(status)

    archived = list(store.archived())
    assert [m["kind"] for m in archived] == ["trans"]
    assert archived[0]["body"] == json.loads(trans_body("COUPON-1"))
    assert store.stats()["pending"] == 0
    assert store.archive_stats()["messages"] == 1


def test_sender_archives_what_the_api_accepted(api, client, store):
    url = api.base + "/api/revend/machine/v2/bins"
    store.enqueue(
        "bins",
        url,
        encode_body(
            {
                "machineId": MACHINE_ID,
                "data": {"empty_records": [{"barcode": "SEAL-1", "bin_type": "pet", "dateline": 1790000000}]},
            }
        ),
    )

    assert Sender(store, client).run_once() == 1
    assert [m["body"]["data"]["empty_records"][0]["barcode"] for m in store.archived()] == ["SEAL-1"]


def test_prune_keeps_twelve_weeks_of_transactions_including_rejected_ones(store):
    now = time.time()
    for coupon, age_days in (("OLD", 85), ("RECENT", 83)):
        store.delivered(store.enqueue("trans", "http://api/trans", trans_body(coupon)))
        store._db.execute(
            "UPDATE sent_archive SET sent_at = ? WHERE id = (SELECT MAX(id) FROM sent_archive)",
            (now - age_days * 86400,),
        )

    rejected_trans = store.enqueue("trans", "http://api/trans", trans_body("REJECTED"))
    rejected_log = store.enqueue("logs", "http://api/logs", encode_body({"machineId": MACHINE_ID}))
    for message_id in (rejected_trans, rejected_log):
        store.reject(message_id, "422")
    # Older than the 30-day dead-letter window, younger than 12 weeks.
    store._db.execute("UPDATE outbox SET next_attempt_at = ?", (now - (DEAD_RETENTION_SECONDS + 86400),))

    store.prune()

    coupons = [next(iter(m["body"]["data"]["transactions"])) for m in store.archived()]
    assert coupons == ["RECENT"]
    assert [d["kind"] for d in store.dead_letters()] == ["trans"]

    store._db.execute("UPDATE outbox SET next_attempt_at = ?", (now - ARCHIVE_RETENTION_SECONDS - 86400,))
    store.prune()
    assert store.dead_letters() == []


def test_archive_export_writes_json_lines(tmp_path, capsys):
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    store = Store(tmp_path / "agent.db")
    store.delivered(store.enqueue("trans", "http://api/trans", trans_body("COUPON-9")))
    store.close()

    out = tmp_path / "archive.jsonl"
    assert cli.main(["--home", str(tmp_path), "archive-export", "--out", str(out)]) == 0

    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert "COUPON-9" in json.loads(lines[0])["body"]["data"]["transactions"]
    assert "1 message(s) exported" in capsys.readouterr().err
