"""Install / uninstall on a machine. Runs from the unpacked release zip.

Windows 10/11: the panel's one-line PowerShell command downloads the zip and
runs its ``install.ps1``. Windows 7 (no TLS 1.2 in PowerShell 2.0): copy the
zip to the machine and run ``install.cmd RV-XXXX-XXXX-XXXX``. Both end here,
in ``revend-sync.exe install``, which has its own TLS stack.
"""

from __future__ import annotations

import getpass
import json
import os
import socket
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

import requests

from . import __version__, legacy, windows
from .config import Config, home, save
from .machine_db import DbSettings, MachineDb

DEFAULT_PANEL = "https://panel.revend.pl"


class InstallError(Exception):
    pass


@dataclass
class InstallOptions:
    code: str | None = None
    enrollment_file: Path | None = None
    panel_url: str = DEFAULT_PANEL
    legacy_dir: Path | None = None
    db_host: str | None = None
    db_port: int | None = None
    db_name: str | None = None
    db_user: str | None = None
    db_password: str | None = None
    install_root: Path = field(default_factory=windows.program_files)
    data_dir: Path = field(default_factory=home)
    register_task: bool = True
    stop_legacy: bool = True


def redeem(panel_url: str, code: str, session: Any = requests) -> dict[str, Any]:
    response = session.post(
        panel_url.rstrip("/") + "/api/revend/agent/enroll",
        json={"code": code.strip(), "hostname": socket.gethostname()[:100], "softwareVersion": __version__},
        headers={"Accept": "application/json"},
        timeout=(10, 30),
        allow_redirects=False,
    )
    if response.status_code != 200:
        raise InstallError(
            "Kod instalacyjny jest błędny, wygasł albo został już użyty - wygeneruj nowy w panelu "
            f"(HTTP {response.status_code})."
        )
    return response.json()["data"]


def legacy_candidates(explicit: Path | None) -> list[Path]:
    candidates = [explicit] if explicit else []
    if sys.platform == "win32":
        for _, cmdline in windows.legacy_processes():
            for token in cmdline.replace('"', " ").split():
                if token.endswith(("sync.php", "daemon.bat")):
                    candidates.append(Path(token).parent)
        drive = Path(os.environ.get("SystemDrive", "C:") + "\\")
        for pattern in ("*", "Users/*/Desktop/*", "Users/*/Documents/*"):
            candidates += [p for p in drive.glob(pattern) if p.is_dir()]
    return candidates


def install(
    options: InstallOptions,
    say: Callable[[str], None] = print,
    ask_password: Callable[[str], str] = getpass.getpass,
) -> Config:
    if sys.platform == "win32" and not windows.is_admin():
        raise InstallError("Uruchom instalację jako administrator.")

    # 1. Credentials for this machine.
    if options.enrollment_file:
        enrollment = json.loads(options.enrollment_file.read_text(encoding="utf-8-sig"))
        enrollment = enrollment.get("data", enrollment)
    elif options.code:
        enrollment = redeem(options.panel_url, options.code)
    else:
        raise InstallError("Podaj kod instalacyjny z panelu.")
    say(f"Maszyna {enrollment['machineId']} ({enrollment.get('integration')})")

    # 2. The old PHP agent, if present: its database settings and cursor.
    old = legacy.find(legacy_candidates(options.legacy_dir))
    if old is not None:
        say(f"Znaleziono stary agent PHP: {old.root}")

    db = _db_settings(options, old)
    if not db.password and old is None and options.db_password is None:
        db = replace(db, password=ask_password("Hasło do bazy maszyny (MySQL): "))
    machine_db = MachineDb(db)
    try:
        problems = machine_db.check_schema()
    except Exception as error:  # noqa: BLE001
        raise InstallError(f"Brak połączenia z bazą maszyny: {error}") from error
    if problems:
        raise InstallError("Baza maszyny nie pasuje: " + "; ".join(problems))

    # 3. Stop the old agent before taking its cursor, so nothing slips between.
    #    Should anything fail from here on, the old agent gets its autostart
    #    back - the machine must never be left without any agent.
    if options.stop_legacy:
        _stop_legacy(old, options.data_dir, machine_db, say)
    try:
        return _finish(options, enrollment, db, old, machine_db, say)
    except Exception:
        if options.stop_legacy and sys.platform == "win32":
            windows.LegacyBackup(options.data_dir / "legacy-backup").restore()
            say("Instalacja przerwana - przywrócono autostart starego agenta. Zrestartuj komputer.")
        raise


