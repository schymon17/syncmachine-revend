from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

import pymysql
from pymysql.cursors import DictCursor

from .config import DatabaseConfig


class Database:
    def __init__(self, cfg: DatabaseConfig):
        self.cfg = cfg

    @contextmanager
    def connect(self) -> Iterator[pymysql.connections.Connection]:
        conn = pymysql.connect(
            host=self.cfg.host,
            port=int(self.cfg.port),
            user=self.cfg.username,
            password=self.cfg.password,
            database=self.cfg.database,
            charset="utf8mb4",
            autocommit=False,
            cursorclass=DictCursor,
        )
        try:
            yield conn
        finally:
            conn.close()

    def ping(self) -> None:
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()

    @staticmethod
    def fetch_all(conn, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchall() or [])

    @staticmethod
    def fetch_one(conn, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            return dict(row) if row else None

    @staticmethod
    def execute(conn, sql: str, params: tuple[Any, ...] = ()) -> int:
        with conn.cursor() as cur:
            count = cur.execute(sql, params)
        return int(count)

