"""Packaged entry point with a noninteractive dependency and asset check.

The check loads modules and native libraries only. It does not start the local
server, create a window, enumerate/open serial ports, or reserve a trial.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import importlib
import json
import platform
import struct
import sys
from datetime import datetime, timezone
from pathlib import Path

from flightmill.desktop import bootstrap

REQUIRED_MODULES = (
    "pydantic", "fastapi", "jinja2", "serial", "serial.serialwin32",
    "serial.tools.list_ports", "webview", "uvicorn", "websockets",
    "uvicorn.logging", "uvicorn.loops.auto", "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto", "uvicorn.lifespan.on",
    "flightmill.acquisition.windows_serial", "flightmill.desktop.launcher",
    "simulator.simulated_device",
)
REQUIRED_UI_FILES = (
    "templates/index.html", "static/css/app.css", "static/js/app.js",
    "static/js/chart-data.js", "static/js/trial-display.js",
    "static/assets/bee.svg", "static/assets/bee-idle.svg",
)


def _native_bridge() -> dict:
    """Load the same CLR bridge assemblies used by the desktop, without a window."""
    clr = importlib.import_module("clr")
    interop = importlib.import_module("webview.util").interop_dll_path
    clr.AddReference("System.Windows.Forms")
    assemblies = {}
    for name in ("Microsoft.Web.WebView2.Core.dll", "Microsoft.Web.WebView2.WinForms.dll"):
        path = Path(interop(name))
        clr.AddReference(str(path))
        assemblies[name] = str(path)
    loader = Path(interop("win-x64")) / "WebView2Loader.dll"
    if not loader.is_file():
        raise FileNotFoundError(f"Missing WebView2 native loader: {loader}")
    ctypes.WinDLL(str(loader))
    return {"clr_module": str(clr.__file__), "assemblies": assemblies,
            "native_loader": str(loader), "note": "No renderer or window was created."}


def self_check(*, package_root: Path | None = None, frozen: bool | None = None,
               importer=None, preflight_check=None, native_probe=None) -> dict:
    """Return a JSON-ready report; failures never show a dialog or launch the app."""
    package_root = package_root or Path(__file__).resolve().parents[1]
    frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    importer = importer or importlib.import_module
    preflight_check = preflight_check or bootstrap.preflight
    native_probe = native_probe or _native_bridge
    report = {
        "schema_version": 1,
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "ok": False,
        "scope": "Noninteractive imports and file checks; no UI, server, or USB activity.",
        "runtime": {"executable": sys.executable, "python": platform.python_version(),
                    "pointer_bits": struct.calcsize("P") * 8, "platform": platform.platform(),
                    "frozen": frozen, "package_root": str(package_root)},
        "modules": {}, "ui_sha256": {}, "errors": [],
    }

    def check(label, action):
        try:
            return action()
        except Exception as exc:
            report["errors"].append({"check": label, "error": f"{type(exc).__name__}: {exc}"})
            return None

    check("pinned_dependencies", preflight_check)
    for name in REQUIRED_MODULES:
        module = check(f"import:{name}", lambda name=name: importer(name))
        if module is not None:
            report["modules"][name] = str(getattr(module, "__file__", "built-in"))
    report["native_bridge"] = check("native_bridge", native_probe)
    for relative in REQUIRED_UI_FILES:
        def hash_asset(relative=relative):
            return hashlib.sha256((package_root / "ui" / relative).read_bytes()).hexdigest()
        digest = check(f"asset:{relative}", hash_asset)
        if digest is not None:
            report["ui_sha256"][relative] = digest

    metadata_path = package_root / "build_info.json"
    if frozen or metadata_path.is_file():
        metadata = check("build_info", lambda: json.loads(metadata_path.read_text(encoding="utf-8")))
        if metadata is not None:
            report["build_info"] = metadata
            if not isinstance(metadata, dict):
                report["errors"].append({"check": "build_info", "error": "Expected an object."})
            else:
                expected_assets = metadata.get("ui_sha256", {})
                if not isinstance(expected_assets, dict):
                    report["errors"].append({"check": "build_info", "error":
                                             "ui_sha256 must be an object."})
                else:
                    for relative, expected in expected_assets.items():
                        if report["ui_sha256"].get(relative) != expected:
                            report["errors"].append({"check": f"asset_hash:{relative}", "error":
                                                     "UI asset differs from the build manifest."})
    report["ok"] = not report["errors"]
    return report


def main() -> None:
    if "--self-check" not in sys.argv[1:]:
        bootstrap.main()
        return
    parser = argparse.ArgumentParser(description="Check a Flight Mill package without launching it")
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    report = self_check()
    try:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        if sys.stderr is not None:
            print(f"Could not write package check report: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    raise SystemExit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()
