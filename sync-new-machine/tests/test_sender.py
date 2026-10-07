from __future__ import annotations

import json
import logging

from revend_sync import logs
from revend_sync.api import encode_body
from revend_sync.sender import Sender

from .fake_api import MACHINE_ID


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def heartbeat_body():
    return encode_body({"machineId": MACHINE_ID, "timestamp": "2026-10-07T00:00:00Z", "kind": "heartbeat"})


def test_queued_messages_are_delivered_in_order(api, client, store):
    url = api.base + "/api/revend/machine/v2/logs"
    for i in range(3):
        store.enqueue("logs", url, encode_body({"machineId": MACHINE_ID, "entries": [{"n": i}]}))

    assert Sender(store, client).run_once() == 3
    assert [p["entries"][0]["n"] for p in api.got("logs")] == [0, 1, 2]
    assert store.stats()["pending"] == 0
    assert store.get("last_sent_at") is not None


def test_offline_keeps_everything_and_sends_it_when_back(api, client, store):
    clock = Clock()
    good_url = api.base + "/api/revend/machine/v2/heartbeat"
    store.enqueue("heartbeat", "http://127.0.0.1:9/api/revend/machine/v2/heartbeat", heartbeat_body())
    sender = Sender(store, client, now=clock)

    assert sender.run_once() == 0
    assert sender.online is False
    assert store.stats()["pending"] + store.stats()["failed"] == 1
    # Paused: no hammering while offline.
    assert sender.run_once() == 0

    # Connection back (here: the message points at the live server).
    store._db.execute("UPDATE outbox SET url = ?, next_attempt_at = 0", (good_url,))
    clock.now += 120
    assert sender.run_once() == 1
    assert sender.online is True


def test_a_rejected_message_becomes_a_dead_letter_and_does_not_block_others(api, client, store, caplog):
    api.reject_seals.add("USED-SEAL")
    url = api.base + "/api/revend/machine/v2/bins"
    for seal in ("USED-SEAL", "GOOD-SEAL"):
        store.enqueue(
            "bins",
            url,
            encode_body(
                {
                    "machineId": MACHINE_ID,
                    "data": {"empty_records": [{"barcode": seal, "bin_type": "pet", "dateline": 1790000000}]},
                }
            ),
        )

    with caplog.at_level(logging.ERROR):
        assert Sender(store, client).run_once() == 1

    assert [p["data"]["empty_records"][0]["barcode"] for p in api.got("bins")] == ["GOOD-SEAL"]
    stats = store.stats()
    assert (stats["pending"], stats["dead"]) == (0, 1)
    assert "seal_already_used" in store.dead_letters()[0]["last_error"]
    assert "rejected" in caplog.text


def test_a_server_error_is_retried_under_a_new_idempotency_key(api, client, store):
    store.enqueue("heartbeat", api.base + "/api/revend/machine/v2/heartbeat", heartbeat_body())
    key_before = store._db.execute("SELECT idempotency_key FROM outbox").fetchone()[0]

    api.fail_with = 500
    assert Sender(store, client).run_once() == 0
    key_after, attempts = store._db.execute("SELECT idempotency_key, attempts FROM outbox").fetchone()
    assert key_after != key_before and attempts == 1

    api.fail_with = None
    store._db.execute("UPDATE outbox SET next_attempt_at = 0")
    assert Sender(store, client).run_once() == 1


def test_only_the_newest_unsent_status_is_kept(store):
    for level in (10, 20, 30):
        store.enqueue("status", "http://x/status", encode_body({"level": level}), replace_pending=True)
    rows = store._db.execute("SELECT body FROM outbox").fetchall()
    assert [json.loads(r[0])["level"] for r in rows] == [30]


def test_warnings_are_buffered_for_the_panel_without_secrets(api, client, store, tmp_path):
    logs.setup(tmp_path / "logs", store)
    logging.getLogger("revend_sync.test").warning(
        "Something odd", extra={"context": {"seal": "S1", "secret": "abc"}}
    )
    logging.getLogger("revend_sync.test").info("Routine")

    assert logs.queue_upload(store, MACHINE_ID, api.base + "/api/revend/machine/v2/logs") == 1
    assert Sender(store, client).run_once() == 1
    entry = api.got("logs")[0]["entries"][0]
    assert entry["level"] == "warning" and entry["message"] == "Something odd"
    assert entry["context"] == {"seal": "S1", "secret": "***"}
    assert "abc" not in (tmp_path / "logs" / "agent.log").read_text()
    logging.getLogger().handlers.clear()


def test_a_rejected_log_upload_does_not_create_another_upload(api, client, store, tmp_path):
    logs.setup(tmp_path / "logs", store)
    api.fail_with = 422
    store.enqueue(
        "logs",
        api.base + "/api/revend/machine/v2/logs",
        encode_body({"machineId": MACHINE_ID, "entries": []}),
    )

    Sender(store, client).run_once()

    assert logs.queue_upload(store, MACHINE_ID, api.base + "/api/revend/machine/v2/logs") == 0
    logging.getLogger().handlers.clear()
