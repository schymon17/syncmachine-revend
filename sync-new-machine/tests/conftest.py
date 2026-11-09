from __future__ import annotations

import uuid

import pymysql
import pytest

from revend_sync.api import Client
from revend_sync.machine_db import DbSettings, MachineDb
from revend_sync.store import Store

from .fake_api import KEY_ID, SECRET, FakeApi
from .mysql_support import SCHEMA, SERVERS


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


@pytest.fixture(params=SERVERS or ["none"])
def machine(request):
    host, port = request.param.split(":")
    name = "qcs_" + uuid.uuid4().hex[:8]
    admin = pymysql.connect(host=host, port=int(port), user="root", password="test", autocommit=True)
    with admin.cursor() as cursor:
        cursor.execute(f"CREATE DATABASE {name}")
        cursor.execute(f"USE {name}")
        for statement in filter(str.strip, SCHEMA.split(";")):
            cursor.execute(statement)
    db = MachineDb(DbSettings(host=host, port=int(port), database=name, user="root", password="test"))
    assert db.check_schema() == []
    yield db, admin
    db.close()
    with admin.cursor() as cursor:
        cursor.execute(f"DROP DATABASE {name}")
    admin.close()
