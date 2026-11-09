from __future__ import annotations

import hashlib
import json
import stat
import sys
import zipfile

import pytest

from revend_sync import updates
from revend_sync.supervisor import EXIT_SWITCH, Supervisor
from revend_sync.windows import current_version, set_current

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="fake executables are shell scripts")


def make_release(tmp_path, version, reports=None):
    """A release zip like build-agent.ps1 makes, with a fake executable."""
    source = tmp_path / f"src-{version}"
    agent = source / "revend-sync"
    agent.mkdir(parents=True)
    exe = agent / "revend-sync"
    exe.write_text(f"#!/bin/sh\necho {reports or version}\n")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    (source / "install.cmd").write_text("@echo off")
    archive = tmp_path / f"revend-sync-{version}.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        for path in source.rglob("*"):
            if path.is_dir():
                continue
            info = zipfile.ZipInfo(str(path.relative_to(source)))
            info.external_attr = (path.stat().st_mode & 0xFFFF) << 16
            bundle.writestr(info, path.read_bytes())
    return archive


def ready(data_dir, archive, version, sha=None):
    updates_dir = data_dir / "updates"
    updates_dir.mkdir(parents=True, exist_ok=True)
    target = updates_dir / archive.name
    target.write_bytes(archive.read_bytes())
    sha = sha or hashlib.sha256(target.read_bytes()).hexdigest()
    (updates_dir / "ready.json").write_text(
        json.dumps({"version": version, "file": target.name, "sha256": sha})
    )


@pytest.fixture
def dirs(tmp_path):
    install_root = tmp_path / "Sync"
    (install_root / "versions" / "3.0.0").mkdir(parents=True)
    set_current(install_root, "3.0.0")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return install_root, data_dir


def test_a_verified_update_is_unpacked_and_put_on_trial(tmp_path, dirs):
    install_root, data_dir = dirs
    ready(data_dir, make_release(tmp_path, "3.1.0"), "3.1.0")
    supervisor = Supervisor(install_root, data_dir, version="3.0.0", command=["sleep", "30"])

    assert supervisor.apply_update() is True

    assert current_version(install_root) == "3.1.0"
    assert (install_root / "versions" / "3.1.0" / "revend-sync").exists()
    assert supervisor.state() == {**supervisor.state(), "trial": "3.1.0", "previous": "3.0.0"}
    assert not (data_dir / "updates" / "ready.json").exists()


def test_an_update_with_a_wrong_checksum_is_refused(tmp_path, dirs):
    install_root, data_dir = dirs
    ready(data_dir, make_release(tmp_path, "3.1.0"), "3.1.0", sha="0" * 64)

    assert Supervisor(install_root, data_dir, version="3.0.0").apply_update() is False
    assert current_version(install_root) == "3.0.0"
    assert not (data_dir / "updates" / "ready.json").exists()


def test_an_update_whose_executable_does_not_report_its_version_is_refused(tmp_path, dirs):
    install_root, data_dir = dirs
    ready(data_dir, make_release(tmp_path, "3.1.0", reports="garbage"), "3.1.0")

    assert Supervisor(install_root, data_dir, version="3.0.0").apply_update() is False
    assert current_version(install_root) == "3.0.0"
    assert not (install_root / "versions" / "3.1.0").exists()


def test_a_crashing_new_version_rolls_back_and_is_never_retried(tmp_path, dirs):
    install_root, data_dir = dirs
    set_current(install_root, "3.1.0")
    (data_dir / "update-state.json").write_text(json.dumps({"trial": "3.1.0", "previous": "3.0.0"}))
    supervisor = Supervisor(
        install_root,
        data_dir,
        version="3.1.0",
        command=[sys.executable, "-c", "raise SystemExit(1)"],
        poll_seconds=0.01,
        restart_delay=lambda n: 0,
    )

    assert supervisor.run() == EXIT_SWITCH

    assert current_version(install_root) == "3.0.0"
    assert supervisor.state() == {"failed": ["3.1.0"]}
    assert updates._failed_versions(data_dir) == ["3.1.0"]


def test_a_new_version_that_runs_long_enough_is_confirmed(tmp_path, dirs):
    install_root, data_dir = dirs
    (data_dir / "update-state.json").write_text(json.dumps({"trial": "3.1.0", "previous": "3.0.0"}))
    clock = [0.0]
    supervisor = Supervisor(
        install_root,
        data_dir,
        version="3.1.0",
        command=["sleep", "30"],
        now=lambda: clock[0],
        poll_seconds=0.01,
    )

    supervisor._start_child()
    clock[0] = 301
    assert supervisor.on_trial
    supervisor._commit_trial()
    supervisor._stop_child()

    assert supervisor.state() == {}


def test_a_crashing_agent_outside_a_trial_is_just_restarted(tmp_path, dirs):
    install_root, data_dir = dirs
    starts = tmp_path / "starts"
    command = [sys.executable, "-c", f"open({str(starts)!r}, 'a').write('x'); raise SystemExit(1)"]
    supervisor = Supervisor(
        install_root, data_dir, version="3.0.0", command=command, poll_seconds=0.01, restart_delay=lambda n: 0
    )

    for _ in range(5):
        supervisor._start_child()
        supervisor.child.wait()
        assert supervisor._child_exited() is False

    assert starts.read_text() == "xxxxx"
    assert current_version(install_root) == "3.0.0"
