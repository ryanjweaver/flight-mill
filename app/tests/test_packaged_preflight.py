"""SOFTWARE ONLY: package diagnostics use injected imports/native probes, never a UI."""

import hashlib
import json
from types import SimpleNamespace

import pytest

from flightmill.desktop import packaged


@pytest.fixture
def package_root(tmp_path):
    root = tmp_path / "flightmill"
    for relative in packaged.REQUIRED_UI_FILES:
        destination = root / "ui" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("asset:" + relative, encoding="utf-8")
    return root


def run_check(root, **overrides):
    arguments = {
        "package_root": root,
        "frozen": False,
        "importer": lambda name: SimpleNamespace(__file__=f"BUNDLE/{name}.pyc"),
        "preflight_check": lambda: None,
        "native_probe": lambda: {"verified_by": "test double"},
    }
    arguments.update(overrides)
    return packaged.self_check(**arguments)


def test_source_check_records_runtime_imports_and_asset_hashes(package_root):
    result = run_check(package_root)
    assert result["ok"]
    assert result["errors"] == []
    assert result["runtime"]["package_root"] == str(package_root)
    assert result["runtime"]["frozen"] is False
    assert set(result["modules"]) == set(packaged.REQUIRED_MODULES)
    assert result["modules"]["serial.serialwin32"] == "BUNDLE/serial.serialwin32.pyc"
    for relative in packaged.REQUIRED_UI_FILES:
        expected = hashlib.sha256((package_root / "ui" / relative).read_bytes()).hexdigest()
        assert result["ui_sha256"][relative] == expected
    assert "build_info" not in result


def test_frozen_check_requires_build_identity(package_root):
    result = run_check(package_root, frozen=True)
    assert not result["ok"]
    assert result["errors"][0]["check"] == "build_info"


def test_frozen_check_accepts_matching_asset_manifest(package_root):
    expected = run_check(package_root)["ui_sha256"]
    build_info = {"build_id": "accepted-source-123", "ui_sha256": expected}
    (package_root / "build_info.json").write_text(json.dumps(build_info), encoding="utf-8")
    result = run_check(package_root, frozen=True)
    assert result["ok"]
    assert result["build_info"]["build_id"] == "accepted-source-123"


def test_asset_hash_mismatch_is_not_success(package_root):
    (package_root / "build_info.json").write_text(
        json.dumps({"ui_sha256": {"static/js/app.js": "a" * 64}}), encoding="utf-8")
    result = run_check(package_root, frozen=True)
    assert not result["ok"]
    assert result["errors"][0]["check"] == "asset_hash:static/js/app.js"


@pytest.mark.parametrize("content", ["not JSON", "[]", '{"ui_sha256": []}'])
def test_invalid_build_metadata_is_reported(package_root, content):
    (package_root / "build_info.json").write_text(content, encoding="utf-8")
    result = run_check(package_root, frozen=True)
    assert not result["ok"]
    assert result["errors"][0]["check"] == "build_info"


def test_failures_are_collected_without_starting_anything(package_root):
    (package_root / "ui/static/js/app.js").unlink()

    def missing_pin():
        raise RuntimeError("Missing dependency: pyserial==3.5")

    def missing_import(name):
        if name == "flightmill.acquisition.windows_serial":
            raise ImportError("Windows serial module missing")
        return SimpleNamespace(__file__=name)

    def missing_native():
        raise OSError("Native bridge unavailable")

    result = run_check(package_root, importer=missing_import,
                       preflight_check=missing_pin, native_probe=missing_native)
    assert not result["ok"]
    assert {failure["check"] for failure in result["errors"]} == {
        "pinned_dependencies", "import:flightmill.acquisition.windows_serial",
        "native_bridge", "asset:static/js/app.js",
    }
    assert "Windows serial module missing" in json.dumps(result)


def test_native_probe_only_loads_bridge_libraries(tmp_path, monkeypatch):
    imports, references, loads = [], [], []
    loader = tmp_path / "win-x64" / "WebView2Loader.dll"
    loader.parent.mkdir()
    loader.write_bytes(b"test fixture")
    clr = SimpleNamespace(__file__="clr.py", AddReference=references.append)
    util = SimpleNamespace(interop_dll_path=lambda name: str(tmp_path / name))

    def fake_import(name):
        imports.append(name)
        return {"clr": clr, "webview.util": util}[name]

    monkeypatch.setattr(packaged.importlib, "import_module", fake_import)
    monkeypatch.setattr(packaged.ctypes, "WinDLL", loads.append, raising=False)
    result = packaged._native_bridge()
    assert imports == ["clr", "webview.util"]
    assert references == ["System.Windows.Forms",
                          str(tmp_path / "Microsoft.Web.WebView2.Core.dll"),
                          str(tmp_path / "Microsoft.Web.WebView2.WinForms.dll")]
    assert loads == [str(loader)]
    assert result["native_loader"] == str(loader)


def test_normal_entry_delegates_to_existing_preflight_and_launch(monkeypatch):
    launches = []
    monkeypatch.setattr(packaged.sys, "argv", ["FlightMill.exe", "--output-dir", "Trials"])
    monkeypatch.setattr(packaged.bootstrap, "main", lambda: launches.append(True))
    monkeypatch.setattr(packaged, "self_check", lambda: pytest.fail("Unexpected package check"))
    packaged.main()
    assert launches == [True]


@pytest.mark.parametrize("ok,exit_code", [(True, 0), (False, 1)])
def test_self_check_entry_writes_report_without_launcher(tmp_path, monkeypatch, ok, exit_code):
    destination = tmp_path / "reports" / "check.json"
    monkeypatch.setattr(packaged.sys, "argv",
                        ["FlightMill.exe", "--self-check", "--report", str(destination)])
    monkeypatch.setattr(packaged, "self_check", lambda: {"ok": ok})
    monkeypatch.setattr(packaged.bootstrap, "main", lambda: pytest.fail("Unexpected launch"))
    with pytest.raises(SystemExit) as error:
        packaged.main()
    assert error.value.code == exit_code
    assert json.loads(destination.read_text(encoding="utf-8")) == {"ok": ok}


def test_report_write_failure_returns_error_without_dialog(tmp_path, monkeypatch):
    destination = tmp_path / "directory"
    destination.mkdir()
    monkeypatch.setattr(packaged.sys, "argv",
                        ["FlightMill.exe", "--self-check", "--report", str(destination)])
    monkeypatch.setattr(packaged, "self_check", lambda: {"ok": True})
    with pytest.raises(SystemExit) as error:
        packaged.main()
    assert error.value.code == 2
