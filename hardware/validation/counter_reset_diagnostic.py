"""Two nonzero trials on one uninterrupted connection and boot; no firmware edits."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from uuid import UUID, uuid4

from hardware.validation.checkpoint_02_runner import (
    Board,
    Evidence,
    MIN_EVENT_INTERVAL_US,
    default_output,
    operator_action,
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


def one_event_trial_matches(
    messages: list[Message], session: UUID, boot_id: str, device_id: str
) -> bool:
    starts = [m for m in messages if isinstance(m, TrialStartedMessage) and m.session_id == session]
    events = [m for m in messages if isinstance(m, EventMessage) and m.session_id == session]
    stops = [m for m in messages if isinstance(m, TrialStoppedMessage) and m.session_id == session]
    if len(starts) != 1 or len(events) != 1 or len(stops) != 1:
        return False
    start, event, stop = starts[0], events[0], stops[0]
    return (
        all(
            m.boot_id == boot_id and m.device_id == device_id
            for m in messages
            if isinstance(m, DeviceMessage)
        )
        and not any(m.type == "error" for m in messages)
        and start.start_source == stop.stop_source == "ui"
        and start.message_seq < event.message_seq < stop.message_seq
        and event.event_n == stop.accepted_event_count == 1
        and event.dt_us == event.dropped_events == stop.dropped_events == 0
        and event.event_us <= stop.duration_us
    )


def counter_reset_matches(
    messages: list[Message], sessions: list[UUID], boot_id: str, device_id: str
) -> bool:
    if len(sessions) != 2 or len(set(sessions)) != 2:
        return False
    trials = [
        m for m in messages if isinstance(m, (TrialStartedMessage, EventMessage, TrialStoppedMessage))
    ]
    return (
        len(trials) == 6
        and [m.type for m in trials] == ["trial_started", "event", "trial_stopped"] * 2
        and [m.session_id for m in trials] == [sessions[0]] * 3 + [sessions[1]] * 3
        and all(a.message_seq < b.message_seq for a, b in zip(trials, trials[1:]))
        and all(one_event_trial_matches(messages, session, boot_id, device_id) for session in sessions)
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True)
    parser.add_argument("--expected-device-id", required=True)
    parser.add_argument("--firmware-bin", type=Path, required=True)
    args = parser.parse_args()
    evidence = Evidence(
        default_output("counter_reset_diagnostic").resolve(),
        "counter_reset_diagnostic",
        args.port,
        args.firmware_bin.resolve(),
    )
    board = Board(args.port, evidence)
    sessions = [uuid4(), uuid4()]
    session: UUID | None = None
    (evidence.output_dir / "diagnostic_scope.json").write_text(
        json.dumps(
            {
                "scope": "Two nonzero trials, same boot and connection; NOT full Checkpoint 02 acceptance",
                "driver_source_sha256": sha256_file(Path(__file__)),
                "window_helper_source_sha256": sha256_file(
                    Path(__file__).with_name("sensor_single_cycle_diagnostic.py")
                ),
                "expected_device_id": args.expected_device_id,
                "min_event_interval_us": MIN_EVENT_INTERVAL_US,
                "expected_passages_per_trial": 1,
                "initial_probe_resets_board": True,
                "reopen_or_reset_between_trials": False,
                "stop_condition": "operator completion and clear hold, NOT target event count",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    try:
        # Open only once: the probe resets before both trials, never between them.
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
        for index, session in enumerate(sessions, start=1):
            name = f"trial_{index}"
            board.arm(session)
            started = board.start(session)
            status = board.get_status()
            require(
                evidence,
                f"{name}_starts_at_zero",
                status.state == "RECORDING"
                and started.start_source == "ui"
                and started.boot_id == status.boot_id == hello.boot_id
                and status.session_id == session
                and status.sensor_state == status.event_n == status.dropped_events == 0,
                "same boot, new session, clear RECORDING with zero events/drops",
                status.model_dump_json(),
            )
            verify_window(board, evidence, f"{name}_clear_baseline", 0, 0, session)
            if not operator_action(
                evidence,
                f"{name}_one_revolution",
                f"Trial {index} of 2: rotate the actual arm through exactly ONE complete revolution, "
                "passing the flag through the sensor once without pausing in the slot. "
                "Stop with the flag clear. Do not press START/STOP, reset, or disconnect USB. "
                "Report an accidental extra passage instead of confirming one",
            ):
                raise RuntimeError(f"{name}: exactly one physical revolution was not confirmed")
            verify_window(board, evidence, f"{name}_one_event_clear_hold", 0, 1, session)
            summary = board.stop(session, f"counter_reset_{name}")
            status = board.get_status()
            require(
                evidence,
                f"{name}_complete_nonzero",
                status.state == "COMPLETE"
                and status.session_id == session
                and status.boot_id == hello.boot_id
                and status.sensor_state == 0
                and status.event_n == summary.accepted_event_count == 1
                and status.dropped_events == summary.dropped_events == 0,
                "same boot/session COMPLETE, count one, clear, no drops",
                status.model_dump_json(),
            )
            messages = [parse_line(frame) for frame in evidence.protocol_frames]
            require(
                evidence,
                f"{name}_first_event_and_summary",
                one_event_trial_matches(messages, session, hello.boot_id, hello.device_id),
                "one UI start, event_n=1 dt_us=0, one matching UI stop, same boot/device",
                summary.model_dump_json(),
            )
            board.disarm(session)
            status = board.get_status()
            require(
                evidence,
                f"{name}_returned_idle",
                status.state == "IDLE"
                and status.session_id is None
                and status.boot_id == hello.boot_id
                and status.sensor_state == status.event_n == status.dropped_events == 0,
                "same boot clear IDLE, zero counts/drops",
                status.model_dump_json(),
            )
            print(f"PASS: {name} complete with exactly one event", flush=True)
        messages = [parse_line(frame) for frame in evidence.protocol_frames]
        require(
            evidence,
            "counter_reset_without_reboot",
            counter_reset_matches(messages, sessions, hello.boot_id, hello.device_id)
            and evidence.boot_ids == [hello.boot_id]
            and not evidence.sequence_issues
            and not evidence.malformed_after_handshake,
            "two distinct nonzero trials, each event_n=1 dt_us=0, same boot and uninterrupted capture",
            f"sessions={[str(item) for item in sessions]}; boot_ids={evidence.boot_ids}",
        )
    except (EOFError, KeyboardInterrupt):
        evidence.ambiguous("diagnostic_interrupted", "two completed nonzero trials", "interrupted")
    except Exception as exc:
        evidence.add(
            "diagnostic_exception", False, "two completed nonzero trials", f"{type(exc).__name__}: {exc}"
        )
        print(f"Diagnostic stopped: {type(exc).__name__}: {exc}", flush=True)
    finally:
        if session is not None and board.serial is not None and board.serial.is_open:
            try:
                status = board.get_status()
                if status.state == "RECORDING" and status.session_id == session:
                    summary = board.stop(session, "counter_reset_failure_cleanup")
                    evidence.add("cleanup_stop", True, "failure cleanup only", summary.model_dump_json())
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
    print(f"Counter-reset diagnostic: {'PASS' if evidence.passed else 'NOT PASS'}", flush=True)
    return 0 if evidence.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
