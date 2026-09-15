"""Record installed GUI dependency versions and available license notices."""

from __future__ import annotations

import importlib.metadata as metadata
import json
import platform
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    pending = ["pydantic", "fastapi", "jinja2", "pyserial", "pywebview", "uvicorn",
               "websockets", "pyinstaller", "httpx", "pytest", "pytest-asyncio",
               "hatchling", "ruff"]
    found = {}
    while pending:
        name = canonicalize_name(pending.pop())
        if name in found:
            continue
        distribution = metadata.distribution(name)
        found[name] = distribution
        for dependency in distribution.requires or []:
            requirement = Requirement(dependency)
            if requirement.marker is None or requirement.marker.evaluate({"extra": ""}):
                pending.append(requirement.name)
    lines = ["# Resolved Windows Python 3.12 GUI preview environment.",
             "# Includes test/build tools. Hash-locked release validation is a separate gate."]
    lines.extend(f"{name}=={found[name].version}" for name in sorted(found))
    (root / "app/packaging/requirements-windows.lock").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    notices = ["Flight Mill GUI preview: third-party dependency notices",
               "Generated from installed package metadata. Includes development/build tools.",
               "Python: " + platform.python_version(), ""]
    for name in sorted(found):
        distribution = found[name]
        info = distribution.metadata
        notices.extend(["=" * 72, f"{name} {distribution.version}",
                        "License: " + str(info.get("License-Expression")
                                          or info.get("License") or "See package notices"), ""])
        for item in distribution.files or []:
            filename = item.name.lower()
            if (".dist-info" in str(item) and
                    (filename.startswith(("license", "copying", "notice"))
                     or "/licenses/" in str(item).replace("\\", "/"))):
                location = distribution.locate_file(item)
                if location.is_file():
                    notices.extend([str(item), location.read_text(encoding="utf-8",
                                                                 errors="replace"), ""])
    (root / "app/packaging/THIRD_PARTY_NOTICES.txt").write_text(
        "\n".join(notices), encoding="utf-8"
    )
    inventory = {"python": platform.python_version(), "platform": platform.platform(),
                 "packages": {name: found[name].version for name in sorted(found)}}
    (root / "app/packaging/build_environment.json").write_text(
        json.dumps(inventory, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Recorded {len(found)} installed GUI/test/build dependencies.")


if __name__ == "__main__":
    main()
