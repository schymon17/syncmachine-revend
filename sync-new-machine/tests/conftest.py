from __future__ import annotations

import pytest

from revend_sync.api import Client
from revend_sync.store import Store

from .fake_api import KEY_ID, SECRET, FakeApi


@pytest.fixture
def api():
    server = FakeApi().start()
    yield server
    server.stop()


@pytest.fixture
def client():
    return Client(KEY_ID, SECRET)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "agent.db")
    yield s
    s.close()
