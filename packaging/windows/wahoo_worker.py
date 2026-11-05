# packaging/windows/wahoo_worker.py — PyInstaller entry script for wahoo-worker.exe
from wahoo_lsl.apps.worker import main

raise SystemExit(main())
