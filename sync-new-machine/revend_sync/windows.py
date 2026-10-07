"""Windows system operations for install/uninstall. Paths are injectable for tests."""

from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from xml.sax.saxutils import escape

TASK_NAME = "ReVend Sync"
LEGACY_MARKERS = (b"daemon.bat", "daemon.bat".encode("utf-16-le"))
DISABLED_SUFFIX = ".disabled-by-revend-sync"


def program_files() -> Path:
    return Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "ReVend" / "Sync"


def is_admin() -> bool:
    if sys.platform != "win32":
        return os.geteuid() == 0 if hasattr(os, "geteuid") else False
    import ctypes

    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def startup_dirs() -> list[Path]:
    """The all-users Startup folder and every user's own one."""
    program_data = os.environ.get("ProgramData", r"C:\ProgramData")
    dirs = [Path(program_data) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "StartUp"]
    users = Path(os.environ.get("SystemDrive", "C:") + "\\") / "Users"
    pattern = str(
        users / "*" / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    )
    dirs += [Path(p) for p in glob.glob(pattern)]
    return [d for d in dirs if d.is_dir()]


def legacy_startup_entries(dirs: Iterable[Path]) -> list[Path]:
    """Startup shortcuts and scripts that launch the old agent's daemon.bat."""
    entries = []
    for directory in dirs:
        for entry in directory.iterdir():
            if not entry.is_file() or entry.stat().st_size > 1024 * 1024:
                continue
            data = entry.read_bytes().lower()
            if any(marker in data for marker in LEGACY_MARKERS):
                entries.append(entry)
    return entries


def legacy_processes() -> list[tuple[int, str]]:
    """(pid, command line) of the old agent: the daemon.bat loop first, then php sync.php."""
    import psutil

    found = []
    for process in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            cmdline = " ".join(process.info["cmdline"] or []).lower()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if "daemon.bat" in cmdline or (
            "sync.php" in cmdline and "php" in (process.info["name"] or "").lower()
        ):
            found.append((process.info["pid"], cmdline))
    # The cmd.exe loop restarts php within 10 s - stop it before php.
    found.sort(key=lambda item: 0 if "daemon.bat" in item[1] else 1)
    return found


def kill(pids: Iterable[int]) -> None:
    import psutil

    for pid in pids:
        try:
            process = psutil.Process(pid)
            for child in process.children(recursive=True):
                child.kill()
            process.kill()
        except psutil.NoSuchProcess:
            continue


@dataclass
class LegacyBackup:
    """What the installer disabled, so ``uninstall --restore-legacy`` can put it back."""

    directory: Path

    @property
    def manifest(self) -> Path:
        return self.directory / "manifest.json"

    def load(self) -> dict:
        try:
            return json.loads(self.manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"startup": [], "daemon_bat": None}

    def save(self, data: dict) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        self.manifest.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def disable(self, startup_entries: Iterable[Path], daemon_bat: Path | None) -> dict:
        self.directory.mkdir(parents=True, exist_ok=True)
        data = self.load()
        for entry in startup_entries:
            target = self.directory / f"{int(time.time())}-{entry.name}"
            shutil.move(str(entry), str(target))
            data["startup"].append({"original": str(entry), "backup": str(target)})
        if daemon_bat is not None and daemon_bat.exists():
            disabled = daemon_bat.with_name(daemon_bat.name + DISABLED_SUFFIX)
            os.replace(daemon_bat, disabled)
            data["daemon_bat"] = str(daemon_bat)
        self.save(data)
        return data

    def restore(self) -> dict:
        data = self.load()
        for item in data.get("startup", []):
            if Path(item["backup"]).exists() and not Path(item["original"]).exists():
                shutil.move(item["backup"], item["original"])
        daemon_bat = data.get("daemon_bat")
        if daemon_bat:
            disabled = Path(daemon_bat + DISABLED_SUFFIX)
            if disabled.exists() and not Path(daemon_bat).exists():
                os.replace(disabled, daemon_bat)
        self.save({"startup": [], "daemon_bat": None})
        return data


def install_files(package_dir: Path, install_root: Path, version: str) -> Path:
    """Copy the unpacked agent into ``versions/<version>`` and write the launcher."""
    target = install_root / "versions" / version
    if package_dir.resolve() != target.resolve():
        staging = target.with_name(target.name + ".tmp")
        if staging.exists():
            shutil.rmtree(staging)
        shutil.copytree(package_dir, staging)
        if target.exists():
            shutil.rmtree(target)
        os.replace(staging, target)
    write_launcher(install_root)
    set_current(install_root, version)
    return target


LAUNCHER = """@echo off
rem ReVend Sync launcher - started by the "ReVend Sync" scheduled task.
rem Runs the supervisor of the version named in current.txt; after an update
rem the supervisor exits and this loop starts the new version.
setlocal
cd /d "%~dp0"
:loop
set "VERSION="
set /p VERSION=<"%~dp0current.txt"
if exist "%~dp0versions\\%VERSION%\\revend-sync.exe" (
  "%~dp0versions\\%VERSION%\\revend-sync.exe" service --install-root "%~dp0."
) else (
  echo Missing version %VERSION% >> "%~dp0launcher.log"
)
ping 127.0.0.1 -n 6 >nul
goto loop
"""


# Service commands for technicians, always against the current version.
COMMAND = """@echo off
setlocal
set /p VERSION=<"%~dp0current.txt"
"%~dp0versions\\%VERSION%\\revend-sync.exe" %*
"""


def write_launcher(install_root: Path) -> Path:
    install_root.mkdir(parents=True, exist_ok=True)
    path = install_root / "launcher.cmd"
    path.write_text(LAUNCHER.replace("\n", "\r\n"), encoding="ascii")
    (install_root / "revend-sync.cmd").write_text(COMMAND.replace("\n", "\r\n"), encoding="ascii")
    return path


def current_version(install_root: Path) -> str | None:
    try:
        return (install_root / "current.txt").read_text(encoding="ascii").strip() or None
    except OSError:
        return None


def set_current(install_root: Path, version: str) -> None:
    tmp = install_root / "current.txt.tmp"
    tmp.write_text(version, encoding="ascii")
    os.replace(tmp, install_root / "current.txt")


def task_xml(launcher: Path) -> str:
    """Scheduled task: at boot, as SYSTEM, no time limit, restarted if it ever stops."""
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>ReVend machine sync agent</Description>
  </RegistrationInfo>
  <Triggers>
    <BootTrigger>
      <Enabled>true</Enabled>
      <Delay>PT30S</Delay>
    </BootTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>S-1-5-18</UserId>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>5</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>999</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>cmd.exe</Command>
      <Arguments>/c "{escape(str(launcher))}"</Arguments>
      <WorkingDirectory>{escape(str(launcher.parent))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def register_task(launcher: Path, work_dir: Path) -> None:
    xml_path = work_dir / "revend-sync-task.xml"
    xml_path.write_text(task_xml(launcher), encoding="utf-16")
    try:
        _schtasks("/Create", "/TN", TASK_NAME, "/XML", str(xml_path), "/F")
    finally:
        xml_path.unlink()
    _schtasks("/Run", "/TN", TASK_NAME)


def remove_task() -> None:
    _schtasks("/End", "/TN", TASK_NAME, check=False)
    _schtasks("/Delete", "/TN", TASK_NAME, "/F", check=False)


def _schtasks(*args: str, check: bool = True) -> None:
    subprocess.run(["schtasks", *args], check=check, capture_output=True)
