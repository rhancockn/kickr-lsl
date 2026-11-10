# packaging/windows/trainer_server.py — PyInstaller entry script for TrainerServer.exe
from wahoo_lsl.apps.server_app import main

raise SystemExit(main())
