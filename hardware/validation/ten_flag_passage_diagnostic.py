"""Count all events during ten or twenty operator-confirmed actual revolutions.

Host-only diagnostic using production firmware and the configured interval rule.
Never stop capture at the expected event count: wait for operator completion,
then cleanly stop and compare the entire trial with the confirmed revolutions.
"""

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
    validate_event_batch,
)
from hardware.validation.sensor_single_cycle_diagnostic import require, verify_window
from flightmill.protocol.models import EventMessage, parse_line


EXPECTED_PASSAGES = 10


def assess_events(
    events: list[EventMessage],
    summary_count: int,
    summary_drops: int,
    *,
    expected_passages: int = EXPECTED_PASSAGES,
) -> dict:
    """Compare every captured event without truncating to the expected count."""
    if expected_passages not in {10, 20}:
        raise ValueError("Expected passages must be 10 or 20")
    sequence_ok, detail = validate_event_batch(events, expected_passages)
    timing_ok = all(
        current.event_us - previous.event_us == current.dt_us
        and current.dt_us >= MIN_EVENT_INTERVAL_US
        for previous, current in zip(events, events[1:])
    )
    return {
        "expected_physical_passages": expected_passages,
        "captured_event_messages": len(events),
        "summary_accepted_event_count": summary_count,
        "summary_dropped_events": summary_drops,
        "count_classification": (
            "10 events"
            if summary_count == 10
            else "20 events"
            if summary_count == 20
            else f"other: {summary_count} events"
        ),
        "passed": sequence_ok
        and timing_ok
        and summary_count == expected_passages
        and summary_drops == 0,
        "timing_consistent": timing_ok,
        "sequence_detail": detail,
        "events": [event.model_dump(mode="json") for event in events],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True)
    parser.add_argument("--expected-device-id", required=True)
    parser.add_argument("--firmware-bin", type=Path, required=True)
    parser.add_argument("--passages", type=int, choices=(10, 20), default=EXPECTED_PASSAGES)
    args = parser.parse_args()
    count_word = "ten" if args.passages == 10 else "twenty"
    mode = f"{count_word}_flag_passage_diagnostic"
    evidence = Evidence(
        default_output(mode).resolve(),
        mode,
        args.port,
        args.firmware_bin.resolve(),
    )
    board = Board(args.port, evidence)
    session: UUID | None = None
    (evidence.output_dir / "diagnostic_scope.json").write_text(
        json.dumps(
            {
                "scope": f"{args.passages} actual-arm revolutions with one flag; NOT full Checkpoint 02 acceptance",
                "driver_source_sha256": sha256_file(Path(__file__)),
                "window_helper_source_sha256": sha256_file(
                    Path(__file__).with_name("sensor_single_cycle_diagnostic.py")
                ),
                "expected_device_id": args.expected_device_id,
                "expected_physical_passages": args.passages,
                "min_event_interval_us": MIN_EVENT_INTERVAL_US,
                "stop_condition": "operator completion, NOT reaching the expected event count",
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
            "expected device, fresh clear IDLE, zero events/drops",
            status.model_dump_json(),
        )
        session = uuid4()
        board.arm(session)
        board.start(session)
        verify_window(board, evidence, "recording_clear_baseline", 0, 0, session)
        if not operator_action(
            evidence,
            f"{count_word}_actual_flag_passages",
            f"Rotate the actual flight-mill arm through exactly {args.passages} complete "
            f"revolutions in one direction, with one flag passage per revolution. Pass through without "
            "pausing inside the slot; leave roughly one second between passages. "
            f"Stop with the flag clear after revolution {args.passages}. Do not press START/STOP. "
            "Report any accidental extra revolution instead of confirming the requested count",
        ):
            raise RuntimeError(f"Exactly {args.passages} actual revolutions were not confirmed")
        status = board.get_status()
        summary = board.stop(session, mode)
        evidence.add("diagnostic_clean_stop", True, "clean serial stop", summary.model_dump_json())
        board.disarm(session)
        idle = board.get_status()
        evidence.add(
            "returned_idle", idle.state == "IDLE", "IDLE after disarm", idle.model_dump_json()
        )
        evidence.add(
            f"clear_after_{count_word}_passages",
            status.state == "RECORDING"
            and status.sensor_state == 0
            and status.boot_id == hello.boot_id
            and status.session_id == session,
            "same recording trial, flag clear before stop",
            status.model_dump_json(),
        )
        events = [
            item
            for frame in evidence.protocol_frames
            if isinstance(item := parse_line(frame), EventMessage) and item.session_id == session
        ]
        comparison = assess_events(
            events,
            summary.accepted_event_count,
            summary.dropped_events,
            expected_passages=args.passages,
        )
        evidence.add(
            "same_boot_and_device",
            evidence.boot_ids == [hello.boot_id]
            and summary.boot_id == hello.boot_id
            and all(
                event.boot_id == hello.boot_id and event.device_id == hello.device_id
                for event in events
            ),
            "no reset or different device during trial",
            f"boot_ids={evidence.boot_ids}",
        )
        (evidence.output_dir / "count_comparison.json").write_text(
            json.dumps(comparison, indent=2) + "\n", encoding="utf-8"
        )
        evidence.add(
            f"{count_word}_passages_exact_count",
            comparison["passed"],
            f"{args.passages} actual revolutions, exactly {args.passages} events, first dt=0, no drops",
            f"captured={len(events)}; summary={summary.accepted_event_count}; "
            f"drops={summary.dropped_events}; detail=count_comparison.json",
        )
        print(
            f"OBSERVED: {summary.accepted_event_count} events for {args.passages} confirmed revolutions",
            flush=True,
        )
    except (EOFError, KeyboardInterrupt):
        evidence.ambiguous(
            "diagnostic_interrupted", f"{args.passages} completed revolutions", "interrupted"
        )
    except Exception as exc:
        evidence.add(
            "diagnostic_exception", False, "complete diagnostic", f"{type(exc).__name__}: {exc}"
        )
        print(f"Diagnostic stopped: {type(exc).__name__}: {exc}", flush=True)
    finally:
        if session is not None and board.serial is not None and board.serial.is_open:
            try:
                status = board.get_status()
                if status.state == "RECORDING" and status.session_id == session:
                    summary = board.stop(session, "flag_diagnostic_cleanup")
                    evidence.add("cleanup_stop", True, "clean stop", summary.model_dump_json())
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
    print(
        f"{args.passages}-revolution diagnostic: {'PASS' if evidence.passed else 'NOT PASS'}",
        flush=True,
    )
    return 0 if evidence.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
