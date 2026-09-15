from __future__ import annotations

import unittest
from datetime import datetime, timezone
from uuid import UUID

from pydantic import ValidationError

from flightmill.acquisition.calculations import circumference_m
from flightmill.hardware import DEV_BOARD_MODEL
from flightmill.storage.metadata import TrialMetadata


SESSION = UUID("20000000-0000-4000-8000-000000000001")


def metadata_fields() -> dict[str, object]:
    return {
        "trial_uuid": SESSION,
        "session_id": SESSION,
        "created_at_local_iso8601": datetime(2026, 9, 2, 13, 0, tzinfo=timezone.utc),
        "species_code": "Mrot",
        "trial_type": "prelim",
        "trial_number": 2,
        "well_id": "A6",
        "attempt": 1,
        "circumference_m": circumference_m(0.10),
        "device_id": "SIM-FM-0001",
        "exact_esp32s3_dev_board_model": DEV_BOARD_MODEL,
        "firmware_version": "sim-0.1.0",
        "application_version": "0.1.0-dev",
        "serial_port": "SIM",
        "baud_rate": 115200,
    }


class MetadataTests(unittest.TestCase):
    def test_incomplete_metadata_serializes_field_value_rows(self) -> None:
        metadata = TrialMetadata(**metadata_fields())
        rows = dict(metadata.field_value_rows()[1:])
        self.assertEqual(rows["metadata_schema_version"], "1")
        self.assertEqual(rows["incomplete"], "true")
        self.assertEqual(rows["arm_radius_m"], "0.1")

    def test_complete_metadata_requires_stop_evidence_and_checksum(self) -> None:
        with self.assertRaises(ValidationError):
            TrialMetadata(**metadata_fields(), incomplete=False)

    def test_naive_timestamp_and_unvalidated_interval_fail(self) -> None:
        fields = metadata_fields()
        fields["created_at_local_iso8601"] = datetime(2026, 9, 2, 13, 0)
        with self.assertRaises(ValidationError):
            TrialMetadata(**fields)
        fields = metadata_fields()
        fields["min_event_interval_us"] = 49_999
        with self.assertRaises(ValidationError):
            TrialMetadata(**fields)


if __name__ == "__main__":
    unittest.main()
