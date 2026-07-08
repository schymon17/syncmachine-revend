from __future__ import annotations

from typing import Any

from .db import Database


OUTBOX_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS sync_outbox (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    event_type VARCHAR(64) NOT NULL,
    source_table VARCHAR(64) NOT NULL,
    source_pk VARCHAR(191) NOT NULL,
    status VARCHAR(24) NOT NULL DEFAULT 'pending',
    attempts INT UNSIGNED NOT NULL DEFAULT 0,
    last_error TEXT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    locked_at DATETIME NULL,
    sent_at DATETIME NULL,
    PRIMARY KEY (id),
    UNIQUE KEY sync_outbox_unique_event (event_type, source_table, source_pk),
    KEY sync_outbox_status_id (status, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""


TRIGGERS = {
    "sync_user_transaction_ai": """
CREATE TRIGGER sync_user_transaction_ai
AFTER INSERT ON user_transaction
FOR EACH ROW
BEGIN
    IF NEW.transactiondone IN (2,4,5) AND NEW.print_barcode IS NOT NULL AND NEW.print_barcode <> '' THEN
        INSERT INTO sync_outbox (event_type, source_table, source_pk, status, created_at)
        VALUES ('transaction_finished', 'user_transaction', NEW.print_barcode, 'pending', NOW())
        ON DUPLICATE KEY UPDATE status='pending', last_error=NULL, created_at=NOW();
    END IF;
END
""",
    "sync_user_transaction_au": """
CREATE TRIGGER sync_user_transaction_au
AFTER UPDATE ON user_transaction
FOR EACH ROW
BEGIN
    IF NEW.transactiondone IN (2,4,5) AND NEW.print_barcode IS NOT NULL AND NEW.print_barcode <> '' THEN
        INSERT INTO sync_outbox (event_type, source_table, source_pk, status, created_at)
        VALUES ('transaction_finished', 'user_transaction', NEW.print_barcode, 'pending', NOW())
        ON DUPLICATE KEY UPDATE status='pending', last_error=NULL, created_at=NOW();
    END IF;
END
""",
    "sync_empty_record_ai": """
CREATE TRIGGER sync_empty_record_ai
AFTER INSERT ON empty_record
FOR EACH ROW
BEGIN
    INSERT INTO sync_outbox (event_type, source_table, source_pk, status, created_at)
    VALUES ('bin_record', 'empty_record', NEW.id, 'pending', NOW())
    ON DUPLICATE KEY UPDATE status='pending', last_error=NULL, created_at=NOW();
END
""",
    "sync_command_ai": """
CREATE TRIGGER sync_command_ai
AFTER INSERT ON command
FOR EACH ROW
BEGIN
    INSERT INTO sync_outbox (event_type, source_table, source_pk, status, created_at)
    VALUES ('status_changed', 'command', NEW.id, 'pending', NOW())
    ON DUPLICATE KEY UPDATE status='pending', last_error=NULL, created_at=NOW();
END
""",
    "sync_command_au": """
CREATE TRIGGER sync_command_au
AFTER UPDATE ON command
FOR EACH ROW
BEGIN
    INSERT INTO sync_outbox (event_type, source_table, source_pk, status, created_at)
    VALUES ('status_changed', 'command', NEW.id, 'pending', NOW())
    ON DUPLICATE KEY UPDATE status='pending', last_error=NULL, created_at=NOW();
END
""",
}


class Outbox:
    def __init__(self, db: Database):
        self.db = db

    def install(self, force_recreate: bool = False) -> None:
        with self.db.connect() as conn:
            Database.execute(conn, OUTBOX_TABLE_SQL)
            if force_recreate:
                for name in TRIGGERS:
                    Database.execute(conn, f"DROP TRIGGER IF EXISTS {name}")
            for name, sql in TRIGGERS.items():
                existing = Database.fetch_one(
                    conn,
                    """
                    SELECT TRIGGER_NAME
                    FROM information_schema.TRIGGERS
                    WHERE TRIGGER_SCHEMA = DATABASE() AND TRIGGER_NAME = %s
                    """,
                    (name,),
                )
                if not existing:
                    Database.execute(conn, sql)
            conn.commit()

    def claim(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = Database.fetch_all(
                conn,
                """
                SELECT * FROM sync_outbox
                WHERE status IN ('pending', 'failed')
                ORDER BY id ASC
                LIMIT %s
                """,
                (limit,),
            )
            if not rows:
                conn.commit()
                return []
            ids = [int(row["id"]) for row in rows]
            placeholders = ",".join(["%s"] * len(ids))
            Database.execute(
                conn,
                f"UPDATE sync_outbox SET status='processing', locked_at=NOW() WHERE id IN ({placeholders})",
                tuple(ids),
            )
            conn.commit()
            return rows

    def mark_done(self, ids: list[int]) -> None:
        if not ids:
            return
        with self.db.connect() as conn:
            placeholders = ",".join(["%s"] * len(ids))
            Database.execute(
                conn,
                f"UPDATE sync_outbox SET status='done', sent_at=NOW(), last_error=NULL WHERE id IN ({placeholders})",
                tuple(ids),
            )
            conn.commit()

    def mark_failed(self, ids: list[int], error: str) -> None:
        if not ids:
            return
        with self.db.connect() as conn:
            placeholders = ",".join(["%s"] * len(ids))
            Database.execute(
                conn,
                f"""
                UPDATE sync_outbox
                SET status='failed', attempts=attempts+1, last_error=%s, locked_at=NULL
                WHERE id IN ({placeholders})
                """,
                (error[:4000], *ids),
            )
            conn.commit()

    def counts(self) -> dict[str, int]:
        with self.db.connect() as conn:
            rows = Database.fetch_all(conn, "SELECT status, COUNT(*) AS count FROM sync_outbox GROUP BY status")
            conn.commit()
        return {str(row["status"]): int(row["count"]) for row in rows}

