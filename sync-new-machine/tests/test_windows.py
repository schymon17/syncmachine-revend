from __future__ import annotations

import xml.etree.ElementTree as ET

from revend_sync import windows


def test_startup_entries_of_the_old_agent_are_found(tmp_path):
    startup = tmp_path / "StartUp"
    startup.mkdir()
    # A shortcut stores its target path in UTF-16.
    (startup / "sync.lnk").write_bytes(b"L\x00\x00\x00" + "C:\\sync\\daemon.bat".encode("utf-16-le"))
    (startup / "start-sync.bat").write_text('start "" "C:\\sync\\DAEMON.BAT"\r\n')
    (startup / "anydesk.lnk").write_bytes(b"L\x00\x00\x00" + "C:\\AnyDesk.exe".encode("utf-16-le"))

    found = sorted(p.name for p in windows.legacy_startup_entries([startup]))
    assert found == ["start-sync.bat", "sync.lnk"]


def test_disabling_the_old_agent_can_be_undone(tmp_path):
    startup = tmp_path / "StartUp"
    startup.mkdir()
    shortcut = startup / "sync.lnk"
    shortcut.write_bytes(b"daemon.bat")
    legacy_root = tmp_path / "sync"
    legacy_root.mkdir()
    daemon = legacy_root / "daemon.bat"
    daemon.write_text("@echo off")

    backup = windows.LegacyBackup(tmp_path / "backup")
    backup.disable([shortcut], daemon)
    assert not shortcut.exists() and not daemon.exists()
    assert (legacy_root / ("daemon.bat" + windows.DISABLED_SUFFIX)).exists()

    backup.restore()
    assert shortcut.read_bytes() == b"daemon.bat"
    assert daemon.read_text() == "@echo off"
    assert backup.load() == {"startup": [], "daemon_bat": None}


def test_the_scheduled_task_runs_the_launcher_as_system_at_boot(tmp_path):
    xml = windows.task_xml(tmp_path / "Program Files" / "ReVend" / "Sync" / "launcher.cmd")
    root = ET.fromstring(xml.encode("utf-16"))
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}

    assert root.find("t:Principals/t:Principal/t:UserId", ns).text == "S-1-5-18"
    assert root.find("t:Triggers/t:BootTrigger", ns) is not None
    assert root.find("t:Settings/t:ExecutionTimeLimit", ns).text == "PT0S"
    assert root.find("t:Settings/t:RestartOnFailure/t:Count", ns).text == "999"
    assert root.find("t:Actions/t:Exec/t:Command", ns).text == "cmd.exe"
    assert "launcher.cmd" in root.find("t:Actions/t:Exec/t:Arguments", ns).text


def test_install_files_puts_the_version_next_to_the_launcher(tmp_path):
    package = tmp_path / "unzipped" / "revend-sync"
    package.mkdir(parents=True)
    (package / "revend-sync.exe").write_bytes(b"exe")
    root = tmp_path / "Sync"

    target = windows.install_files(package, root, "3.0.0")

    assert (target / "revend-sync.exe").read_bytes() == b"exe"
    assert windows.current_version(root) == "3.0.0"
    launcher = (root / "launcher.cmd").read_bytes()
    assert b"\r\n" in launcher and b"versions\\%VERSION%\\revend-sync.exe" in launcher
    assert b"service --install-root" in launcher
    assert (root / "revend-sync.cmd").exists()