def _finish(
    options: InstallOptions,
    enrollment: dict[str, Any],
    db: DbSettings,
    old: legacy.LegacyAgent | None,
    machine_db: MachineDb,
    say: Callable[[str], None],
) -> Config:
    transactions_from, bins_from = legacy.cursors(old, machine_db)
    say(
        f"Transakcje od id {transactions_from}, worki od id {bins_from} (nadwyżkę serwer odrzuci jako duplikaty)"
    )

    config = Config(
        machine_id=enrollment["machineId"],
        api_base_url=enrollment["apiBaseUrl"],
        agent_base_url=enrollment["agentBaseUrl"],
        key_id=enrollment["keyId"],
        secret=enrollment["secret"],
        db=db,
        transactions_from_id=transactions_from,
        bins_from_id=bins_from,
    )
    save(config, options.data_dir)
    machine_db.close()

    # 4. Files and autostart.
    if getattr(sys, "frozen", False):
        target = windows.install_files(Path(sys.executable).parent, options.install_root, __version__)
        say(f"Zainstalowano {__version__} w {target}")
    if options.register_task and sys.platform == "win32":
        windows.register_task(options.install_root / "launcher.cmd", options.data_dir)
        say(f'Zadanie "{windows.TASK_NAME}" uruchomione - agent działa i startuje razem z Windows.')

    return config


def uninstall(data_dir: Path, restore_legacy: bool, say: Callable[[str], None] = print) -> None:
    if sys.platform == "win32":
        windows.remove_task()
        windows.kill(_agent_pids())
    if restore_legacy:
        restored = windows.LegacyBackup(data_dir / "legacy-backup").restore()
        say(
            f"Przywrócono autostart starego agenta ({len(restored.get('startup', []))} wpis(y)). "
            "Uruchom go ponownie albo zrestartuj komputer."
        )
    say("Agent zatrzymany i usunięty z autostartu. Dane zostały w " + str(data_dir))


def _db_settings(options: InstallOptions, old: legacy.LegacyAgent | None) -> DbSettings:
    base = (old.db_settings() if old else None) or DbSettings()
    return DbSettings(
        host=options.db_host or base.host,
        port=options.db_port or base.port,
        database=options.db_name or base.database,
        user=options.db_user or base.user,
        password=options.db_password if options.db_password is not None else base.password,
    )


def _stop_legacy(
    old: legacy.LegacyAgent | None, data_dir: Path, machine_db: MachineDb, say: Callable[[str], None]
) -> None:
    if sys.platform == "win32":
        processes = windows.legacy_processes()
        windows.kill(pid for pid, _ in processes)
        entries = windows.legacy_startup_entries(windows.startup_dirs())
        windows.LegacyBackup(data_dir / "legacy-backup").disable(entries, old.daemon_bat if old else None)
        if processes or entries:
            say(
                f"Zatrzymano stary agent ({len(processes)} proces(y)), autostart przeniesiony do kopii "
                f"({len(entries)} wpis(y))."
            )
    dropped = legacy.drop_triggers(machine_db)
    if dropped:
        say("Usunięto triggery starego agenta: " + ", ".join(dropped))


def _agent_pids() -> list[int]:
    import psutil

    own = os.getpid()
    return [
        p.pid
        for p in psutil.process_iter(["name"])
        if (p.info["name"] or "").lower() == "revend-sync.exe" and p.pid != own
    ]
