"""Physical START, ignored short press, and long STOP on unchanged firmware.

The host only arms the trial. Serial STOP is reserved for failure cleanup.
Capture continues while waiting for each operator action/LED confirmation.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from uuid import UUID, uuid4

from hardware.validation.checkpoint_02_runner import (
    Board,
    Evidence,
    MIN_EVENT_INTERVAL_US,
    default_output,
    operator_action,
    operator_yes_no,
    sha256_file,
)
from hardware.validation.sensor_single_cycle_diagnostic import require, verify_window
from flightmill.protocol.models import (
    DeviceMessage,
    EventMessage,
    Message,
    TrialStartedMessage,
    TrialStoppedMessage,
    parse_line,
)


def physical_transitions_match(
    messages: list[Message],
    session: UUID,
    boot_id: str,
    *,
    stopped: bool,
) -> bool:
    """Require one physical start, and zero/one physical stops; never duplicates."""
    starts = [m for m in messages if isinstance(m, TrialStartedMessage) and m.session_id == session]
    stops = [m for m in messages if isinstance(m, TrialStoppedMessage) and m.session_id == session]
    return (
        len(starts) == 1
        and starts[0].start_source == "physical_button"
        and len(stops) == int(stopped)
        and all(
            m.stop_source == "physical_button"
            and m.stop_reason == "physical_button"
            and m.accepted_event_count == 0
            and m.dropped_events == 0
            for m in stops
        )
        and not any(isinstance(m, EventMessage) for m in messages)
        and all(m.boot_id == boot_id for m in messages if isinstance(m, DeviceMessage))
        and not any(m.type in {"start", "stop", "error"} for m in messages)
    )


def check_transitions(evidence: Evidence, session: UUID, boot_id: str, *, stopped: bool) -> None:
    messages = [parse_line(frame) for frame in evidence.protocol_frames]
    require(
        evidence,
        "one_physical_stop_no_duplicates" if stopped else "short_press_no_transition",
        physical_transitions_match(messages, session, boot_id, stopped=stopped)
        and not evidence.sequence_issues
        and not evidence.malformed_after_handshake,
        f"one physical start, {int(stopped)} physical stops, no serial START/STOP, events or resets",
        f"starts={sum(isinstance(m, TrialStartedMessage) for m in messages)}; "
        f"stops={sum(isinstance(m, TrialStoppedMessage) for m in messages)}",
    )


def action(evidence: Evidence, name: str, prompt: str) -> None:
    if not operator_action(evidence, name, prompt):
        raise RuntimeError(f"{name} was not confirmed")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True)
    parser.add_argument("--expected-device-id", required=True)
    parser.add_argument("--firmware-bin", type=Path, required=True)
    args = parser.parse_args()
    evidence = Evidence(
        default_output("button_operation_diagnostic").resolve(),
        "button_operation_diagnostic",
        args.port,
        args.firmware_bin.resolve(),
    )
    board = Board(args.port, evidence)
    session: UUID | None = None
    (evidence.output_dir / "diagnostic_scope.json").write_text(
        json.dumps(
            {
                "scope": "Physical START/ignored short press/long STOP; NOT full Checkpoint 02 acceptance",
                "driver_source_sha256": sha256_file(Path(__file__)),
                "window_helper_source_sha256": sha256_file(
                    Path(__file__).with_name("sensor_single_cycle_diagnostic.py")
                ),
                "expected_device_id": args.expected_device_id,
                "min_event_interval_us": MIN_EVENT_INTERVAL_US,
                "physical_stop_instruction_seconds": 2,
                "serial_stop_allowed_only_for_failure_cleanup": True,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    try:
        board.open()
        hello = board.wait_for_hello()
        status = board.get_status()
        require(
            evidence,
            "clear_idle_precondition",
            hello.device_id == args.expected_device_id
            and hello.state == status.state == "IDLE"
            and hello.boot_id == status.boot_id
            and status.sensor_state == 0
            and status.event_n == status.dropped_events == 0,
            "expected device, fresh clear IDLE, zero counts/drops",
            status.model_dump_json(),
        )
        session = uuid4()
        armed = board.arm(session)
        status = board.get_status()
        require(
            evidence,
            "armed_not_recording",
            status.state == "ARMED"
            and status.session_id == session
            and status.boot_id == hello.boot_id
            and status.sensor_state == 0
            and status.event_n == status.dropped_events == 0
            and armed.config.min_event_interval_us == MIN_EVENT_INTERVAL_US,
            "ARMED, clear, zero events, requested interval",
            status.model_dump_json(),
        )
        action(
            evidence,
            "action_physical_start",
            "Keep the flag clear. Press and release START/STOP once briefly (about half a second)",
        )
        started = board.wait_for(
            lambda m: isinstance(m, TrialStartedMessage) and m.session_id == session,
            5.0,
            "physical-button trial_started",
        )
        require(
            evidence,
            "physical_button_start",
            started.start_source == "physical_button",
            "trial_started from physical_button",
            started.model_dump_json(),
        )
        if not operator_yes_no(
            evidence,
            "recording_leds",
            "Are green READY and blue REC both steadily lit, with EVENT off?",
        ):
            raise RuntimeError("Recording LEDs were not confirmed")
        verify_window(board, evidence, "recording_clear_baseline", 0, 0, session)
        action(
            evidence,
            "action_recording_short_press",
            "Keep the flag clear. Press and release START/STOP once briefly (about half a second)",
        )
        verify_window(board, evidence, "recording_short_press_ignored", 0, 0, session)
        check_transitions(evidence, session, hello.boot_id, stopped=False)
        action(
            evidence,
            "action_physical_long_stop",
            "Keep the flag clear. Hold START/STOP for at least TWO seconds, then release it",
        )
        stopped = board.wait_for(
            lambda m: isinstance(m, TrialStoppedMessage) and m.session_id == session,
            6.0,
            "physical-button trial_stopped",
        )
        require(
            evidence,
            "physical_button_long_stop",
            stopped.stop_source == "physical_button",
            "trial_stopped from physical_button",
            stopped.model_dump_json(),
        )
        settle = time.monotonic() + 2.0
        while time.monotonic() < settle:
            board.read_message(0.1)
        status = board.get_status()
        require(
            evidence,
            "complete_after_physical_stop",
            status.state == "COMPLETE"
            and status.session_id == session
            and status.boot_id == hello.boot_id
            and status.sensor_state == 0
            and status.event_n == status.dropped_events == 0,
            "same trial COMPLETE, clear, zero counts/drops",
            status.model_dump_json(),
        )
        if not operator_yes_no(
            evidence,
            "stopped_leds",
            "Is PWR steady, READY slowly blinking, and both blue REC and yellow EVENT off?",
        ):
            raise RuntimeError("Stopped LEDs were not confirmed")
        check_transitions(evidence, session, hello.boot_id, stopped=True)
        board.disarm(session)
        status = board.get_status()
        require(
            evidence,
            "returned_idle",
            status.state == "IDLE" and status.boot_id == hello.boot_id,
            "same boot IDLE after disarm",
            status.model_dump_json(),
        )
    except (EOFError, KeyboardInterrupt):
        evidence.ambiguous(
            "diagnostic_interrupted", "completed physical button sequence", "interrupted"
        )
    except Exception as exc:
        evidence.add(
            "diagnostic_exception",
            False,
            "complete physical button sequence",
            f"{type(exc).__name__}: {exc}",
        )
        print(f"Diagnostic stopped: {type(exc).__name__}: {exc}", flush=True)
    finally:
        if session is not None and board.serial is not None and board.serial.is_open:
            try:
                status = board.get_status()
                if status.state == "RECORDING" and status.session_id == session:
                    summary = board.stop(session, "button_diagnostic_failure_cleanup")
                    evidence.add(
                        "serial_failure_cleanup",
                        True,
                        "safety cleanup only; NOT a physical STOP pass",
                        summary.model_dump_json(),
                    )
                    status = board.get_status()
                if status.state in {"ARMED", "COMPLETE"} and status.session_id == session:
                    board.disarm(session)
            except Exception as exc:
                evidence.add(
                    "cleanup_failed", False, "stopped and disarmed", f"{type(exc).__name__}: {exc}"
                )
        board.close()
        evidence.finalize()
    print(f"Evidence directory: {evidence.output_dir}", flush=True)
    print(f"Button operation diagnostic: {'PASS' if evidence.passed else 'NOT PASS'}", flush=True)
    return 0 if evidence.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
