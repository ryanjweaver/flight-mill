from __future__ import annotations

import unittest
from pathlib import Path
from uuid import UUID

from flightmill.protocol.integrity import FindingCode, IntegrityTracker
from flightmill.protocol.models import (
    EventMessage,
    ProtocolError,
    TrialStoppedMessage,
    parse_line,
    serialize_line,
)


ROOT = Path(__file__).parents[2]
SESSION = UUID("20000000-0000-4000-8000-000000000001")


class ProtocolTests(unittest.TestCase):
    def test_all_valid_examples_round_trip(self) -> None:
        path = ROOT / "protocol" / "examples" / "valid_session.jsonl"
        for line in path.read_bytes().splitlines(keepends=True):
            if not line.strip():
                continue
            message = parse_line(line)
            self.assertEqual(parse_line(serialize_line(message)), message)

    def test_all_invalid_examples_fail(self) -> None:
        path = ROOT / "protocol" / "examples" / "invalid_messages.jsonl"
        for line in path.read_bytes().splitlines(keepends=True):
            if not line.strip():
                continue
            with self.subTest(line=line):
                with self.assertRaises(ProtocolError):
                    parse_line(line)

    def test_invalid_utf8_and_nonobject_fail(self) -> None:
        with self.assertRaises(ProtocolError):
            parse_line(b"\xff\n")
        with self.assertRaises(ProtocolError):
            parse_line("[]\n")

    def test_integrity_tracker_detects_duplicate_gap_reset_and_drop(self) -> None:
        tracker = IntegrityTracker()
        tracker.arm(SESSION)
        common = {
            "protocol": "flightmill",
            "protocol_version": 1,
            "device_id": "SIM-FM-0001",
            "firmware_version": "sim-0.1.0",
            "boot_id": "boot-1",
            "session_id": SESSION,
        }
        first = EventMessage(message_seq=1, event_n=1, event_us=0, dt_us=0, dropped_events=0, **common)
        self.assertEqual(tracker.observe(first), [])
        self.assertIn(FindingCode.DUPLICATE_MESSAGE, tracker.observe(first))
        third = EventMessage(message_seq=3, event_n=3, event_us=1_000_000, dt_us=1_000_000, dropped_events=2, **common)
        findings = tracker.observe(third)
        self.assertIn(FindingCode.MESSAGE_GAP, findings)
        self.assertIn(FindingCode.EVENT_GAP, findings)
        self.assertIn(FindingCode.DROPPED_EVENTS_INCREASED, findings)
        reset = EventMessage(message_seq=1, event_n=1, event_us=0, dt_us=0, dropped_events=0, **{**common, "boot_id": "boot-2"})
        self.assertIn(FindingCode.BOOT_CHANGED, tracker.observe(reset))

    def test_final_summary_accepts_zero_events(self) -> None:
        summary = TrialStoppedMessage(
            protocol="flightmill",
            protocol_version=1,
            device_id="SIM-FM-0001",
            firmware_version="sim-0.1.0",
            message_seq=5,
            boot_id="boot-1",
            session_id=SESSION,
            accepted_event_count=0,
            dropped_events=0,
            duration_us=60_000_000,
            stop_reason="planned_duration",
            stop_source="automatic",
        )
        self.assertEqual(summary.accepted_event_count, 0)


if __name__ == "__main__":
    unittest.main()
