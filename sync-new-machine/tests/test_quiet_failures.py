"""A rejected key, a flapping connection and a backlog must not flood the API or the panel."""

from __future__ import annotations

import logging

import pytest

from revend_sync.agent import Agent, Task
from revend_sync.api import AUTH_PAUSE_SECONDS, OUTAGE_REPORT_SECONDS, ApiError, Client, encode_body
from revend_sync.sender import SENDS_PER_MINUTE, Sender

from .fake_api import KEY_ID, MACHINE_ID, SECRET

DEAD_URL = "http://127.0.0.1:9/api/revend/machine/v2/heartbeat"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def heartbeat_body():
    return encode_body({"machineId": MACHINE_ID, "timestamp": "2026-11-13T00:00:00Z", "kind": "heartbeat"})


def requests_seen(api):
    return len(api.nonces)


def test_a_rejected_key_pauses_every_request_and_is_logged_once(api, caplog):
    clock = Clock()
    client = Client("deleted-machine-key", SECRET, monotonic=clock)
    url = api.base + "/api/revend/machine/v2/heartbeat"

    with caplog.at_level(logging.WARNING):
        with pytest.raises(ApiError) as first:
            client.post(url, heartbeat_body())
        for _ in range(5):
            with pytest.raises(ApiError) as paused:
                client.post(url, heartbeat_body())
            assert paused.value.auth and paused.value.code == "auth_paused"

    assert first.value.auth and first.value.status == 401
    assert caplog.text.count("API key rejected") == 1
    assert client.auth_rejected


def test_after_the_pause_one_probe_goes_out_and_a_good_key_resumes(api, caplog):
    clock = Clock()
    client = Client(KEY_ID, SECRET, monotonic=clock)
    url = api.base + "/api/revend/machine/v2/heartbeat"
    client._key_rejected(401, "unauthorized")

    with pytest.raises(ApiError):
        client.post(url, heartbeat_body())
    assert api.got("heartbeat") == []

    clock.now += AUTH_PAUSE_SECONDS
    with caplog.at_level(logging.WARNING):
        assert client.post(url, heartbeat_body()).status == 202
    assert not client.auth_rejected
    assert "accepted again" in caplog.text


def test_a_rejected_key_keeps_messages_instead_of_dead_letters(api, store):
    clock = Clock()
    client = Client("deleted-machine-key", SECRET, monotonic=clock)
    for _ in range(3):
        store.enqueue("heartbeat", api.base + "/api/revend/machine/v2/heartbeat", heartbeat_body())
    sender = Sender(store, client, now=clock)

    assert sender.run_once() == 0
    stats = store.stats()
    assert (stats["pending"], stats["failed"], stats["dead"]) == (3, 0, 0)
    assert sender._paused_until == clock.now + AUTH_PAUSE_SECONDS
    assert sender.run_once() == 0  # paused: nothing goes out


def test_a_flapping_connection_is_reported_once_an_hour_with_a_count(caplog):
    clock = Clock()
    client = Client(KEY_ID, SECRET, monotonic=clock)
    caplog.set_level(logging.WARNING)

    for _ in range(6):  # down and back every 5 minutes
        with pytest.raises(ApiError):
            client.post(DEAD_URL, b"{}")
        clock.now += 60
        client._came_online()
        clock.now += 240
    assert caplog.text.count("API unreachable") == 1

    clock.now += OUTAGE_REPORT_SECONDS
    with pytest.raises(ApiError):
        client.post(DEAD_URL, b"{}")
    reports = [r for r in caplog.records if r.getMessage().startswith("API unreachable")]
    assert len(reports) == 2
    assert reports[1].context["earlierOutages"] == 5
    assert reports[1].context["earlierOfflineSeconds"] == 360


def test_tasks_do_not_log_an_outage_or_a_rejected_key_themselves(caplog):
    agent = Agent.__new__(Agent)
    agent.db_ok = None

    def fail(error):
        def run():
            raise error

        return run

    with caplog.at_level(logging.WARNING):
        Agent._run(
            agent, Task("adverts", 300, fail(ApiError("ConnectionError", status=None, retryable=True)))
        )
        Agent._run(
            agent, Task("commands", 60, fail(ApiError("HTTP 401", status=401, retryable=False, auth=True)))
        )

    assert "Task" not in caplog.text


def test_a_task_that_fails_recovers_and_fails_again_logs_once_per_10_minutes(caplog):
    agent = Agent.__new__(Agent)
    agent.db_ok = None
    failing = {"on": True}

    def run():
        if failing["on"]:
            raise ApiError("HTTP 503", status=503, retryable=True)

    task = Task("adverts", 300, run)
    with caplog.at_level(logging.WARNING):
        for _ in range(4):
            failing["on"] = True
            Agent._run(agent, task)
            failing["on"] = False
            Agent._run(agent, task)

    assert caplog.text.count("Task adverts failed") == 1


def test_a_backlog_goes_out_below_the_api_rate_limit(api, client, store):
    clock = Clock()
    url = api.base + "/api/revend/machine/v2/heartbeat"
    for _ in range(SENDS_PER_MINUTE + 30):
        store.enqueue("heartbeat", url, heartbeat_body())
    sender = Sender(store, client, now=clock)

    sent = sum(sender.run_once() for _ in range(30))  # 30 runs within the same minute
    assert sent == SENDS_PER_MINUTE

    clock.now += 60
    assert sender.run_once() == 20  # the next minute, up to the per-run limit
