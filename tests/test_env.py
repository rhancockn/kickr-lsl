"""Guards the captured environment: required packages importable at the minimum versions."""
from importlib.metadata import version

import bleak  # noqa: F401
import pylsl

import wahoo_lsl


def _major_minor(v: str) -> tuple[int, int]:
    major, minor = v.split(".")[:2]
    return int(major), int(minor)


def test_bleak_version():
    assert _major_minor(version("bleak")) >= (1, 0)


def test_pylsl_version_and_liblsl_loaded():
    assert _major_minor(version("pylsl")) >= (1, 17)
    assert pylsl.library_version() > 0  # liblsl shared library actually loads


def test_package_installed():
    assert wahoo_lsl.__version__ == "0.1.0"
