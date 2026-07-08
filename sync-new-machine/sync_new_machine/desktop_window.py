from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

import webview


HOST = "127.0.0.1"
PORT = 8787
URL = f"http://{HOST}:{PORT}"


def _is_panel_running() -> bool:
    try:
        with socket.create_connection((HOST, PORT), timeout=0.5):
            return True
    except OSError:
        return False


def _start_panel() -> subprocess.Popen:
    root = Path(__file__).resolve().parents[1]
    return subprocess.Popen(
        [sys.executable, "-m", "sync_new_machine.web_app"],
        cwd=str(root),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _ensure_panel() -> subprocess.Popen | None:
    if _is_panel_running():
        return None
    process = _start_panel()
    for _ in range(40):
        if _is_panel_running():
            return process
        time.sleep(0.25)
    raise RuntimeError("Panel nie wystartowal na http://127.0.0.1:8787")


def main() -> None:
    process = _ensure_panel()
    window = webview.create_window(
        "Revend Sync New",
        URL,
        width=1720,
        height=1040,
        min_size=(1360, 860),
        confirm_close=True,
    )
    try:
        webview.start(gui="cocoa")
    finally:
        if process is not None:
            process.terminate()


if __name__ == "__main__":
    main()
