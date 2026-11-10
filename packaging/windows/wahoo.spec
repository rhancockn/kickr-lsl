# PyInstaller spec: TrainerServer.exe, TrainerDashboard.exe and wahoo-worker.exe in one folder (WahooLSL).
# Build with packaging\windows\build.ps1 on Windows.
from pathlib import Path

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules

HERE = Path(SPECPATH)
HIDDEN = collect_submodules("wahoo_lsl") + collect_submodules("bleak") + collect_submodules("winrt")
BINARIES = collect_dynamic_libs("pylsl")  # liblsl (lsl.dll)

APPS = (  # name, entry script, console, icon
    ("TrainerServer", "trainer_server.py", False, "server.ico"),
    ("TrainerDashboard", "trainer_dashboard.py", False, "dashboard.ico"),
    ("wahoo-worker", "wahoo_worker.py", True, None),
)

exes, parts = [], []
for name, script, console, icon in APPS:
    a = Analysis([str(HERE / script)], binaries=BINARIES, hiddenimports=HIDDEN)
    exes.append(EXE(PYZ(a.pure), a.scripts, [], exclude_binaries=True, name=name, console=console,
                    icon=str(HERE / icon) if icon else None))
    parts += [a.binaries, a.datas]

coll = COLLECT(*exes, *parts, name="WahooLSL")
