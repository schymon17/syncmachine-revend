from __future__ import annotations

import json
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .config import AppConfig
from .db import Database
from .http_client import ApiClient
from .logging_store import LogStore
from .outbox import Outbox
from .paths import data_path
from .state import JsonState


StatusCallback = Callable[[dict[str, Any]], None]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SyncEngine:
    def __init__(self, cfg: AppConfig, log: LogStore, status_callback: StatusCallback | None = None):
        self.cfg = cfg
        self.log = log
        self.status_callback = status_callback
        self.db = Database(cfg.database)
        self.http = ApiClient(cfg.api)
        self.outbox = Outbox(self.db)
        self.state = JsonState()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.queue_file = data_path("offline-transactions.jsonl")
        self.last_heartbeat = 0.0
        self.last_remote_refresh = 0.0
        self.started_at: float | None = None

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self.run, name="sync-engine", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)

    def emit(self, **payload: Any) -> None:
        if self.status_callback:
            self.status_callback(payload)

    def run(self) -> None:
        self.started_at = time.time()
        self.log.log("INFO", "Sync engine started", machine_id=self.cfg.machine_id)
        if self.cfg.sync.install_triggers:
            try:
                self.outbox.install()
                self.log.log("INFO", "DB watcher installed")
            except Exception as exc:
                self.log.log("ERROR", "DB watcher install failed; fallback scan will be used", error=str(exc))

        while not self.stop_event.is_set():
            loop_start = time.time()
            try:
                self.flush_offline_queue()
                claimed = self.outbox.claim(limit=50)
                if claimed:
                    self.process_events(claimed)
                else:
                    self.fallback_scan()
                now = time.time()
                if self.cfg.sync.enabled_heartbeat and now - self.last_heartbeat >= self.cfg.sync.heartbeat_interval_seconds:
                    self.send_heartbeat()
                    self.last_heartbeat = now
                if now - self.last_remote_refresh >= self.cfg.sync.remote_refresh_seconds:
                    self.run_remote_refresh()
                    self.last_remote_refresh = now
                self.emit_status()
            except Exception as exc:
                self.log.log("ERROR", "Sync loop failed", error=str(exc))
                self.emit(state="error", message=str(exc))
            sleep_for = max(0.2, float(self.cfg.sync.outbox_poll_seconds) - (time.time() - loop_start))
            self.stop_event.wait(sleep_for)
        self.log.log("INFO", "Sync engine stopped")

    def emit_status(self) -> None:
        try:
            counts = self.outbox.counts()
        except Exception:
            counts = {}
        self.emit(
            state="running",
            pending=counts.get("pending", 0) + counts.get("failed", 0),
            done=counts.get("done", 0),
            offline_queue=self.offline_queue_count(),
        )

    def process_events(self, rows: list[dict[str, Any]]) -> None:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            grouped[str(row["event_type"])].append(row)

        for event_type, event_rows in grouped.items():
            ids = [int(row["id"]) for row in event_rows]
            try:
                if event_type == "transaction_finished" and self.cfg.sync.enabled_transactions:
                    self.send_transactions([str(row["source_pk"]) for row in event_rows])
                elif event_type == "bin_record" and self.cfg.sync.enabled_bins:
                    self.send_bins([int(row["source_pk"]) for row in event_rows if str(row["source_pk"]).isdigit()])
                elif event_type == "status_changed" and self.cfg.sync.enabled_status:
                    self.send_status()
                else:
                    self.log.log("INFO", "Ignored outbox event", event_type=event_type, count=len(event_rows))
                self.outbox.mark_done(ids)
            except Exception as exc:
                self.outbox.mark_failed(ids, str(exc))
                self.log.log("WARN", "Outbox event failed", event_type=event_type, error=str(exc))

    def fallback_scan(self) -> None:
        try:
            last_sync = int(self.state.data.get("fallback_user_transaction_lastSync", 0) or 0)
            overlap = max(0, int(self.cfg.sync.transaction_overlap_seconds))
            query_sync = max(0, last_sync - overlap)
            with self.db.connect() as conn:
                rows = Database.fetch_all(
                    conn,
                    """
                    SELECT print_barcode AS transaction_id, MAX(dateline) AS max_dateline
                    FROM user_transaction
                    WHERE dateline > %s
                      AND transactiondone IN (2,4,5)
                      AND print_barcode IS NOT NULL
                      AND print_barcode <> ''
                    GROUP BY print_barcode
                    ORDER BY max_dateline ASC
                    LIMIT %s
                    """,
                    (query_sync, int(self.cfg.sync.transaction_batch)),
                )
                conn.commit()
            ids = [str(row["transaction_id"]) for row in rows]
            if ids:
                self.send_transactions(ids)
        except Exception:
            return

    def base_payload(self, kind: str) -> dict[str, Any]:
        payload = {
            "machineId": self.cfg.machine_id,
            "timestamp": utc_now(),
            "kind": kind,
            "integration": self.cfg.integration or None,
        }
        return payload

    def send_transactions(self, transaction_ids: list[str]) -> None:
        ids = [tid for tid in dict.fromkeys(transaction_ids) if tid]
        if not ids:
            return
        ids = ids[: int(self.cfg.sync.max_transactions_per_payload)]
        placeholders = ",".join(["%s"] * len(ids))
        with self.db.connect() as conn:
            rows = Database.fetch_all(
                conn,
                f"""
                SELECT *
                FROM user_transaction
                WHERE print_barcode IN ({placeholders})
                ORDER BY print_barcode ASC, dateline ASC
                """,
                tuple(ids),
            )
            conn.commit()

        data: dict[str, dict[str, Any]] = {}
        rows_count = 0
        max_dateline = int(self.state.data.get("fallback_user_transaction_lastSync", 0) or 0)
        for row in rows:
            tid = str(row.get("print_barcode") or "")
            if not tid:
                continue
            data.setdefault(tid, {"details": [], "last_transaction_time": None})
            dateline = int(row.get("dateline") or 0)
            row["datetime"] = datetime.fromtimestamp(dateline, timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if dateline else None
            data[tid]["details"].append(row)
            rows_count += 1
            if dateline > max_dateline:
                max_dateline = dateline
            if dateline:
                data[tid]["last_transaction_time"] = row["datetime"]
            if rows_count >= int(self.cfg.sync.max_rows_per_payload):
                break

        if not data:
            return
        payload = self.base_payload("transactions")
        payload["data"] = {"transactions": data, "mid": self.cfg.machine_id}
        try:
            self.http.post_ok("/trans", payload)
        except Exception as exc:
            self.queue_offline("/trans", payload, "transactions", str(exc))
            raise
        self.state.data["fallback_user_transaction_lastSync"] = max_dateline
        self.state.save()
        self.log.log("INFO", "Transactions sent", transactions=len(data), rows=rows_count, to=max_dateline)

    def send_bins(self, source_ids: list[int]) -> None:
        ids = [sid for sid in dict.fromkeys(source_ids) if sid > 0]
        if not ids:
            return
        placeholders = ",".join(["%s"] * len(ids))
        with self.db.connect() as conn:
            rows = Database.fetch_all(
                conn,
                f"SELECT id, mid, dateline, bin_type, barcode FROM empty_record WHERE id IN ({placeholders}) ORDER BY id ASC",
                tuple(ids),
            )
            conn.commit()
        if not rows:
            return
        payload = self.base_payload("sync_bins")
        payload["data"] = {"empty_records": rows[: int(self.cfg.sync.bins_batch)]}
        self.http.post_ok("/bins", payload)
        self.state.data["empty_records_last_id"] = max(int(row["id"]) for row in rows)
        self.state.save()
        self.log.log("INFO", "Bins sent", rows=len(rows))

    def send_status(self) -> None:
        with self.db.connect() as conn:
            row = Database.fetch_one(conn, "SELECT * FROM command ORDER BY id DESC LIMIT 1")
            conn.commit()
        payload = self.base_payload("status")
        payload["data"] = {"command": row or {}}
        if self.cfg.address:
            payload["address"] = self.cfg.address
        if self.cfg.notification_emails:
            payload["notification_emails"] = self.cfg.notification_emails
        if self.cfg.notification_emails_bcc:
            payload["notification_emails_bcc"] = self.cfg.notification_emails_bcc
        self.http.post_ok("/status", payload)
        self.log.log("INFO", "Status sent", command_id=(row or {}).get("id"))

    def send_heartbeat(self) -> None:
        payload = self.base_payload("heartbeat")
        self.http.post_ok("/heartbeat", payload)
        self.state.data["last_heartbeat_sent_at"] = payload["timestamp"]
        self.state.save()
        self.log.log("INFO", "Heartbeat sent")

    def run_remote_refresh(self) -> None:
        if self.cfg.sync.enabled_eans:
            self.fetch_eans()
        if self.cfg.sync.enabled_coupons:
            self.fetch_coupons()
        if self.cfg.sync.enabled_adverts:
            self.fetch_adverts()

    def fetch_eans(self) -> None:
        try:
            body = self.http.post_ok("/eans", self.base_payload("eans"))
            attrs = body.get("data", {}).get("attributes", []) if isinstance(body, dict) else []
            digest = str(hash(json.dumps(attrs, sort_keys=True, default=str)))
            if self.state.data.get("eans_hash") == digest:
                return
            self.import_eans(attrs if isinstance(attrs, list) else [])
            self.state.data["eans_hash"] = digest
            self.state.save()
        except Exception as exc:
            self.log.log("WARN", "EAN refresh failed", error=str(exc))

    def import_eans(self, items: list[dict[str, Any]]) -> None:
        if not items:
            return
        with self.db.connect() as conn:
            try:
                Database.execute(conn, "TRUNCATE TABLE barcode")
            except Exception:
                Database.execute(conn, "DELETE FROM barcode")
            sql = """
            INSERT INTO barcode
            (barcode, brand, bottleinfo, value, maxsdiam, minsdiam, maxbdiam, minbdiam, material_type, metal, capacity, weight, version)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """
            with conn.cursor() as cur:
                for item in items:
                    bottleinfo = item.get("bottleinfo")
                    if isinstance(bottleinfo, (dict, list)):
                        bottleinfo = json.dumps(bottleinfo, ensure_ascii=False)
                    cur.execute(
                        sql,
                        (
                            item.get("barcode"),
                            item.get("brand"),
                            bottleinfo,
                            item.get("value"),
                            item.get("maxsdiam"),
                            item.get("minsdiam"),
                            item.get("maxbdiam"),
                            item.get("minbdiam"),
                            item.get("material_type"),
                            int(item["metal"]) if item.get("metal") is not None else None,
                            item.get("capacity"),
                            item.get("weight"),
                            item.get("version"),
                        ),
                    )
            conn.commit()
        self.log.log("INFO", "EANs imported", count=len(items))

    def fetch_coupons(self) -> None:
        try:
            with self.db.connect() as conn:
                existing = Database.fetch_one(conn, "SELECT COUNT(*) AS c FROM printer_barcode")
                conn.commit()
            current = int((existing or {}).get("c") or 0)
            if current >= 25:
                return
            body = self.http.post_ok("/coupons", self.base_payload("coupons"))
            attrs = body.get("data", {}).get("attributes", []) if isinstance(body, dict) else []
            barcodes = []
            for item in attrs if isinstance(attrs, list) else []:
                barcode = item.get("barcode") if isinstance(item, dict) else item
                if barcode:
                    barcodes.append(str(barcode))
            self.insert_coupons(barcodes[: max(0, 50 - current)])
        except Exception as exc:
            self.log.log("WARN", "Coupons refresh failed", error=str(exc))

    def insert_coupons(self, barcodes: list[str]) -> None:
        if not barcodes:
            return
        with self.db.connect() as conn:
            with conn.cursor() as cur:
                for barcode in dict.fromkeys(barcodes):
                    cur.execute("INSERT IGNORE INTO printer_barcode (barcode) VALUES (%s)", (barcode,))
            conn.commit()
        self.log.log("INFO", "Coupons queued", count=len(barcodes))

    def fetch_adverts(self) -> None:
        try:
            body = self.http.post_ok("/adverts", self.base_payload("adverts"))
            digest = str(hash(json.dumps(body, sort_keys=True, default=str)))
            if self.state.data.get("adverts_hash") != digest:
                self.state.data["adverts_hash"] = digest
                self.state.save()
                self.log.log("INFO", "Adverts metadata refreshed")
        except Exception as exc:
            self.log.log("WARN", "Adverts refresh failed", error=str(exc))

    def queue_offline(self, endpoint: str, payload: dict[str, Any], kind: str, error: str) -> None:
        if kind != "transactions":
            return
        self.queue_file.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "endpoint": endpoint,
            "payload": payload,
            "kind": kind,
            "error": error,
            "queuedAt": utc_now(),
        }
        with self.queue_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")

    def flush_offline_queue(self) -> None:
        if not self.queue_file.exists():
            return
        lines = self.queue_file.read_text(encoding="utf-8").splitlines()
        if not lines:
            return
        remain: list[str] = []
        for line in lines:
            try:
                entry = json.loads(line)
                self.http.post_ok(entry["endpoint"], entry["payload"])
                self.log.log("INFO", "Offline transaction flushed")
            except Exception:
                remain.append(line)
        self.queue_file.write_text(("\n".join(remain) + "\n") if remain else "", encoding="utf-8")

    def offline_queue_count(self) -> int:
        if not self.queue_file.exists():
            return 0
        return len([line for line in self.queue_file.read_text(encoding="utf-8").splitlines() if line.strip()])
