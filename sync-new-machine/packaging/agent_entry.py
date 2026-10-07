"""PyInstaller entry point for revend-sync.exe."""

import sys

from revend_sync.cli import main

if __name__ == "__main__":
    sys.exit(main())
