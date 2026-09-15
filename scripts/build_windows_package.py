"""Build the approved working tree as a self-contained Windows 11 x64 package.

This builds and inspects files only. It never installs the package, opens the
application, starts a server, or connects to a USB device.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as metadata
import json
import os
import platform
import re
import struct
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from shutil import copy2, copytree

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE_ROOT = "FlightMill-Windows11-x64"
RUNTIME_PACKAGES = (
    "pydantic", "fastapi", "jinja2", "pyserial", "pywebview", "uvicorn", "websockets",
)
HIDDEN_IMPORTS = (
    "serial.serialwin32", "serial.tools.list_ports", "serial.tools.list_ports_windows",
    "flightmill.acquisition.windows_serial", "flightmill.desktop.launcher",
    "simulator.simulated_device", "clr", "webview.util", "webview.platforms.winforms",
    "webview.platforms.edgechromium", "uvicorn.logging", "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto", "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl", "uvicorn.lifespan.on",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def regular_files(root: Path) -> list[Path]:
    """Reject links/junctions instead of exporting files outside the payload."""
    files = []
    for directory, dirs, names in os.walk(root, followlinks=False):
        for name in dirs + names:
            path = Path(directory) / name
            attributes = getattr(path.lstat(), "st_file_attributes", 0)
            if path.is_symlink() or attributes & 0x400:
                raise ValueError(f"Package contains a link or reparse point: {path}")
        files.extend(Path(directory) / name for name in names)
    return sorted(files, key=lambda path: path.relative_to(root).as_posix())


def source_inventory(root: Path) -> dict[str, str]:
    files = []
    for folder in (root / "app/src/flightmill", root / "simulator"):
        for path in regular_files(folder):
            relative = path.relative_to(root)
            if "__pycache__" in relative.parts:
                continue
            ui = relative.as_posix().startswith(("app/src/flightmill/ui/static/",
                                                "app/src/flightmill/ui/templates/"))
            if path.suffix == ".py" or ui:
                files.append(path)
    return {path.relative_to(root).as_posix(): sha256(path) for path in sorted(files)}


def build_manifest(package_root: Path, info: dict) -> dict:
    entries = []
    seen = set()
    for path in regular_files(package_root):
        relative = path.relative_to(package_root).as_posix()
        if relative == "manifest.json":
            continue
        if relative.casefold() in seen:
            raise ValueError(f"Case-insensitive package path collision: {relative}")
        seen.add(relative.casefold())
        entries.append({"path": relative, "bytes": path.stat().st_size, "sha256": sha256(path)})
    return {**info, "files": entries}


def verify_package(package_root: Path, manifest: dict) -> None:
    listed = manifest.get("files")
    if not isinstance(listed, list) or not listed:
        raise ValueError("Package manifest has no files")
    actual_files = {path.relative_to(package_root).as_posix(): path
                    for path in regular_files(package_root)
                    if path.relative_to(package_root).as_posix() != "manifest.json"}
    seen = set()
    for entry in listed:
        relative = entry.get("path", "") if isinstance(entry, dict) else ""
        parts = PurePosixPath(relative)
        if (not relative or parts.is_absolute() or "\\" in relative or ":" in relative
                or any(part in {"", ".", ".."} for part in relative.split("/"))
                or relative.casefold() in seen):
            raise ValueError(f"Invalid or duplicated package path: {relative!r}")
        seen.add(relative.casefold())
        path = actual_files.pop(relative, None)
        if (path is None or type(entry.get("bytes")) is not int
                or path.stat().st_size != entry["bytes"]
                or sha256(path) != entry.get("sha256")):
            raise ValueError(f"Package file missing or altered: {relative}")
    if actual_files:
        raise ValueError(f"Unlisted package files: {sorted(actual_files)}")


def write_zip(package_root: Path, destination: Path) -> None:
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED,
                         compresslevel=6) as archive:
        for path in regular_files(package_root):
            archive.write(path, ARCHIVE_ROOT + "/" + path.relative_to(package_root).as_posix())
    with zipfile.ZipFile(destination) as archive:
        bad = archive.testzip()
        if bad is not None:
            raise ValueError(f"ZIP verification failed: {bad}")


def checked_environment() -> dict:
    if os.name != "nt" or sys.version_info[:2] != (3, 12) or struct.calcsize("P") != 8:
        raise RuntimeError("Build using Windows and the configured 64-bit Python 3.12 runtime")
    pins = {}
    for line in (ROOT / "app/packaging/requirements-windows.lock").read_text().splitlines():
        if line and not line.startswith("#"):
            name, expected = line.split("==", 1)
            actual = metadata.version(name)
            if actual != expected:
                raise RuntimeError(f"Dependency mismatch: {name} {actual}; expected {expected}")
            pins[name] = actual
    return {"python": platform.python_version(), "platform": platform.platform(),
            "architecture": "x64", "packages": pins}


def build(output_directory: Path | None = None) -> Path:
    environment = checked_environment()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    package_id = "FlightMill-0.2.0-preview-" + stamp + "-win11-x64"
    if not re.fullmatch(r"[A-Za-z0-9.-]+", package_id):
        raise ValueError("Invalid package ID")
    output_directory = (output_directory or ROOT / "dist" / ("windows-" + stamp)).resolve()
    output_directory.mkdir(parents=True, exist_ok=False)
    workspace = ROOT / "build"
    workspace.mkdir(exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="win-", dir=workspace))
    package_root = output_directory / "package"
    package_root.mkdir()
    source_hashes = source_inventory(ROOT)
    build_info = {
        "package_id": package_id, "app_version": "0.2.0.dev0", "target_os": "Windows 11",
        "architecture": "x64", "built_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_kind": "Current approved working tree, including uncommitted changes",
        "device_firmware": "0.1.5-dev / Protocol 1 for delayed ordinary Connect",
        "ui_sha256": {name.removeprefix("app/src/flightmill/ui/"): value
                      for name, value in source_hashes.items()
                      if name.startswith("app/src/flightmill/ui/") and not name.endswith(".py")},
        "environment": environment,
    }
    build_info_path = work / "build_info.json"
    build_info_path.write_text(json.dumps(build_info, indent=2) + "\n", encoding="utf-8")
    args = [sys.executable, "-B", "-m", "PyInstaller", "--noconfirm", "--onedir", "--windowed",
            "--name", "FlightMill", "--specpath", str(work), "--workpath", str(work / "pyi"),
            "--distpath", str(work / "dist"), "--icon", str(ROOT / "app/packaging/flightmill.ico"),
            "--paths", str(ROOT / "app/src"), "--paths", str(ROOT),
            "--add-data", str(ROOT / "app/src/flightmill/ui/static") + ";flightmill/ui/static",
            "--add-data", str(ROOT / "app/src/flightmill/ui/templates") + ";flightmill/ui/templates",
            "--add-data", str(build_info_path) + ";flightmill", "--collect-all", "webview"]
    for name in RUNTIME_PACKAGES:
        args.extend(["--recursive-copy-metadata", name])
    for name in HIDDEN_IMPORTS:
        args.extend(["--hidden-import", name])
    args.append(str(ROOT / "app/packaging/entrypoint.py"))
    process_env = {**os.environ, "PYTHONPATH": str(ROOT / "app/src") + os.pathsep + str(ROOT)}
    log_path = output_directory / "build.log"
    print(f"Building {package_id}; log: {log_path}", flush=True)
    with log_path.open("w", encoding="utf-8") as log:
        result = subprocess.run(args, cwd=ROOT, env=process_env, stdout=log,
                                stderr=subprocess.STDOUT, check=False)
    if result.returncode:
        raise RuntimeError(f"PyInstaller failed ({result.returncode}); see {log_path}")
    if source_inventory(ROOT) != source_hashes:
        raise RuntimeError("Runtime source changed during build; rebuild from the final working tree")
    copytree(work / "dist/FlightMill", package_root / "FlightMill")
    for name in ("Install-FlightMill.ps1", "Install-FlightMill.cmd"):
        copy2(ROOT / "app/packaging/windows" / name, package_root / name)
    for source, name in ((ROOT / "docs/WINDOWS_PACKAGE_QUICK_START.txt", "README.txt"),
                         (ROOT / "app/packaging/THIRD_PARTY_NOTICES.txt", "THIRD_PARTY_NOTICES.txt"),
                         (ROOT / "LICENSE", "LICENSE"),
                         (ROOT / "NOTICE", "NOTICE")):
        copy2(source, package_root / name)
    base_python = Path(sys.base_prefix)
    license_path = base_python / "LICENSE.txt"
    if not license_path.is_file():
        raise RuntimeError(f"Bundled Python license unavailable: {license_path}")
    copy2(license_path, package_root / "PYTHON_LICENSE.txt")
    (package_root / "build_environment.json").write_text(
        json.dumps(environment, indent=2) + "\n", encoding="utf-8")
    info = {**build_info, "source_sha256": source_hashes,
            "verification": "Build and package checks only; second-PC operator acceptance pending",
            "distribution_scope": "PolyForm Noncommercial License 1.0.0; see LICENSE and NOTICE"}
    manifest = build_manifest(package_root, info)
    (package_root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    verify_package(package_root, manifest)
    archive = output_directory / (package_id + ".zip")
    write_zip(package_root, archive)
    checksum = sha256(archive)
    (output_directory / (archive.name + ".sha256")).write_text(
        checksum + "  " + archive.name + "\n", encoding="ascii")
    receipt = {"package_id": package_id, "zip": str(archive), "bytes": archive.stat().st_size,
               "sha256": checksum, "manifest_sha256": sha256(package_root / "manifest.json"),
               "file_count": len(manifest["files"]), "build_log": str(log_path),
               "status": "Built; extracted-bundle self-check and operator testing pending"}
    (output_directory / "build_receipt.json").write_text(
        json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(receipt, indent=2), flush=True)
    return archive


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-directory", type=Path)
    args = parser.parse_args()
    build(args.output_directory)


if __name__ == "__main__":
    main()
