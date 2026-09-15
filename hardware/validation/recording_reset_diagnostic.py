"""Operator reset after one recorded revolution; never manufacture a reset by probing again."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from uuid import UUID, uuid4

from hardware.validation.checkpoint_02_runner import (
    Evidence,
    MIN_EVENT_INTERVAL_US,
    default_output,
    operator_action,
    sha256_file,
)
from hardware.validation.reset_capture_board import ResetCaptureBoard as Board
from hardware.validation.sensor_single_cycle_diagnostic import require, verify_window
from flightmill.protocol.models import (
    DeviceMessage,
    EventMessage,
    HelloMessage,
    Message,
    StatusMessage,
    TrialStartedMessage,
    TrialStoppedMessage,
    parse_line,
)


def reset_matches(
    messages: list[Message], session: UUID, old_boot: str, new_boot: str, device: str
) -> bool:
    hellos = [m for m in messages if isinstance(m, HelloMessage)]
    starts = [m for m in messages if isinstance(m, TrialStartedMessage)]
    events = [m for m in messages if isinstance(m, EventMessage)]
    statuses = [m for m in messages if isinstance(m, StatusMessage) and m.boot_id == new_boot]
    if old_boot == new_boot or len(hellos) != 2 or len(starts) != 1 or len(events) != 1:
        return False
    event = events[0]
    reset_index = messages.index(hellos[1])
    return (
        [m.boot_id for m in hellos] == [old_boot, new_boot]
        and all(m.message_seq == 0 and m.state == "IDLE" for m in hellos)
        and all(m.device_id == device for m in messages if isinstance(m, DeviceMessage))
        and all(
            m.boot_id == old_boot for m in messages[:reset_index] if isinstance(m, DeviceMessage)
        )
        and all(
            m.boot_id == new_boot for m in messages[reset_index:] if isinstance(m, DeviceMessage)
        )
        and starts[0].session_id == event.session_id == session
        and starts[0].boot_id == event.boot_id == old_boot
        and starts[0].start_source == "ui"
        and starts[0].message_seq < event.message_seq
        and event.event_n == 1
        and event.dt_us == event.dropped_events == 0
        and bool(statuses)
        and all(
            m.state == "IDLE"
            and m.session_id is None
            and m.sensor_state == m.event_n == m.dropped_events == 0
            for m in statuses
        )
        and not any(
            isinstance(m, TrialStoppedMessage) or m.type in {"stop", "error"} for m in messages
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True)
    parser.add_argument("--expected-device-id", required=True)
    parser.add_argument("--firmware-bin", type=Path, required=True)
    args = parser.parse_args()
    evidence = Evidence(
        default_output("recording_reset_diagnostic").resolve(),
        "recording_reset_diagnostic",
        args.port,
        args.firmware_bin.resolve(),
    )
    board = Board(args.port, evidence)
    session: UUID | None = None
    reset_requested = False
    (evidence.output_dir / "diagnostic_scope.json").write_text(
        json.dumps(
            {
                "scope": "Physical reset after one recorded revolution; NOT full Checkpoint 02 acceptance",
                "driver_source_sha256": sha256_file(Path(__file__)),
                "window_helper_source_sha256": sha256_file(
                    Path(__file__).with_name("sensor_single_cycle_diagnostic.py")
                ),
                "expected_device_id": args.expected_device_id,
                "min_event_interval_us": MIN_EVENT_INTERVAL_US,
                "initial_probe_resets_before_trial": True,
                "probe_or_software_reset_after_operator_action": False,
                "capture_loss_is_not_a_hardware_failure_or_pass": True,
                "operator_readiness_required_before_arm": True,
                "reset_usb_recovery_source_sha256": sha256_file(
                    Path(__file__).with_name("reset_capture_board.py")
                ),
                "serial_attach_source_sha256": sha256_file(
                    Path(__file__).with_name("post_reset_status_diagnostic.py")
                ),
                "windows_reset_serial_source_sha256": sha256_file(
                    Path(__file__).with_name("windows_reset_serial.py")
                ),
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
            and status.sensor_state == status.event_n == status.dropped_events == 0,
            "expected device, fresh clear IDLE, zero counts/drops",
            status.model_dump_json(),
        )
        if not operator_action(
            evidence,
            "operator_ready_before_start",
            "Stay at the bench with the flag clear and all buttons untouched. "
            "Confirm you are ready for a fresh recording; do NOT rotate yet",
        ):
            raise RuntimeError("Operator readiness was not confirmed; no trial armed or started")
        status = board.get_status()
        require(
            evidence,
            "still_clear_idle_after_ready",
            status.boot_id == hello.boot_id
            and status.state == "IDLE"
            and status.sensor_state == status.event_n == status.dropped_events == 0,
            "same boot, clear IDLE, zero counts/drops immediately before ARM",
            status.model_dump_json(),
        )
        session = uuid4()
        board.arm(session)
        board.start(session)
        verify_window(board, evidence, "recording_clear_baseline", 0, 0, session)
        if not operator_action(
            evidence,
            "one_revolution_before_reset",
            "Rotate the actual arm through exactly ONE complete revolution, passing the flag "
            "through the sensor once without pausing in the slot. Stop with the flag clear. "
            "Do not press START/STOP or RESET yet. Report any accidental extra passage",
        ):
            raise RuntimeError("Exactly one revolution was not confirmed")
        verify_window(board, evidence, "one_event_before_reset", 0, 1, session)
        board.enable_reset_recovery()
        reset_requested = True
        board.allow_startup_noise = True
        if not operator_action(
            evidence,
            "physical_board_reset",
            "Keep the flag clear and USB connected. Press and release the board's RESET "
            "button once (NOT BOOT and NOT the external START/STOP button)",
            allow_disconnect=True,
        ):
            raise RuntimeError("Physical board RESET was not confirmed")
        # Do not call Board.open() here: its probe would cause an extra reset.
        new_hello = board.wait_for_hello(15.0)
        board.get_status()
        messages = [parse_line(frame) for frame in evidence.protocol_frames]
        require(
            evidence,
            "recording_reset_not_clean_stop",
            reset_matches(messages, session, hello.boot_id, new_hello.boot_id, hello.device_id)
            and not evidence.sequence_issues
            and not evidence.malformed_after_handshake,
            "one event before physical reset; new hello/boot, same device, clear IDLE, no clean STOP",
            f"old_boot={hello.boot_id}; new_boot={new_hello.boot_id}; "
            f"device={new_hello.device_id}; state={new_hello.state}",
        )
    except (EOFError, KeyboardInterrupt):
        evidence.ambiguous("diagnostic_interrupted", "completed physical reset test", "interrupted")
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        if reset_requested:
            evidence.ambiguous(
                "reset_capture_incomplete",
                "captured physical-reset boundary; no extra host reset/probe",
                detail + "; do not infer a firmware failure from missing reset capture",
            )
        else:
            evidence.add("diagnostic_exception", False, "completed pre-reset trial", detail)
        print(f"Diagnostic stopped: {detail}", flush=True)
    finally:
        if session is not None and board.serial is not None and board.serial.is_open:
            try:
                status = board.get_status()
                if status.state == "RECORDING" and status.session_id == session:
                    summary = board.stop(session, "reset_diagnostic_failure_cleanup")
                    evidence.add(
                        "cleanup_stop", True, "failure cleanup only", summary.model_dump_json()
                    )
                    status = board.get_status()
                if status.state in {"ARMED", "COMPLETE"} and status.session_id == session:
                    board.disarm(session)
            except Exception as exc:
                evidence.add(
                    "cleanup_unverified",
                    False,
                    "confirmed nonrecording state",
                    f"{type(exc).__name__}: {exc}",
                )
        board.close()
        evidence.finalize()
    print(f"Evidence directory: {evidence.output_dir}", flush=True)
    print(f"Recording-reset diagnostic: {'PASS' if evidence.passed else 'NOT PASS'}", flush=True)
    return 0 if evidence.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
