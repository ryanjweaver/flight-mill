"""Controlled clear/block/clear recording diagnostic, not full acceptance.

Uses the unchanged production protocol and existing continuous-capture helper.
Every unsuccessful check aborts progression and attempts a clean serial stop.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from hardware.validation.checkpoint_02_runner import (
    Board,
    Evidence,
    default_output,
    operator_action,
    operator_yes_no,
    sha256_file,
    validate_event_batch,
)
from flightmill.protocol.models import EventMessage, parse_line


def samples_match(
    samples: list[dict[str, Any]], sensor: int, count: int, boot_id: str, session: UUID
) -> bool:
    """Require sustained telemetry, correct identity/state, and no contrary sample."""
    return sum(item.get("type") == "heartbeat" for item in samples) >= 8 and all(
        item.get("type") in {"status", "heartbeat", "sensor_state", "event"}
        and item.get("boot_id") == boot_id
        and item.get("state") == "RECORDING"
        and item.get("session_id", str(session)) == str(session)
        and item.get("sensor_state", sensor) == sensor
        and item.get("event_n", count) == count
        and item.get("dropped_events", 0) == 0
        for item in samples
    )


def require(evidence: Evidence, name: str, passed: bool, expected: str, observed: str) -> None:
    evidence.add(name, passed, expected, observed)
    if not passed:
        raise RuntimeError(f"{name}: {observed}")


def verify_window(
    board: Board, evidence: Evidence, name: str, sensor: int, count: int, session: UUID
) -> None:
    before = board.get_status()
    samples = [before.model_dump(mode="json")]
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        message = board.read_message(min(0.25, max(0.01, deadline - time.monotonic())))
        if message is not None:
            samples.append(message.model_dump(mode="json"))
    samples.append(board.get_status().model_dump(mode="json"))
    events = [
        item
        for frame in evidence.protocol_frames
        if isinstance(item := parse_line(frame), EventMessage) and item.session_id == session
    ]
    event_ok = not events if count == 0 else validate_event_batch(events, count)[0]
    observed = {
        "first_message_seq": samples[0]["message_seq"],
        "last_message_seq": samples[-1]["message_seq"],
        "samples": samples,
        "trial_event_numbers": [item.event_n for item in events],
        "trial_event_dt_us": [item.dt_us for item in events],
    }
    (evidence.output_dir / f"{name}.json").write_text(
        json.dumps(observed, indent=2) + "\n", encoding="utf-8"
    )
    require(
        evidence,
        name,
        samples_match(samples, sensor, count, before.boot_id, session)
        and event_ok
        and not evidence.sequence_issues
        and not evidence.malformed_after_handshake,
        f"10 s RECORDING sensor={sensor}, count={count}, no extra events or capture issues",
        f"samples={len(samples)}; first_seq={samples[0]['message_seq']}; "
        f"last_seq={samples[-1]['message_seq']}; event_n={[item.event_n for item in events]}; "
        f"detail={name}.json",
    )
    print(f"PASS: {name}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True)
    parser.add_argument("--expected-device-id", required=True)
    parser.add_argument("--firmware-bin", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    output = args.output_dir or default_output("sensor_single_cycle_diagnostic")
    evidence = Evidence(
        output.resolve(), "sensor_single_cycle_diagnostic", args.port, args.firmware_bin.resolve()
    )
    board = Board(args.port, evidence)
    session: UUID | None = None
    completed = False
    (evidence.output_dir / "diagnostic_scope.json").write_text(
        json.dumps(
            {
                "scope": "Single stationary-cardboard cycle; NOT full Checkpoint 02 acceptance",
                "driver_source_sha256": sha256_file(Path(__file__)),
                "expected_device_id": args.expected_device_id,
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
            and hello.state == "IDLE"
            and status.state == "IDLE"
            and status.sensor_state == 0
            and status.event_n == 0,
            "expected device, fresh IDLE boot, clear, count=0",
            status.model_dump_json(),
        )
        session = uuid4()
        board.arm(session)
        board.start(session)
        if not operator_yes_no(
            evidence,
            "recording_leds",
            "With the slot still clear, are green READY and blue REC solid?",
        ):
            raise RuntimeError("Recording LED confirmation was not YES")
        verify_window(board, evidence, "recording_clear_baseline", 0, 0, session)
        if not operator_action(
            evidence,
            "insert_stationary_cardboard",
            "Insert the cardboard ONCE, support it fully blocking the beam, "
            "and leave it there with hands away; do not withdraw it",
        ):
            raise RuntimeError("Cardboard insertion was not confirmed")
        verify_window(board, evidence, "recording_held_blocked", 1, 1, session)
        if not operator_action(
            evidence,
            "withdraw_cardboard",
            "Remove the cardboard ONCE, leave the beam clear and hands away",
        ):
            raise RuntimeError("Cardboard removal was not confirmed")
        verify_window(board, evidence, "recording_clear_after_withdrawal", 0, 1, session)
        completed = True
    except (EOFError, KeyboardInterrupt):
        evidence.ambiguous("diagnostic_interrupted", "complete controlled cycle", "interrupted")
    except Exception as exc:
        evidence.add(
            "diagnostic_exception",
            False,
            "complete controlled cycle",
            f"{type(exc).__name__}: {exc}",
        )
        print(f"Diagnostic stopped: {type(exc).__name__}: {exc}", flush=True)
    finally:
        if session is not None and board.serial is not None and board.serial.is_open:
            try:
                status = board.get_status()
                if status.state == "RECORDING" and status.session_id == session:
                    summary = board.stop(session, "single_cycle_diagnostic")
                    evidence.add(
                        "diagnostic_clean_stop",
                        True,
                        "serial stop summary",
                        summary.model_dump_json(),
                    )
                    if completed:
                        evidence.add(
                            "single_cycle_summary",
                            summary.accepted_event_count == 1 and summary.dropped_events == 0,
                            "exactly one accepted event; no drops",
                            summary.model_dump_json(),
                        )
                    status = board.get_status()
                if status.state in {"ARMED", "COMPLETE"} and status.session_id == session:
                    board.disarm(session)
            except Exception as exc:
                evidence.add(
                    "diagnostic_cleanup",
                    False,
                    "clean stop and disarm",
                    f"{type(exc).__name__}: {exc}",
                )
        board.close()
        evidence.finalize()
    print(f"Evidence directory: {evidence.output_dir}", flush=True)
    print(f"Single-cycle diagnostic only: {'PASS' if evidence.passed else 'NOT PASS'}", flush=True)
    return 0 if evidence.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
