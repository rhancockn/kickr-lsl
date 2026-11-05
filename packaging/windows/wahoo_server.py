# packaging/windows/wahoo_server.py — PyInstaller entry script for WahooServer.exe
from wahoo_lsl.apps.server_app import main

raise SystemExit(main())
