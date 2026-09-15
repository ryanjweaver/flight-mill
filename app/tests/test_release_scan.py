from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.check_release_contents import scan


class ReleaseScanTests(unittest.TestCase):
    def test_clean_tree_passes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "clean.md").write_text("No sensitive content.", encoding="utf-8")
            self.assertEqual(scan(root), [])

    def test_scan_root_under_release_is_not_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "release" / "candidate"
            root.mkdir(parents=True)
            (root / "path.txt").write_text(
                "C:" + "\\Users\\AResearcher\\Desktop", encoding="utf-8"
            )
            self.assertEqual(len(scan(root)), 1)

    def test_jsonl_and_cpp_header_are_scanned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("capture.jsonl", "config.hpp"):
                (root / name).write_text(
                    "C:" + "\\Users\\AResearcher\\Desktop", encoding="utf-8"
                )
            self.assertEqual(len(scan(root)), 2)

    def test_artifact_mode_scans_every_member_and_rejects_dependency_folders(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nested = root / ".venv"
            nested.mkdir()
            (nested / "unusual.log").write_text(
                "C:" + "\\Users\\AResearcher\\Desktop", encoding="utf-8"
            )
            (root / "binary.elf").write_bytes(
                b"\x7fELF\x00" + b"C:" + b"\\Users\\AResearcher\\build\x00"
            )
            findings = scan(root, artifact=True)
            self.assertTrue(any("excluded artifact directory" in item for item in findings))
            self.assertEqual(sum("personal Windows user path" in item for item in findings), 2)

    def test_empty_artifact_does_not_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(scan(Path(directory), artifact=True), ["artifact contains no files"])

    def test_escaped_json_and_utf16_paths_are_found(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "escaped.json").write_text(
                '{"path":"C:' + '\\\\Users\\\\AResearcher\\\\Desktop"}', encoding="utf-8"
            )
            (root / "powershell.log").write_text(
                "C:" + "\\Users\\AResearcher\\Desktop", encoding="utf-16"
            )
            self.assertEqual(len(scan(root, artifact=True)), 2)

    def test_local_dependency_and_build_directories_are_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in (".pio", ".pio-core", ".venv", "venv"):
                dependency = root / name
                dependency.mkdir()
                (dependency / "external.txt").write_text(
                    "C:" + "\\Users\\ThirdParty\\cache",
                    encoding="utf-8",
                )
            self.assertEqual(scan(root), [])

    def test_prohibited_source_personal_path_and_secret_are_found(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "source.md").write_text("jo" + "ve", encoding="utf-8")
            (root / "path.txt").write_text(
                "C:" + "\\Users\\AResearcher\\Desktop\\data",
                encoding="utf-8",
            )
            (root / "secret.py").write_text(
                "api" + "_key = 'do-not-ship'",
                encoding="utf-8",
            )
            findings = scan(root)
            labels = {finding.split(":", 1)[0] for finding in findings}
            self.assertEqual(
                labels,
                {
                    "prohibited source name",
                    "personal Windows user path",
                    "credential assignment",
                },
            )


if __name__ == "__main__":
    unittest.main()
