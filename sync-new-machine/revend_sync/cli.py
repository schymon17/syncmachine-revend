"""Command line: ``python -m revend_sync <command>`` (or ``revend-sync.exe <command>``)."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import IO

from . import __version__, installer, logs
from . import config as config_module
from .agent import Agent
from .api import ApiError, Client, encode_body
from .machine_db import DbSettings, MachineDb
from .store import Store


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="revend-sync", description="ReVend machine sync agent")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--home", type=Path, help="data directory (default: %%PROGRAMDATA%%\\ReVend\\Sync)")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("run", help="run the agent in the foreground (the service runs this)")

    enroll = commands.add_parser("enroll", help="store credentials from the installer's enrollment file")
    enroll.add_argument("--file", type=Path, required=True)
    enroll.add_argument("--db-host", default="127.0.0.1")
    enroll.add_argument("--db-port", type=int, default=3306)
    enroll.add_argument("--db-name", default="qcs")
    enroll.add_argument("--db-user", default="root")
    enroll.add_argument("--db-password", help="machine database password (asked when omitted)")
    enroll.add_argument(
        "--transactions-from-id", type=int, help="first user_transaction id to send (default: new only)"
    )
    enroll.add_argument("--bins-from-id", type=int, help="first empty_record id to send (default: new only)")

    install = commands.add_parser("install", help="install on this machine (run as administrator)")
    source = install.add_mutually_exclusive_group(required=True)
    source.add_argument("--code", help="installation code from the machine page in the panel")
    source.add_argument("--enrollment-file", type=Path, help="enrollment saved by the panel's install.ps1")
    install.add_argument("--panel", default=installer.DEFAULT_PANEL)
    install.add_argument("--legacy-dir", type=Path, help="old PHP agent folder (found automatically)")
    install.add_argument("--db-host")
    install.add_argument("--db-port", type=int)
    install.add_argument("--db-name")
    install.add_argument("--db-user")
    install.add_argument("--db-password")
    install.add_argument("--install-root", type=Path, default=None)
    install.add_argument("--keep-legacy", action="store_true", help="do not stop the old PHP agent")

    uninstall = commands.add_parser("uninstall", help="stop the agent and remove it from autostart")
    uninstall.add_argument(
        "--restore-legacy", action="store_true", help="give the old PHP agent its autostart back"
    )

    service = commands.add_parser("service", help="supervisor started by launcher.cmd")
    service.add_argument("--install-root", type=Path, required=True)

    commands.add_parser("check", help="check the machine database and the API connection")
    commands.add_parser("status", help="show the queue and recent rejections")
    commands.add_parser("requeue-dead", help="send rejected messages again (after a server-side fix)")

    args = parser.parse_args(argv)
    home = args.home or config_module.home()

    if args.command == "enroll":
        return _enroll(args, home)
    if args.command == "install":
        return _install(args, home)
    if args.command == "uninstall":
        installer.uninstall(home, args.restore_legacy)
        return 0
    if args.command == "service":
        return _service(args.install_root, home)
    if args.command == "run":
        return _run(home)
    if args.command in ("status", "requeue-dead", "check") and not (home / "config.json").exists():
        print(f"Agent is not installed here (no {home / 'config.json'}).", file=sys.stderr)
        return 1
    if args.command == "check":
        return _check(home)
    if args.command == "status":
        return _status(home)
    if args.command == "requeue-dead":
        count = Store(home / "agent.db").requeue_dead()
        print(f"{count} message(s) queued again")
        return 0
    return 2


def _enroll(args: argparse.Namespace, home: Path) -> int:
    enrollment = json.loads(args.file.read_text(encoding="utf-8-sig"))
    # The installer stores the response's "data"; accept the whole response too.
    enrollment = enrollment.get("data", enrollment)
    password = (
        args.db_password if args.db_password is not None else getpass.getpass("Machine database password: ")
    )
    db = DbSettings(
        host=args.db_host, port=args.db_port, database=args.db_name, user=args.db_user, password=password
    )

    problems = MachineDb(db).check_schema()
    if problems:
        print("Machine database check failed:\n  " + "\n  ".join(problems), file=sys.stderr)
        return 1

    config = config_module.Config(
        machine_id=enrollment["machineId"],
        api_base_url=enrollment["apiBaseUrl"],
        agent_base_url=enrollment["agentBaseUrl"],
        key_id=enrollment["keyId"],
        secret=enrollment["secret"],
        db=db,
        transactions_from_id=args.transactions_from_id,
        bins_from_id=args.bins_from_id,
    )
    config_module.save(config, home)
    print(f"Enrolled {config.machine_id} (key {config.key_id})")
    return 0


def _install(args: argparse.Namespace, home: Path) -> int:
    options = installer.InstallOptions(
        code=args.code,
        enrollment_file=args.enrollment_file,
        panel_url=args.panel,
        legacy_dir=args.legacy_dir,
        db_host=args.db_host,
        db_port=args.db_port,
        db_name=args.db_name,
        db_user=args.db_user,
        db_password=args.db_password,
        data_dir=home,
        stop_legacy=not args.keep_legacy,
    )
    if args.install_root:
        options.install_root = args.install_root
    try:
        installer.install(options)
    except installer.InstallError as error:
        print(f"BLAD: {error}", file=sys.stderr)
        return 1
    return 0


def _service(install_root: Path, home: Path) -> int:
    from .supervisor import Supervisor

    home.mkdir(parents=True, exist_ok=True)
    lock = _single_instance(home / "supervisor.lock")
    if lock is None:
        return 3
    store = Store(home / "agent.db")
    logs.setup(home / "logs", store, filename="supervisor.log")
    supervisor = Supervisor(install_root, home)

    def stop(*_: object) -> None:
        supervisor.stop()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, stop)  # type: ignore[attr-defined]
    code = supervisor.run()
    store.close()
    return code


def _run(home: Path) -> int:
    home.mkdir(parents=True, exist_ok=True)
    lock = _single_instance(home / "agent.lock")
    if lock is None:
        print("Another agent is already running for this data directory.", file=sys.stderr)
        return 3

    store = Store(home / "agent.db")
    logs.setup(home / "logs", store, debug=bool(os.environ.get("REVEND_SYNC_DEBUG")))
    agent = Agent(config_module.load(home), store, data_dir=home)

    def stop(*_: object) -> None:
        agent.stop()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, stop)  # type: ignore[attr-defined]

    agent.run()
    store.close()
    return 0


def _check(home: Path) -> int:
    config = config_module.load(home)
    ok = True

    db = MachineDb(config.db)
    try:
        problems = db.check_schema()
        print("Machine database: " + ("OK" if not problems else "; ".join(problems)))
        ok = ok and not problems
    except Exception as error:  # noqa: BLE001
        print(f"Machine database: {error}")
        ok = False

    client = Client(config.key_id, config.secret)
    try:
        response = client.post(config.url("register"), encode_body({"machineId": config.machine_id}))
        attributes = response.data.get("data", {}).get("attributes", {})
        print(
            f"API: OK, registered={attributes.get('registered')}, integration={attributes.get('integration')}, "
            f"clock offset {client.clock_offset:+.0f} s"
        )
    except ApiError as error:
        print(f"API: {error}")
        ok = False
    return 0 if ok else 1


def _status(home: Path) -> int:
    store = Store(home / "agent.db")
    stats = store.stats()
    oldest = stats["oldest_pending_at"]
    print(f"Pending: {stats['pending']}, retrying: {stats['failed']}, rejected: {stats['dead']}")
    if oldest:
        print(f"Oldest unsent message: {int(time.time() - oldest)} s ago")
    last = store.get("last_sent_at")
    print("Last delivery: " + (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last)) if last else "never"))
    for dead in store.dead_letters(10):
        print(f"  rejected #{dead['id']} {dead['kind']}: {str(dead['last_error'])[:200]}")
    return 0


def _single_instance(path: Path) -> IO[str] | None:
    """An exclusive lock held for the process lifetime; released by the OS if it dies."""
    handle = open(path, "a+")
    try:
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle
