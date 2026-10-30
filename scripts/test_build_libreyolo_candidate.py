"""Regression coverage for third-party wheel inventory boundaries."""

import importlib.util
from pathlib import Path
import zipfile

import pytest

spec = importlib.util.spec_from_file_location(
    "candidate", Path(__file__).with_name("build-libreyolo-candidate.py")
)
candidate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(candidate)


def test_wheel_inventory_ignores_vendored_distribution_metadata(tmp_path):
    wheel = tmp_path / "example.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            "setuptools-84.2.1.dist-info/METADATA",
            "Name: setuptools\nVersion: 84.2.1\n",
        )
        archive.writestr(
            "setuptools/_vendor/wheel-1.0.dist-info/METADATA",
            "Name: wheel\nVersion: 1.0\n",
        )
    assert candidate.wheel_metadata(wheel) == ("setuptools", "84.2.1")


def test_wheel_inventory_rejects_ambiguous_distribution(tmp_path):
    wheel = tmp_path / "example.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("first-1.dist-info/METADATA", "Name: first\nVersion: 1\n")
        archive.writestr("second-1.dist-info/METADATA", "Name: second\nVersion: 1\n")
    with pytest.raises(ValueError, match="invalid metadata"):
        candidate.wheel_metadata(wheel)
