from __future__ import annotations

import queue
import socket
import threading
import tkinter as tk
from datetime import datetime, timezone
from tkinter import messagebox
from typing import Any

from .bootstrap import bootstrap_remote_config
from .config import AppConfig, load_config, save_config
from .db import Database
from .dev_seed import DevSeeder, apply_dev_defaults, result_to_text
from .http_client import ApiClient
from .logging_store import LogStore
from .outbox import Outbox
from .state import JsonState
from .sync_engine import SyncEngine


BG = "#f3f5f7"
PANEL = "#ffffff"
TEXT = "#17202a"
MUTED = "#667085"
OK = "#047857"
BAD = "#b42318"
WARN = "#b54708"


class SyncApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Revend Sync New")
        self.geometry("1120x760")
        self.minsize(980, 680)
        self.configure(bg=BG)

        self.cfg = load_config()
        self.log = LogStore()
        self.state = JsonState()
        self.engine: SyncEngine | None = None
        self.ui_events: queue.Queue[dict[str, Any]] = queue.Queue()

        self.vars: dict[str, tk.Variable] = {}
        self.metric_vars: dict[str, tk.StringVar] = {}
        self.status_var = tk.StringVar(value="Zatrzymany")

        self._build_ui()
        self._load_vars()
        self.after(100, self._show_window)
        self.after(900, self.refresh_dashboard)
        self.after(250, self._drain_events)

    def _show_window(self) -> None:
        self.deiconify()
        self.lift()

    def _build_ui(self) -> None:
        root = tk.Frame(self, bg=BG)
        root.pack(fill="both", expand=True)

        header = tk.Frame(root, bg="#101828", padx=18, pady=14)
        header.pack(fill="x")
        tk.Label(header, text="Revend Sync New", bg="#101828", fg="white", font=("Segoe UI", 20, "bold")).pack(side="left")
        tk.Label(header, textvariable=self.status_var, bg="#101828", fg="#d0d5dd", font=("Segoe UI", 12)).pack(side="right")

        body = tk.Frame(root, bg=BG, padx=16, pady=16)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=3)
        body.columnconfigure(1, weight=2)
        body.rowconfigure(1, weight=1)

        self._build_dashboard(body)
        self._build_actions(body)
        self._build_config(body)
        self._build_log_panel(body)

    def _panel(self, parent: tk.Widget, title: str) -> tk.Frame:
        panel = tk.Frame(parent, bg=PANEL, padx=14, pady=12, highlightthickness=1, highlightbackground="#d0d5dd")
        tk.Label(panel, text=title, bg=PANEL, fg=TEXT, font=("Segoe UI", 13, "bold")).pack(anchor="w", pady=(0, 10))
        return panel

    def _build_dashboard(self, parent: tk.Frame) -> None:
        panel = self._panel(parent, "Stan maszyny i polaczen")
        panel.grid(row=0, column=0, sticky="nsew", padx=(0, 12), pady=(0, 12))
        panel.columnconfigure(0, weight=1)
        panel.columnconfigure(1, weight=1)

        metrics = [
            ("db", "Baza danych"),
            ("internet", "Internet"),
            ("machine_id", "Numer seryjny / Machine ID"),
            ("machine_db", "Maszyna w bazie"),
            ("api", "API base URL"),
            ("watcher", "Watcher / triggery"),
            ("outbox", "Outbox"),
            ("heartbeat", "Ostatni heartbeat"),
        ]
        grid = tk.Frame(panel, bg=PANEL)
        grid.pack(fill="x")
        for idx, (key, label) in enumerate(metrics):
            cell = tk.Frame(grid, bg="#f8fafc", padx=10, pady=8, highlightthickness=1, highlightbackground="#e4e7ec")
            cell.grid(row=idx // 2, column=idx % 2, sticky="ew", padx=4, pady=4)
            grid.columnconfigure(idx % 2, weight=1)
            tk.Label(cell, text=label, bg="#f8fafc", fg=MUTED, font=("Segoe UI", 10)).pack(anchor="w")
            var = tk.StringVar(value="Sprawdzam...")
            self.metric_vars[key] = var
            tk.Label(cell, textvariable=var, bg="#f8fafc", fg=TEXT, font=("Segoe UI", 11, "bold"), wraplength=420, justify="left").pack(anchor="w")

    def _build_actions(self, parent: tk.Frame) -> None:
        panel = self._panel(parent, "Akcje")
        panel.grid(row=0, column=1, sticky="nsew", pady=(0, 12))
        buttons = [
            ("Odswiez status", self.refresh_dashboard),
            ("Test bazy", self.test_db),
            ("Test internetu", self.test_internet),
            ("Pobierz config z API", self.bootstrap),
            ("Instaluj watcher", self.install_watcher),
            ("Start sync", self.start_sync),
            ("Stop sync", self.stop_sync),
            ("Heartbeat teraz", self.heartbeat_now),
        ]
        for text, command in buttons:
            tk.Button(panel, text=text, command=command, padx=10, pady=7).pack(fill="x", pady=3)

        tk.Label(panel, text="Sztuczne dane", bg=PANEL, fg=TEXT, font=("Segoe UI", 12, "bold")).pack(anchor="w", pady=(14, 6))
        for text, kind in [
            ("Dodaj transakcje", "transactions"),
            ("Dodaj bin", "bins"),
            ("Dodaj status", "status"),
            ("Dodaj wszystko", "all"),
        ]:
            tk.Button(panel, text=text, command=lambda k=kind: self.seed_dev(k), padx=10, pady=7).pack(fill="x", pady=3)

    def _build_config(self, parent: tk.Frame) -> None:
        panel = self._panel(parent, "Konfiguracja")
        panel.grid(row=1, column=0, sticky="nsew", padx=(0, 12))
        panel.columnconfigure(1, weight=1)

        fields = [
            ("Machine ID", "machine_id", None),
            ("URL konfiguracji API v2", "config_url", None),
            ("Token API", "token", "*"),
            ("API base URL", "base_url", None),
            ("Integracja", "integration", None),
            ("DB host", "db_host", None),
            ("DB port", "db_port", None),
            ("DB nazwa", "db_database", None),
            ("DB user", "db_username", None),
            ("DB haslo", "db_password", "*"),
        ]
        for row, (label, key, show) in enumerate(fields):
            tk.Label(panel, text=label, bg=PANEL, fg=TEXT).grid(row=row, column=0, sticky="w", pady=3)
            var = tk.StringVar()
            self.vars[key] = var
            tk.Entry(panel, textvariable=var, show=show or "", relief="solid", bd=1).grid(row=row, column=1, sticky="ew", pady=3, padx=(10, 0))

        self.vars["install_triggers"] = tk.BooleanVar()
        tk.Checkbutton(panel, text="Instaluj watcher/triggery w bazie", variable=self.vars["install_triggers"], bg=PANEL).grid(
            row=len(fields), column=0, columnspan=2, sticky="w", pady=(8, 4)
        )
        tk.Button(panel, text="Ustaw statyczna baze qcs", command=self.apply_dev_db_config).grid(row=len(fields) + 1, column=0, sticky="w", pady=(10, 0))
        tk.Button(panel, text="Zapisz konfiguracje", command=self.save_from_form).grid(row=len(fields) + 1, column=1, sticky="e", pady=(10, 0))

    def _build_log_panel(self, parent: tk.Frame) -> None:
        panel = self._panel(parent, "Zdarzenia")
        panel.grid(row=1, column=1, sticky="nsew")
        panel.rowconfigure(0, weight=1)
        self.log_text = tk.Text(panel, height=18, wrap="word", bg="#0b1220", fg="#e5e7eb", insertbackground="white")
        self.log_text.pack(fill="both", expand=True)

    def _load_vars(self) -> None:
        mapping = {
            "machine_id": self.cfg.machine_id,
            "config_url": self.cfg.api.config_url,
            "token": self.cfg.api.token,
            "base_url": self.cfg.api.base_url,
            "integration": self.cfg.integration,
            "db_host": self.cfg.database.host,
            "db_port": str(self.cfg.database.port),
            "db_database": self.cfg.database.database,
            "db_username": self.cfg.database.username,
            "db_password": self.cfg.database.password,
            "install_triggers": self.cfg.sync.install_triggers,
        }
        for key, value in mapping.items():
            self.vars[key].set(value)

    def _apply_form_to_config(self) -> AppConfig:
        self.cfg.machine_id = str(self.vars["machine_id"].get()).strip()
        self.cfg.api.config_url = str(self.vars["config_url"].get()).strip()
        self.cfg.api.token = str(self.vars["token"].get()).strip()
        self.cfg.api.base_url = str(self.vars["base_url"].get()).strip()
        self.cfg.integration = str(self.vars["integration"].get()).strip()
        self.cfg.database.host = str(self.vars["db_host"].get()).strip()
        self.cfg.database.port = int(str(self.vars["db_port"].get()).strip() or 3306)
        self.cfg.database.database = str(self.vars["db_database"].get()).strip()
        self.cfg.database.username = str(self.vars["db_username"].get()).strip()
        self.cfg.database.password = str(self.vars["db_password"].get())
        self.cfg.sync.install_triggers = bool(self.vars["install_triggers"].get())
        return self.cfg

    def _set_metric(self, key: str, text: str) -> None:
        self.metric_vars[key].set(text)

    def append_log(self, line: str) -> None:
        self.log_text.insert("end", line + "\n")
        self.log_text.see("end")

    def run_async(self, title: str, fn) -> None:
        def worker():
            try:
                result = fn()
                self.ui_events.put({"kind": "message", "message": f"{title}: OK", "result": result})
            except Exception as exc:
                self.ui_events.put({"kind": "error", "message": f"{title}: {exc}"})

        threading.Thread(target=worker, daemon=True).start()

    def refresh_dashboard(self) -> None:
        def work() -> dict[str, str]:
            cfg = self._apply_form_to_config()
            result: dict[str, str] = {}
            result["machine_id"] = cfg.machine_id or "BRAK - wpisz/pobierz konfiguracje"
            result["api"] = cfg.api.base_url or "BRAK"
            result["internet"] = "ONLINE" if self._internet_online() else "OFFLINE"
            result["heartbeat"] = str(self.state.load().get("last_heartbeat_sent_at") or "Brak wyslanego heartbeat")

            db = Database(cfg.database)
            try:
                db.ping()
                result["db"] = f"OK: {cfg.database.host}:{cfg.database.port}/{cfg.database.database}"
                with db.connect() as conn:
                    machine = Database.fetch_one(conn, "SELECT id, mid FROM machineinformation WHERE mid=%s LIMIT 1", (cfg.machine_id,))
                    triggers = Database.fetch_all(
                        conn,
                        """
                        SELECT TRIGGER_NAME FROM information_schema.TRIGGERS
                        WHERE TRIGGER_SCHEMA = DATABASE()
                          AND TRIGGER_NAME IN ('sync_user_transaction_ai','sync_user_transaction_au','sync_empty_record_ai','sync_command_ai','sync_command_au')
                        """,
                    )
                    try:
                        counts = Outbox(db).counts()
                    except Exception:
                        counts = {}
                    conn.commit()
                result["machine_db"] = "TAK" if machine else "NIE - brak wpisu w machineinformation"
                result["watcher"] = f"{len(triggers)}/5 triggerow"
                result["outbox"] = ", ".join(f"{k}: {v}" for k, v in counts.items()) if counts else "pusto / brak tabeli"
            except Exception as exc:
                result["db"] = f"BLAD: {exc}"
                result["machine_db"] = "Nie sprawdzono"
                result["watcher"] = "Nie sprawdzono"
                result["outbox"] = "Nie sprawdzono"
            return result

        def worker():
            self.ui_events.put({"kind": "diagnostics", "data": work()})

        threading.Thread(target=worker, daemon=True).start()

    def _internet_online(self) -> bool:
        try:
            with socket.create_connection(("1.1.1.1", 443), timeout=3):
                return True
        except OSError:
            return False

    def save_from_form(self) -> None:
        try:
            save_config(self._apply_form_to_config())
            self.append_log("Konfiguracja zapisana.")
            self.refresh_dashboard()
        except Exception as exc:
            messagebox.showerror("Blad zapisu", str(exc))

    def apply_dev_db_config(self) -> None:
        self.cfg = apply_dev_defaults(self._apply_form_to_config())
        save_config(self.cfg)
        self._load_vars()
        self.append_log("Ustawiono statyczna baze: 127.0.0.1:3306 / qcs / root.")

    def test_db(self) -> None:
        self.run_async("Test bazy", lambda: Database(self._apply_form_to_config().database).ping())

    def test_internet(self) -> None:
        self.run_async("Test internetu", lambda: (_ for _ in ()).throw(RuntimeError("Brak internetu")) if not self._internet_online() else True)

    def bootstrap(self) -> None:
        def work():
            cfg = self._apply_form_to_config()
            if not cfg.machine_id:
                raise RuntimeError("Machine ID jest wymagany")
            new_cfg = bootstrap_remote_config(cfg, cfg.machine_id)
            save_config(new_cfg)
            self.cfg = new_cfg
            return new_cfg

        self.run_async("Pobieranie konfiguracji", work)

    def install_watcher(self) -> None:
        self.run_async("Instalacja watchera", lambda: Outbox(Database(self._apply_form_to_config().database)).install(True))

    def start_sync(self) -> None:
        try:
            save_config(self._apply_form_to_config())
            self.engine = SyncEngine(self.cfg, self.log, self.ui_events.put)
            self.engine.start()
            self.status_var.set("Uruchomiony")
            self.append_log("Synchronizacja wystartowala.")
        except Exception as exc:
            messagebox.showerror("Start failed", str(exc))

    def stop_sync(self) -> None:
        if self.engine:
            self.engine.stop()
        self.status_var.set("Zatrzymany")
        self.append_log("Synchronizacja zatrzymana.")

    def heartbeat_now(self) -> None:
        if not self.engine:
            self.engine = SyncEngine(self._apply_form_to_config(), self.log, self.ui_events.put)
        self.run_async("Heartbeat", self.engine.send_heartbeat)

    def seed_dev(self, kind: str) -> None:
        def work():
            cfg = self._apply_form_to_config()
            seeder = DevSeeder(cfg)
            if kind == "transactions":
                return seeder.seed_transactions(count=1)
            if kind == "bins":
                return seeder.seed_bins(count=1)
            if kind == "status":
                return seeder.seed_status()
            return seeder.seed_all(transactions=3, bins=2, status=True)

        def worker():
            try:
                result = work()
                self.ui_events.put({"kind": "message", "message": "Dodano sztuczne dane: " + result_to_text(result)})
            except Exception as exc:
                self.ui_events.put({"kind": "error", "message": f"Generator danych: {exc}"})

        threading.Thread(target=worker, daemon=True).start()

    def _drain_events(self) -> None:
        while True:
            try:
                row = self.log.events.get_nowait()
                self.append_log(f"{row.get('ts')} [{row.get('level')}] {row.get('message')} {row.get('ctx')}")
            except queue.Empty:
                break
        while True:
            try:
                event = self.ui_events.get_nowait()
            except queue.Empty:
                break
            kind = event.get("kind")
            if kind == "diagnostics":
                for key, value in event.get("data", {}).items():
                    if key in self.metric_vars:
                        self._set_metric(key, value)
            elif kind == "error":
                self.status_var.set("Blad")
                self.append_log(str(event.get("message")))
            elif kind == "message":
                self.append_log(str(event.get("message")))
                if isinstance(event.get("result"), AppConfig):
                    self._load_vars()
            else:
                if event.get("state"):
                    self.status_var.set(str(event["state"]))
                if "pending" in event:
                    self._set_metric("outbox", f"pending/failed: {event['pending']}, offline: {event.get('offline_queue', 0)}")
                if event.get("message"):
                    self.append_log(str(event["message"]))
        self.after(250, self._drain_events)

    def destroy(self) -> None:
        try:
            self.stop_sync()
        finally:
            super().destroy()


def main() -> None:
    app = SyncApp()
    app.mainloop()
