from __future__ import annotations

import unittest

from flightmill.protocol.integrity import FindingCode, IntegrityTracker
from flightmill.constants import DEFAULT_MIN_EVENT_INTERVAL_US
from flightmill.protocol.models import (
    AckMessage,
    ArmCommand,
    EventMessage,
    ProtocolError,
    StartCommand,
    TrialStoppedMessage,
    parse_line,
)
from simulator.simulated_device import (
    SCENARIO_NAMES,
    SESSION_ID,
    SimulatedDevice,
    command_fields,
    run_scenario,
)


class SimulatorTests(unittest.TestCase):
    def test_command_ack_correlation(self) -> None:
        sim = SimulatedDevice()
        sim.boot()
        command = ArmCommand(session_id=SESSION_ID, **command_fields(1))
        messages = sim.process(command)
        self.assertIsInstance(messages[0], AckMessage)
        self.assertEqual(messages[0].request_id, command.request_id)
        self.assertEqual(messages[0].command, command.type)

    def test_counters_reset_at_start_and_chatter_boundary(self) -> None:
        sim = SimulatedDevice()
        sim.boot()
        sim.process(ArmCommand(session_id=SESSION_ID, **command_fields(1)))
        sim.inject_dropped_events(3)
        sim.process(StartCommand(session_id=SESSION_ID, **command_fields(2)))
        first = sim.emit_event(0)
        self.assertIsInstance(first, EventMessage)
        assert first is not None
        self.assertEqual((first.event_n, first.dt_us, first.dropped_events), (1, 0, 0))
        self.assertIsNone(sim.emit_event(DEFAULT_MIN_EVENT_INTERVAL_US - 1))
        boundary = sim.emit_event(DEFAULT_MIN_EVENT_INTERVAL_US)
        assert boundary is not None
        self.assertEqual((boundary.event_n, boundary.dt_us), (2, DEFAULT_MIN_EVENT_INTERVAL_US))

    def test_zero_event_scenario_has_clean_zero_summary(self) -> None:
        messages = [parse_line(frame) for frame in run_scenario("zero_event") if frame]
        summary = next(message for message in messages if isinstance(message, TrialStoppedMessage))
        self.assertEqual(summary.accepted_event_count, 0)
        self.assertEqual(summary.dropped_events, 0)

    def test_required_scenario_inventory_and_expected_faults(self) -> None:
        self.assertEqual(len(SCENARIO_NAMES), 12)
        with self.assertRaises(ProtocolError):
            parse_line(next(frame for frame in run_scenario("malformed_line") if frame == b"{malformed-json}\n"))
        self.assertIn(None, run_scenario("disconnect_reconnect"))

        tracker = IntegrityTracker(active_session_id=SESSION_ID)
        duplicate_findings: list[FindingCode] = []
        for frame in run_scenario("duplicate_event"):
            if frame is not None:
                message = parse_line(frame)
                if hasattr(message, "message_seq"):
                    duplicate_findings.extend(tracker.observe(message))
        self.assertIn(FindingCode.DUPLICATE_MESSAGE, duplicate_findings)

    def test_reset_scenario_changes_boot_id_during_trial(self) -> None:
        messages = [parse_line(frame) for frame in run_scenario("device_reset_during_trial") if frame]
        boot_ids = {message.boot_id for message in messages if hasattr(message, "boot_id")}
        self.assertEqual(len(boot_ids), 2)


if __name__ == "__main__":
    unittest.main()
