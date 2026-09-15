from __future__ import annotations

import json
import unittest
from pathlib import Path

from flightmill.protocol.conformance import check_transcript
from flightmill.protocol.models import MESSAGE_ADAPTER
from flightmill.storage.metadata import TrialMetadata
from simulator.simulated_device import run_scenario


ROOT = Path(__file__).parents[2]


class ConformanceTests(unittest.TestCase):
    def test_canonical_bidirectional_transcript_passes(self) -> None:
        frames = (ROOT / "protocol" / "examples" / "valid_session.jsonl").read_bytes().splitlines(keepends=True)
        report = check_transcript([frame for frame in frames if frame.strip()])
        self.assertEqual(report.issues, [])

    def test_normal_and_zero_device_scenarios_pass(self) -> None:
        self.assertTrue(check_transcript(run_scenario("normal")).passed)
        self.assertTrue(check_transcript(run_scenario("zero_event")).passed)

    def test_fault_scenarios_are_detected(self) -> None:
        expected = {
            "malformed_line": "malformed_line",
            "duplicate_event": "duplicate_message",
            "skipped_message_sequence": "message_gap",
            "device_reset_during_trial": "boot_changed",
            "disconnect_reconnect": "disconnect",
            "reported_drops": "dropped_events_increased",
        }
        for scenario, code in expected.items():
            with self.subTest(scenario=scenario):
                report = check_transcript(run_scenario(scenario))
                self.assertIn(code, {issue.code for issue in report.issues})

    def test_committed_schema_documents_cover_executable_models(self) -> None:
        serial_schema = json.loads(
            (ROOT / "protocol" / "serial_protocol_v1.schema.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            set(serial_schema["required"]),
            {"protocol", "protocol_version", "type", "device_id", "firmware_version"},
        )
        self.assertEqual(serial_schema["$defs"]["config"]["properties"]["min_event_interval_us"]["enum"], [50_000, 150_000])
        generated = MESSAGE_ADAPTER.json_schema()
        branches = []
        for branch in generated["oneOf"]:
            if "$ref" in branch:
                branches.append(generated["$defs"][branch["$ref"].rsplit("/", 1)[-1]])
            else:
                branches.append(branch)
        model_types = {branch["properties"]["type"]["const"] for branch in branches}
        self.assertEqual(set(serial_schema["properties"]["type"]["enum"]), model_types)

        metadata_schema = json.loads(
            (ROOT / "protocol" / "trial_metadata_v1.schema.json").read_text(encoding="utf-8")
        )
        self.assertEqual(set(metadata_schema["properties"]), set(TrialMetadata.model_fields))
        self.assertEqual(metadata_schema["properties"]["arm_radius_m"]["default"], 0.10)
        self.assertIn("exact_esp32s3_dev_board_model", metadata_schema["required"])


if __name__ == "__main__":
    unittest.main()
