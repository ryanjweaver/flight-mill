"""Synthetic-file checks only: no installer, application, UI or USB operation."""
import json
import zipfile

import pytest

from scripts.build_windows_package import (
    ARCHIVE_ROOT,
    build_manifest,
    source_inventory,
    verify_package,
    write_zip,
)


@pytest.fixture
def package(tmp_path):
    root = tmp_path / "Flight Mill test É"
    (root / "FlightMill/_internal").mkdir(parents=True)
    (root / "FlightMill/FlightMill.exe").write_bytes(b"synthetic executable; never run")
    (root / "FlightMill/_internal/python312.dll").write_bytes(b"synthetic DLL")
    (root / "README.txt").write_text("Test instructions", encoding="utf-8")
    return root


def test_manifest_and_zip_preserve_full_payload_at_paths_with_spaces(package, tmp_path):
    manifest = build_manifest(package, {"package_id": "test", "app_version": "test"})
    (package / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    verify_package(package, manifest)
    assert [item["path"] for item in manifest["files"]] == [
        "FlightMill/FlightMill.exe", "FlightMill/_internal/python312.dll", "README.txt",
    ]
    destination = tmp_path / "transfer.zip"
    write_zip(package, destination)
    with zipfile.ZipFile(destination) as archive:
        assert archive.testzip() is None
        assert len(archive.namelist()) == 4
        for path in package.rglob("*"):
            if path.is_file():
                assert archive.read(ARCHIVE_ROOT + "/" + path.relative_to(package).as_posix()) == path.read_bytes()
    with pytest.raises(FileExistsError):
        write_zip(package, destination)


@pytest.mark.parametrize("fault", ["missing", "changed", "extra", "size", "hash", "duplicate"])
def test_integrity_failures_are_rejected(package, fault):
    manifest = build_manifest(package, {})
    path = package / "README.txt"
    if fault == "missing":
        path.unlink()
    elif fault == "changed":
        path.write_text("changed", encoding="utf-8")
    elif fault == "extra":
        (package / "extra.txt").write_text("unlisted", encoding="utf-8")
    elif fault == "size":
        manifest["files"][0]["bytes"] = True
    elif fault == "hash":
        manifest["files"][0]["sha256"] = "0" * 64
    elif fault == "duplicate":
        manifest["files"].append(dict(manifest["files"][0]))
    with pytest.raises(ValueError):
        verify_package(package, manifest)


@pytest.mark.parametrize("path", ["../outside", "/outside", "C:/outside", "a\\b", "a/./b", "a//b"])
def test_manifest_path_escape_rejected(package, path):
    manifest = build_manifest(package, {})
    manifest["files"][0]["path"] = path
    with pytest.raises(ValueError):
        verify_package(package, manifest)


def test_case_colliding_manifest_paths_rejected(package):
    manifest = build_manifest(package, {})
    item = dict(manifest["files"][0])
    item["path"] = item["path"].upper()
    manifest["files"].append(item)
    with pytest.raises(ValueError):
        verify_package(package, manifest)


def test_source_inventory_excludes_recordings_caches_and_ui_tests(tmp_path):
    for name in ("app/src/flightmill/main.py", "simulator/simulated_device.py",
                 "app/src/flightmill/ui/templates/index.html",
                 "app/src/flightmill/ui/static/js/app.js",
                 "app/src/flightmill/__pycache__/main.pyc",
                 "app/src/flightmill/ui/tests/example.test.cjs", "data/trial.csv"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic", encoding="utf-8")
    assert set(source_inventory(tmp_path)) == {
        "app/src/flightmill/main.py", "simulator/simulated_device.py",
        "app/src/flightmill/ui/templates/index.html", "app/src/flightmill/ui/static/js/app.js",
    }
