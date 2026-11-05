# packaging/windows/wahoo_dashboard.py — PyInstaller entry script for WahooDashboard.exe
from wahoo_lsl.apps.dashboard_app import main

raise SystemExit(main())
