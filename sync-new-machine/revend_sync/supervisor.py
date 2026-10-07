"""The service: keeps the agent running and installs its updates.

Started by ``launcher.cmd`` (the "ReVend Sync" scheduled task) as
``revend-sync.exe service``. It runs the agent as a child process and
restarts it after a crash. When the agent has downloaded and verified an
update (``updates/ready.json``), the supervisor unpacks it into its own
``versions/<version>`` directory, points ``current.txt`` at it and exits; the
launcher then starts the new version.

A new version is on trial for ``TRIAL_SECONDS``: if its agent crashes
``TRIAL_CRASHES`` times in that window, ``current.txt`` goes back to the
previous version and the failed one is never installed again.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import Callable, Sequence

from . import __version__
from .windows import current_version, set_current

log = logging.getLogger(__name__)

TRIAL_SECONDS = 300
TRIAL_CRASHES = 3
STOP_TIMEOUT = 20
EXIT_SWITCH = 0  # the launcher starts whatever current.txt names


def agent_command(data_dir: Path) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--home", str(data_dir), "run"]
    return [sys.executable, "-m", "revend_sync", "--home", str(data_dir), "run"]


class Supervisor:
    def __init__(
        self,
        install_root: Path,
        data_dir: Path,
        version: str = __version__,
        command: Sequence[str] | None = None,
        now: Callable[[], float] = time.monotonic,
        poll_seconds: float = 2.0,
        restart_delay: Callable[[int], float] | None = None,
    ):
        self.install_root = install_root
        self.data_dir = data_dir
        self.version = version
        self.command = list(command or agent_command(data_dir))
        self.now = now
        self.poll_seconds = poll_seconds
        # 5, 10, 20, 40, 60 s between restarts of a crashing agent.
        self.restart_delay = restart_delay or (lambda crashes: min(60, 5 * 2 ** (crashes - 1)))
        self.child: subprocess.Popen | None = None
        self.started_at = 0.0
        self.crashes: list[float] = []
        self._stopping = False

    # --- state ------------------------------------------------------------

    @property
    def state_file(self) -> Path:
        return self.data_dir / "update-state.json"

    def state(self) -> dict:
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def save_state(self, state: dict) -> None:
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, self.state_file)

    @property
    def on_trial(self) -> bool:
        return self.state().get("trial") == self.version

    # --- main loop --------------------------------------------------------

    def stop(self) -> None:
        self._stopping = True

    def run(self) -> int:
        log.info("Supervisor %s started", self.version)
        while not self._stopping:
            if self.child is None:
                self._start_child()

            assert self.child is not None
            if self.child.poll() is not None:
                if self._child_exited():
                    return EXIT_SWITCH
                continue

            if self.on_trial and self.now() - self.started_at >= TRIAL_SECONDS:
                self._commit_trial()

            if not self.on_trial and self.apply_update():
                return EXIT_SWITCH

            time.sleep(self.poll_seconds)

        self._stop_child()
        return 0

    def _start_child(self) -> None:
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
            subprocess, "CREATE_NO_WINDOW", 0
        )
        self.child = subprocess.Popen(self.command, creationflags=flags)
        self.started_at = self.now()

    def _child_exited(self) -> bool:
        """Handle an agent exit. Returns True when the supervisor should exit (rollback)."""
        assert self.child is not None
        code = self.child.returncode
        self.child = None
        now = self.now()
        self.crashes = [t for t in self.crashes if now - t < TRIAL_SECONDS] + [now]
        log.error("Agent exited with code %s, restarting", code, extra={"context": {"exit_code": code}})

        if self.on_trial and len(self.crashes) >= TRIAL_CRASHES:
            self._rollback()
            return True

        time.sleep(self.restart_delay(len(self.crashes)))
        return False

    def _stop_child(self) -> None:
        if self.child is None or self.child.poll() is not None:
            self.child = None
            return
        try:
            if sys.platform == "win32":
                self.child.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
            else:
                self.child.terminate()
            self.child.wait(STOP_TIMEOUT)
        except (subprocess.TimeoutExpired, OSError):
            self.child.kill()
            self.child.wait()
        self.child = None

    # --- updates ----------------------------------------------------------

    def apply_update(self) -> bool:
        ready_file = self.data_dir / "updates" / "ready.json"
        if not ready_file.exists():
            return False
        try:
            ready = json.loads(ready_file.read_text(encoding="utf-8"))
            version = str(ready["version"])
            archive = ready_file.parent / ready["file"]
            if version == self.version or version in self.state().get("failed", []):
                ready_file.unlink()
                return False
            if _sha256(archive) != ready["sha256"]:
                raise ValueError("SHA-256 mismatch")
            target = self._unpack(archive, version)
            self._smoke_test(target, version)
        except Exception as error:  # noqa: BLE001 - a bad update must not stop the service
            log.error("Update could not be installed", extra={"context": {"error": str(error)[:500]}})
            ready_file.unlink(missing_ok=True)
            return False

        previous = current_version(self.install_root) or self.version
        self.save_state({**self.state(), "trial": version, "previous": previous, "started": time.time()})
        set_current(self.install_root, version)
        ready_file.unlink(missing_ok=True)
        archive.unlink(missing_ok=True)
        self._cleanup_versions(keep={version, previous})
        log.info("Update %s installed in %s; switching from %s", version, target, previous)
        self._stop_child()
        return True

    def _unpack(self, archive: Path, version: str) -> Path:
        """The release zip holds the agent in ``revend-sync/`` next to the installer scripts."""
        versions = self.install_root / "versions"
        staging = versions / f"{version}.tmp"
        if staging.exists():
            shutil.rmtree(staging)
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.namelist():
                if member.startswith("/") or ".." in Path(member).parts:
                    raise ValueError(f"unsafe path in update: {member}")
            bundle.extractall(staging)
        agent_dir = staging / "revend-sync"
        if not (agent_dir / "revend-sync.exe").exists() and not (agent_dir / "revend-sync").exists():
            shutil.rmtree(staging)
            raise ValueError("update has no revend-sync executable")
        unix_executable = agent_dir / "revend-sync"
        if unix_executable.exists():
            unix_executable.chmod(0o755)  # zipfile drops the executable bit outside Windows
        target = versions / version
        if target.exists():
            shutil.rmtree(target)
        os.replace(agent_dir, target)
        shutil.rmtree(staging, ignore_errors=True)
        return target

    def _smoke_test(self, target: Path, version: str) -> None:
        """The new executable must start and report its version before the launcher is pointed at it."""
        executable = target / ("revend-sync.exe" if sys.platform == "win32" else "revend-sync")
        try:
            result = subprocess.run(
                [str(executable), "--version"], capture_output=True, text=True, timeout=60
            )
            reported = (result.stdout or result.stderr).strip()
        except (OSError, subprocess.TimeoutExpired) as error:
            shutil.rmtree(target, ignore_errors=True)
            raise ValueError(f"new version does not start: {error}") from error
        if reported != version:
            shutil.rmtree(target, ignore_errors=True)
            raise ValueError(f"new version reports {reported!r}, expected {version}")

    def _commit_trial(self) -> None:
        state = self.state()
        state.pop("trial", None)
        state.pop("previous", None)
        self.save_state(state)
        log.info("Version %s confirmed after %s s without problems", self.version, TRIAL_SECONDS)

    def _rollback(self) -> None:
        state = self.state()
        previous = state.get("previous")
        failed = sorted(set(state.get("failed", [])) | {self.version})
        self.save_state({"failed": failed})
        if previous:
            set_current(self.install_root, previous)
        log.critical(
            "Version %s keeps crashing; rolled back to %s",
            self.version,
            previous,
            extra={"context": {"failed": self.version, "previous": previous}},
        )

    def _cleanup_versions(self, keep: set[str]) -> None:
        versions = self.install_root / "versions"
        for path in versions.iterdir() if versions.exists() else []:
            if path.is_dir() and path.name not in keep and path.name != self.version:
                shutil.rmtree(path, ignore_errors=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
