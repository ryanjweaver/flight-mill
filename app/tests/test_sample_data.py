from __future__ import annotations

import csv
import hashlib
import unittest
from pathlib import Path

from flightmill.constants import RAW_CSV_HEADER


ROOT = Path(__file__).parents[2]


class SampleDataTests(unittest.TestCase):
    def _metadata(self, path: Path) -> dict[str, str]:
        with path.open(encoding="utf-8", newline="") as stream:
            rows = list(csv.reader(stream))
        self.assertEqual(rows[0], ["field", "value"])
        return dict(rows[1:])

    def test_zero_event_fixture_is_header_only_and_auditable(self) -> None:
        folder = ROOT / "sample_data" / "zero_event"
        raw = folder / "Mrot_zero1_A1_Attempt1.csv"
        metadata = self._metadata(folder / "Mrot_zero1_A1_Attempt1_metadata.csv")
        with raw.open(encoding="utf-8", newline="") as stream:
            rows = list(csv.reader(stream))
        self.assertEqual(rows, [list(RAW_CSV_HEADER)])
        self.assertEqual(metadata["incomplete"], "false")
        self.assertEqual(metadata["accepted_event_count"], "0")
        self.assertEqual(metadata["actual_duration_s"], "60.0")
        self.assertEqual(hashlib.sha256(raw.read_bytes()).hexdigest(), metadata["raw_csv_sha256"])

    def test_normal_fixture_values_and_checksum(self) -> None:
        folder = ROOT / "sample_data" / "protocol_v1"
        raw = folder / "Mrot_prelim2_A6_Attempt1.csv"
        metadata = self._metadata(folder / "Mrot_prelim2_A6_Attempt1_metadata.csv")
        with raw.open(encoding="utf-8", newline="") as stream:
            rows = list(csv.reader(stream))
        self.assertEqual(rows[0], list(RAW_CSV_HEADER))
        self.assertEqual(rows[2][7], "0.6283185307")
        self.assertEqual(metadata["accepted_event_count"], "2")
        self.assertEqual(hashlib.sha256(raw.read_bytes()).hexdigest(), metadata["raw_csv_sha256"])


if __name__ == "__main__":
    unittest.main()
