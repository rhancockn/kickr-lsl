import importlib
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "packaging" / "windows"
ENTRIES = {
    "trainer_server.py": "wahoo_lsl.apps.server_app",
    "trainer_dashboard.py": "wahoo_lsl.apps.dashboard_app",
    "wahoo_worker.py": "wahoo_lsl.apps.worker",
}


def test_pyproject_entry_points_and_windows_extra():
    tomllib = pytest.importorskip("tomllib")
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project["scripts"]["wahoo-server"] == "wahoo_lsl.apps.server_app:main"
    assert project["scripts"]["wahoo-dashboard-app"] == "wahoo_lsl.apps.dashboard_app:main"
    assert "pyinstaller>=6" in project["optional-dependencies"]["windows"]


@pytest.mark.parametrize("script, module", ENTRIES.items())
def test_entry_scripts_call_existing_mains(script, module):
    assert f"from {module} import main" in (PKG / script).read_text()
    assert callable(importlib.import_module(module).main)


def test_spec_builds_the_three_executables():
    spec = (PKG / "wahoo.spec").read_text()
    for name in ("TrainerServer", "TrainerDashboard", "wahoo-worker"):
        assert f'"{name}"' in spec
    for script in ENTRIES:
        assert script in spec


def test_installer_sources_exist():
    iss = (PKG / "wahoo.iss").read_text()
    sources = re.findall(r'Source: "([^"*]+)"', iss)
    assert sources, "no single-file sources found"
    for src in sources:
        assert (PKG / src.replace("\\", "/")).resolve().is_file(), src
    assert "TrainerServer.exe" in iss and "TrainerDashboard.exe" in iss and "wahoo-worker.exe" in iss


@pytest.mark.parametrize("name", ["server.ico", "dashboard.ico"])
def test_icons_are_ico_files(name):
    assert (PKG / name).read_bytes()[:4] == b"\x00\x00\x01\x00"
