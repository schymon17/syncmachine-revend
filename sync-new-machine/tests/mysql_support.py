"""Helpers for tests against a real MySQL: REVEND_TEST_MYSQL=host:port[,host:port] (root/test)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

SERVERS = [s for s in os.environ.get("REVEND_TEST_MYSQL", "").split(",") if s]
requires_mysql = pytest.mark.skipif(not SERVERS, reason="REVEND_TEST_MYSQL not set")
SCHEMA = "\n".join(
    line
    for line in (Path(__file__).parent / "machine_schema.sql").read_text().splitlines()
    if not line.lstrip().startswith("--")
)


def insert_item(db, coupon, dateline, done=0, status="1", metal="0", id=None):
    db.execute(
        "INSERT INTO user_transaction (id, transactionid, dateline, barcode, metal, recognitionstatus, print_barcode, "
        "bottlevalue, weight, transactiondone) VALUES (%s, %s, %s, '5901234123457', %s, %s, %s, '5', '18', %s)",
        (id, "T" + str(coupon), dateline, metal, status, coupon, done),
    )
