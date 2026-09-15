from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from flightmill.storage.naming import (
    FilenameCollisionError,
    FilenameError,
    TrialName,
    TrialPaths,
)
from flightmill.storage.raw_csv import write_raw_csv
from flightmill.constants import RAW_CSV_HEADER


class NamingAndCsvTests(unittest.TestCase):
    def test_golden_filename(self) -> None:
        name = TrialName("Mrot", "prelim", 2, "A6", 1)
        self.assertEqual(name.raw_filename, "Mrot_prelim2_A6_Attempt1.csv")

    def test_invalid_windows_characters_are_sanitized(self) -> None:
        name = TrialName("M:rot", "pre/lim", 2, "A 6", 1)
        self.assertEqual(name.raw_filename, "M-rot_pre-lim2_A-6_Attempt1.csv")

    def test_reserved_or_empty_tokens_fail(self) -> None:
        with self.assertRaises(FilenameError):
            _ = TrialName("CON", "prelim", 1, "A1", 1).raw_filename
        with self.assertRaises(FilenameError):
            _ = TrialName("***", "prelim", 1, "A1", 1).raw_filename

    def test_collision_checks_final_and_partial_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = TrialPaths.build(Path(directory), TrialName("Mrot", "prelim", 2, "A6", 1))
            paths.raw.write_text("existing", encoding="utf-8")
            with self.assertRaises(FilenameCollisionError):
                paths.assert_available()

    def test_zero_event_csv_is_header_only_without_bom(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Mrot_prelim2_A6_Attempt1.csv"
            write_raw_csv(path, [])
            raw = path.read_bytes()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
            self.assertEqual(raw.decode("utf-8"), ",".join(RAW_CSV_HEADER) + "\n")
            with path.open(encoding="utf-8", newline="") as stream:
                self.assertEqual(list(csv.reader(stream)), [list(RAW_CSV_HEADER)])
            with self.assertRaises(FileExistsError):
                write_raw_csv(path, [])


if __name__ == "__main__":
    unittest.main()
