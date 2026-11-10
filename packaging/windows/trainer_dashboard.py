# packaging/windows/trainer_dashboard.py — PyInstaller entry script for TrainerDashboard.exe
from wahoo_lsl.apps.dashboard_app import main

raise SystemExit(main())
