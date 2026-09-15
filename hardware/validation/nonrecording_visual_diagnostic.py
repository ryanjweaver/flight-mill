"""Current-image IDLE/ARMED no-counting, unarmed warning, and EVENT visual checks."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from uuid import uuid4

from hardware.validation.checkpoint_02_runner import (
    Board, Evidence, default_output, operator_action, operator_yes_no, sha256_file,
)
from hardware.validation.post_reset_status_diagnostic import attach_without_probe
from hardware.validation.sensor_single_cycle_diagnostic import require, verify_window
from hardware.validation.usb_idle_reconnect_diagnostic import verify_initial_port
from flightmill.protocol.models import (
    DeviceMessage, ErrorMessage, EventMessage, SensorStateMessage, StatusMessage,
    TrialStartedMessage, TrialStoppedMessage, parse_line,
)


def frames(evidence, start=0):
    return [parse_line(frame) for frame in evidence.protocol_frames[start:]]


def nonrecording_matches(messages, state, boot, device, session=None, *, warning=False):
    statuses = [m for m in messages if isinstance(m, StatusMessage)]
    errors = [m for m in messages if isinstance(m, ErrorMessage)]
    permitted = {"get_status", "ack", "status", "heartbeat", "sensor_state"}
    if warning:
        permitted.add("error")
    return (
        bool(statuses) and statuses[-1].sensor_state == 0
        and len(errors) == int(warning)
        and all(m.code == "button_unarmed" and m.request_id is None and m.command is None
                for m in errors)
        and all(m.type in permitted for m in messages)
        and all(m.boot_id == boot and m.device_id == device and m.state == state
                for m in messages if isinstance(m, DeviceMessage))
        and all(getattr(m, "event_n", 0) == getattr(m, "dropped_events", 0) == 0
                for m in messages)
        and all(m.session_id == session for m in messages if hasattr(m, "session_id"))
    )


def cycle_levels(messages):
    return [m.sensor_state for m in messages if isinstance(m, SensorStateMessage)]


def action(evidence, name, prompt):
    if not operator_action(evidence, name, prompt):
        raise RuntimeError(f"{name} was not confirmed")


def observe(evidence, name, prompt):
    if not operator_yes_no(evidence, name, prompt):
        raise RuntimeError(f"{name} was not confirmed")


def check_cycles(board, evidence, state, boot, session=None):
    start = len(evidence.protocol_frames)
    before = board.get_status()
    require(evidence, f"{state.lower()}_clear_before_cycles",
            before.state == state and before.boot_id == boot and before.session_id == session
            and before.sensor_state == before.event_n == before.dropped_events == 0,
            f"clear {state}, zero counts/drops", before.model_dump_json())
    action(evidence, f"{state.lower()}_three_revolutions",
           f"While {state}, rotate the actual arm through THREE slow complete revolutions, "
           "ending clear. Watch yellow EVENT; it should stay off. Leave all buttons untouched")
    board.get_status()
    messages = frames(evidence, start)
    levels = cycle_levels(messages)
    require(evidence, f"no_counting_{state.lower()}",
            nonrecording_matches(messages, state, boot, board.device_id, session)
            and levels.count(1) >= 3 and levels.count(0) >= 3
            and not evidence.sequence_issues and not evidence.malformed_after_handshake,
            f"three confirmed revolutions with observed beam cycles, {state}, no events or counts",
            f"sensor_levels={levels}; messages={len(messages)}")
    print(f"PASS: no_counting_{state.lower()}", flush=True)
    observe(evidence, f"event_off_{state.lower()}",
            f"During all three {state} revolutions, did yellow EVENT stay off and blue REC stay off?")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True)
    parser.add_argument("--expected-usb-serial", required=True)
    parser.add_argument("--expected-device-id", required=True)
    parser.add_argument("--expected-firmware-version", required=True)
    parser.add_argument("--firmware-bin", type=Path, required=True)
    args = parser.parse_args()
    evidence = Evidence(default_output("nonrecording_visual_diagnostic").resolve(),
                        "nonrecording_visual_diagnostic", args.port, args.firmware_bin.resolve())
    board = Board(args.port, evidence)
    board.device_id = args.expected_device_id
    board.firmware_version = args.expected_firmware_version
    session = None
    (evidence.output_dir / "diagnostic_scope.json").write_text(json.dumps({
        "scope": "IDLE/ARMED no-counting, unarmed warning, and EVENT visuals; NOT full Checkpoint 02",
        "driver_sha256": sha256_file(Path(__file__)),
        "helper_sha256": {name: sha256_file(Path(__file__).with_name(name)) for name in (
            "sensor_single_cycle_diagnostic.py", "post_reset_status_diagnostic.py",
            "usb_idle_reconnect_diagnostic.py",
        )},
        "expected_device_id": args.expected_device_id,
        "expected_usb_serial": args.expected_usb_serial,
        "no_probe_reset_or_flash": True, "recording_requires_current_readiness": True,
        "all_events_retained": True,
    }, indent=2) + "\n", encoding="utf-8")
    try:
        verify_initial_port(args.port, args.expected_usb_serial)
        attach_without_probe(board)
        initial = board.get_status()
        boot = initial.boot_id
        require(evidence, "initial_clear_idle",
                initial.device_id == args.expected_device_id
                and initial.firmware_version == args.expected_firmware_version
                and initial.state == "IDLE" and initial.session_id is None
                and initial.sensor_state == initial.event_n == initial.dropped_events == 0,
                "expected device/firmware, clear IDLE with zero counts/drops",
                initial.model_dump_json())
        check_cycles(board, evidence, "IDLE", boot)
        start = len(evidence.protocol_frames)
        action(evidence, "unarmed_short_press",
               "Press/release the external START/STOP button briefly once while IDLE. "
               "Watch yellow EVENT for three quick flashes; do not press RESET or BOOT")
        board.get_status()
        require(evidence, "unarmed_button_rejected",
                nonrecording_matches(frames(evidence, start), "IDLE", boot, board.device_id,
                                     warning=True),
                "exactly one unsolicited button_unarmed error, still IDLE, no events/counts",
                f"messages={len(evidence.protocol_frames) - start}")
        print("PASS: unarmed_button_rejected", flush=True)
        observe(evidence, "unarmed_warning_leds",
                "Did yellow EVENT give three quick flashes, with blue REC off and "
                "green READY continuing its slow blink?")
        action(evidence, "ready_before_armed_check",
               "Stay at the bench with the flag clear and buttons untouched. "
               "Confirm readiness for the ARMED no-counting check; do not rotate yet")
        session = uuid4()
        board.arm(session)
        observe(evidence, "armed_leds",
                "While ARMED, is green READY steady, with blue REC and yellow EVENT off?")
        check_cycles(board, evidence, "ARMED", boot, session)
        board.disarm(session)
        session = None
        action(evidence, "ready_before_event_recording",
               "Stay at the bench with the flag clear and buttons untouched. "
               "Confirm readiness for a short EVENT-light recording; do not rotate yet")
        status = board.get_status()
        require(evidence, "clear_idle_before_event_recording",
                status.boot_id == boot and status.state == "IDLE" and status.session_id is None
                and status.sensor_state == status.event_n == status.dropped_events == 0,
                "same-boot clear IDLE before recording", status.model_dump_json())
        session = uuid4()
        board.arm(session)
        board.start(session)
        verify_window(board, evidence, "recording_clear_baseline", 0, 0, session)
        action(evidence, "one_event_visual_revolution",
               "Rotate the actual arm exactly ONE complete revolution, ending clear. "
               "Watch yellow EVENT for a brief pulse at the flag passage. Leave buttons untouched")
        verify_window(board, evidence, "one_event_visual_hold", 0, 1, session)
        summary = board.stop(session, "event_visual_acceptance")
        require(evidence, "one_event_visual_summary",
                summary.accepted_event_count == 1 and summary.dropped_events == 0,
                "one accepted event, zero drops", summary.model_dump_json())
        board.disarm(session)
        session = None
        print("Recording stopped; returned to IDLE", flush=True)
        observe(evidence, "event_pulse_visible",
                "During that one recorded revolution, did yellow EVENT visibly pulse once "
                "at the flag passage?")
        observe(evidence, "final_idle_leds",
                "Now are red PWR steady, green READY slowly blinking, and blue REC/yellow EVENT off?")
        final = board.get_status()
        messages = frames(evidence)
        require(evidence, "complete_capture_and_final_idle",
                evidence.boot_ids == [boot]
                and all(m.device_id == args.expected_device_id
                        and m.firmware_version == args.expected_firmware_version
                        for m in messages if isinstance(m, DeviceMessage))
                and sum(isinstance(m, EventMessage) for m in messages) == 1
                and sum(isinstance(m, TrialStartedMessage) for m in messages) == 1
                and sum(isinstance(m, TrialStoppedMessage) for m in messages) == 1
                and sum(isinstance(m, ErrorMessage) for m in messages) == 1
                and final.state == "IDLE" and final.session_id is None
                and final.sensor_state == final.event_n == final.dropped_events == 0
                and not evidence.sequence_issues and not evidence.malformed_after_handshake,
                "same boot/identity, exactly one recording and expected warning, final clear IDLE",
                final.model_dump_json())
    except (EOFError, KeyboardInterrupt):
        evidence.ambiguous("diagnostic_interrupted", "complete physical visual checks", "interrupted")
    except Exception as exc:
        evidence.add("diagnostic_exception", False, "complete physical visual checks",
                     f"{type(exc).__name__}: {exc}")
        print(f"Diagnostic stopped: {type(exc).__name__}: {exc}", flush=True)
    finally:
        if session is not None and board.serial is not None and board.serial.is_open:
            try:
                status = board.get_status()
                if status.state == "RECORDING" and status.session_id == session:
                    board.stop(session, "visual_diagnostic_failure_cleanup")
                    status = board.get_status()
                if status.state in {"ARMED", "COMPLETE"} and status.session_id == session:
                    board.disarm(session)
            except Exception as exc:
                evidence.add("cleanup_failed", False, "nonrecording state confirmed", str(exc))
        board.close()
        evidence.finalize()
    print(f"Evidence directory: {evidence.output_dir}", flush=True)
    print(f"Nonrecording/visual diagnostic: {'PASS' if evidence.passed else 'NOT PASS'}", flush=True)
    return 0 if evidence.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
