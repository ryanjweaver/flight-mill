from __future__ import annotations

import tempfile
import threading
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import patch

from flightmill.protocol.models import EventMessage
from serial import SerialException
from hardware.validation.checkpoint_02_runner import (
    Board,
    Evidence,
    esptool_probe_command,
    operator_action,
    validate_event_batch,
)


def event(number: int, event_us: int, dt_us: int, dropped: int = 0) -> EventMessage:
    return EventMessage(
        protocol="flightmill",
        protocol_version=1,
        device_id="FM-S3-001122334455",
        firmware_version="0.1.0-dev",
        message_seq=number,
        boot_id="boot-1",
        session_id="20000000-0000-4000-8000-000000000001",
        event_n=number,
        event_us=event_us,
        dt_us=dt_us,
        dropped_events=dropped,
    )


class Checkpoint02RunnerTests(unittest.TestCase):
    def test_fragmented_serial_line_is_reassembled_without_losing_raw_chunks(self) -> None:
        frame = event(1, 50_000, 0).model_dump_json().encode() + b"\n"

        class FragmentedSerial:
            def __init__(self) -> None:
                self.chunks = deque([frame[:30], frame[30:]])

            def readline(self) -> bytes:
                return self.chunks.popleft() if self.chunks else b""

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "fragments"
            evidence = Evidence(output, "test", "COM_TEST", None)
            board = Board("COM_TEST", evidence)
            board.serial = FragmentedSerial()
            board.allow_startup_noise = False
            try:
                self.assertEqual(board.read_message(0.2).event_n, 1)
                self.assertEqual(evidence.protocol_frames, [frame])
                self.assertFalse(evidence.malformed_after_handshake)
                self.assertEqual(len((output / "raw_serial_capture.jsonl").read_text().splitlines()), 2)
            finally:
                board.serial = None
                evidence.finalize()

    def test_expected_disconnect_during_operator_wait_is_recorded(self) -> None:
        read_attempted = threading.Event()

        class DisconnectedSerial:
            is_open = True

            def readline(self) -> bytes:
                read_attempted.set()
                raise SerialException("device was unplugged")

        with tempfile.TemporaryDirectory() as directory:
            evidence = Evidence(Path(directory) / "disconnect", "test", "COM_TEST", None)
            board = Board("COM_TEST", evidence)
            board.serial = DisconnectedSerial()

            def unplug_and_answer(prompt: str) -> str:
                read_attempted.wait(1.0)
                return "DONE"

            try:
                with patch("builtins.input", side_effect=unplug_and_answer):
                    self.assertTrue(operator_action(
                        evidence, "unplug", "Disconnect USB", allow_disconnect=True
                    ))
                self.assertEqual(
                    evidence.operator_observations["expected_disconnect_read_error"],
                    "SerialException",
                )
            finally:
                board.serial = None
                evidence.finalize()

    def test_operator_wait_continues_capture_and_preserves_events_for_checks(self) -> None:
        """A person may pause indefinitely before typing DONE; USB must be drained."""
        frames = [event(n, n * 50_000, 0 if n == 1 else 50_000) for n in range(1, 4)]

        class FakeSerial:
            is_open = True

            def __init__(self) -> None:
                self.frames = deque(frame.model_dump_json().encode() + b"\n" for frame in frames)

            def readline(self) -> bytes:
                return self.frames.popleft() if self.frames else b""

            def close(self) -> None:
                self.is_open = False

        with tempfile.TemporaryDirectory() as directory:
            evidence = Evidence(Path(directory) / "capture", "test", "COM_TEST", None)
            board = Board("COM_TEST", evidence)
            board.serial = FakeSerial()
            captured = threading.Event()
            original_capture = evidence.capture_protocol

            def capture(raw: bytes) -> None:
                original_capture(raw)
                if len(evidence.protocol_frames) == len(frames):
                    captured.set()

            evidence.capture_protocol = capture
            captured_before_done = []

            def delayed_operator_answer(prompt: str) -> str:
                captured_before_done.append(captured.wait(1.0))
                return "DONE"

            try:
                with patch("builtins.input", side_effect=delayed_operator_answer):
                    self.assertTrue(operator_action(evidence, "beam", "Interrupt the beam"))
                self.assertEqual(captured_before_done, [True])
                received = [board.read_message(0.1) for _ in frames]
                self.assertEqual([message.event_n for message in received], [1, 2, 3])
                # Consuming queued messages must not write duplicate protocol frames.
                self.assertEqual(len(evidence.protocol_frames), 3)
            finally:
                board.close()
                evidence.finalize()

    def test_sequence_gap_prevents_pass_even_when_individual_checks_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            evidence = Evidence(Path(directory) / "gap", "test", "COM_TEST", None)
            try:
                evidence.add("sensor_clear", True, "0", "0")
                evidence.observe_message(event(1, 50_000, 0))
                evidence.observe_message(event(3, 150_000, 50_000))
                self.assertFalse(evidence.passed)
            finally:
                evidence.finalize()

    def test_malformed_capture_prevents_pass_even_when_individual_checks_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            evidence = Evidence(Path(directory) / "malformed", "test", "COM_TEST", None)
            try:
                evidence.add("sensor_clear", True, "0", "0")
                evidence.malformed_after_handshake.append("truncated JSON frame")
                self.assertFalse(evidence.passed)
            finally:
                evidence.finalize()

    def test_initial_probe_is_read_only_and_targets_selected_port(self) -> None:
        command = esptool_probe_command("COM_TEST")
        self.assertIn("flash_id", command)
        self.assertNotIn("write_flash", command)
        self.assertEqual(command[command.index("--port") + 1], "COM_TEST")

    def test_event_batch_requires_exact_sequence_zero_first_delta_and_no_drops(self) -> None:
        valid = [event(1, 1_000, 0), event(2, 51_000, 50_000)]
        self.assertTrue(validate_event_batch(valid, 2)[0])
        self.assertFalse(validate_event_batch(valid[:1], 2)[0])
        self.assertFalse(validate_event_batch([event(1, 1_000, 0, dropped=1)], 1)[0])

    def test_evidence_does_not_convert_ambiguous_to_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "evidence"
            evidence = Evidence(output, "test", "COM_TEST", None)
            evidence.ambiguous("visual_led", "YES or NO", "no answer")
            self.assertFalse(evidence.passed)
            evidence.finalize()
            results = (output / "results.json").read_text(encoding="utf-8")
            self.assertIn('"status": "AMBIGUOUS"', results)
            self.assertTrue((output / "raw_serial_capture.jsonl").exists())
            self.assertTrue((output / "protocol_transcript.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
