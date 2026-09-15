"""Fail a production-tree scan on known release hygiene violations."""

from __future__ import annotations

import argparse
import os
import re
from pathlib import Path


TEXT_SUFFIXES = {
    ".c",
    ".cpp",
    ".css",
    ".csv",
    ".h",
    ".hpp",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".jsonl",
    ".log",
    ".map",
    ".md",
    ".ps1",
    ".py",
    ".r",
    ".svg",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
SKIP_PARTS = {
    ".git",
    ".pio",
    ".pio-core",
    ".venv",
    "__pycache__",
    "reference",
    "release",
    "venv",
    "build",
    "dist",
    "data",
    ".runtime",
}
FORBIDDEN_NAMES = {".rhistory"}
FORBIDDEN_PATTERNS = {
    "personal Windows user path": re.compile(r"[A-Za-z]:[/\\]+Users[/\\]+[^/\\\s]+", re.I),
    "prohibited source name": re.compile(r"\b" + "jo" + "ve" + r"\b", re.I),
    "credential assignment": re.compile(
        r"(?i)(api[_-]?key|access[_-]?token|client[_-]?secret)\s*[:=]\s*['\"][^'\"]+"
    ),
}


def scan(root: Path, *, artifact: bool = False) -> list[str]:
    """Scan source with local exclusions, or every byte-decoded artifact member."""
    findings: list[str] = []
    file_count = 0
    paths = []
    for directory, directories, files in os.walk(root, followlinks=False):
        if not artifact:
            directories[:] = [name for name in directories if name.lower() not in SKIP_PARTS]
        paths.extend(Path(directory) / name for name in files)
        paths.extend(Path(directory) / name for name in directories
                     if (Path(directory) / name).is_symlink())
    for path in paths:
        relative = path.relative_to(root)
        excluded = any(part.lower() in SKIP_PARTS for part in relative.parts[:-1])
        if not artifact and excluded:
            continue
        if artifact and path.is_symlink():
            findings.append(f"symbolic link in artifact: {relative}")
            continue
        if not path.is_file():
            continue
        file_count += 1
        if artifact and excluded:
            findings.append(f"excluded artifact directory: {relative}")
        if path.name.lower() in FORBIDDEN_NAMES:
            findings.append(f"forbidden filename: {relative}")
        if not artifact and path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        raw = path.read_bytes()
        encoding = "utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8"
        text = raw.decode(encoding, errors="replace")
        for label, pattern in FORBIDDEN_PATTERNS.items():
            if pattern.search(text):
                findings.append(f"{label}: {relative}")
    if artifact and not file_count:
        findings.append("artifact contains no files")
    return findings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", type=Path, default=Path(__file__).parents[1])
    parser.add_argument("--artifact", action="store_true",
                        help="scan every member, including logs/binaries; reject excluded directories")
    args = parser.parse_args()
    findings = scan(args.root.resolve(), artifact=args.artifact)
    if findings:
        print("Release-content scan failed:")
        for finding in findings:
            print(f"- {finding}")
        return 1
    print("Release-content scan passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
