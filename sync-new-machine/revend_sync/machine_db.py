"""Read access to the machine's local MySQL database.

The agent only reads the machine's own tables, except for the product
catalogue (``barcode``) and the coupon pool (``printer_barcode``), which it
fills. It installs no triggers: a broken trigger would make the machine's
own INSERT/UPDATE fail, and polling by primary key is cheap and fast enough.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import pymysql
import pymysql.cursors

log = logging.getLogger(__name__)

# Columns the agent needs; the machine software may have more.
REQUIRED_COLUMNS = {
    "user_transaction": {
        "id",
        "dateline",
        "print_barcode",
        "transactiondone",
        "recognitionstatus",
        "barcode",
    },
    "empty_record": {"id", "dateline", "bin_type", "barcode"},
    "command": {"errorcode"},
}
OPTIONAL_TRANSACTION_COLUMNS = ("transactionid", "metal", "material", "weight", "bottlevalue")


@dataclass
class DbSettings:
    host: str = "127.0.0.1"
    port: int = 3306
    database: str = "qcs"
    user: str = "root"
    password: str = ""


class MachineDb:
    def __init__(self, settings: DbSettings):
        self._settings = settings
        self._conn: pymysql.connections.Connection | None = None
        self.columns: dict[str, set[str]] = {}

    def connect(self) -> None:
        self.close()
        s = self._settings
        self._conn = pymysql.connect(
            host=s.host,
            port=s.port,
            user=s.user,
            password=s.password,
            database=s.database,
            charset="utf8mb4",
            autocommit=True,
            connect_timeout=5,
            read_timeout=30,
            write_timeout=30,
            cursorclass=pymysql.cursors.DictCursor,
        )

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001 - closing a dead connection may fail any way
                pass
        self._conn = None

    @property
    def connected(self) -> bool:
        return self._conn is not None and self._conn.open

    def query(self, sql: str, params: Sequence[Any] | dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Run a query, reconnecting once if MySQL restarted or the connection timed out."""
        for attempt in (1, 2):
            try:
                if not self.connected:
                    self.connect()
                assert self._conn is not None
                with self._conn.cursor() as cursor:
                    cursor.execute(sql, params)
                    return list(cursor.fetchall())
            except pymysql.err.OperationalError:
                self.close()
                if attempt == 2:
                    raise
        return []

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> int:
        if not self.connected:
            self.connect()
        assert self._conn is not None
        with self._conn.cursor() as cursor:
            return cursor.execute(sql, params)

    def executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> int:
        if not self.connected:
            self.connect()
        assert self._conn is not None
        with self._conn.cursor() as cursor:
            return cursor.executemany(sql, rows)

    def table_columns(self, table: str) -> set[str]:
        rows = self.query(f"SHOW COLUMNS FROM `{table}`")
        return {str(r["Field"]) for r in rows}

    def check_schema(self) -> list[str]:
        """Missing tables/columns, as messages. Empty when the machine database fits."""
        problems = []
        for table, required in REQUIRED_COLUMNS.items():
            try:
                columns = self.table_columns(table)
            except pymysql.err.ProgrammingError:
                problems.append(f"table {table} is missing")
                continue
            self.columns[table] = columns
            missing = sorted(required - columns)
            if missing:
                problems.append(f"table {table} lacks columns: {', '.join(missing)}")
        return problems

    def transaction_select(self) -> str:
        columns = REQUIRED_COLUMNS["user_transaction"] | {
            c for c in OPTIONAL_TRANSACTION_COLUMNS if c in self.columns.get("user_transaction", set())
        }
        return ", ".join(f"`{c}`" for c in sorted(columns))
