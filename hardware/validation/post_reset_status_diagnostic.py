"""Read status after USB re-enumeration without a probe, upload, or reset command.

A changed boot can establish that a reboot occurred. This cannot replace a
missing reset-boundary hello/capture in the physical reset acceptance test.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import serial

from hardware.validation.checkpoint_02_runner import (
    BAUD_RATE,
    Board,
    Evidence,
    default_output,
    sha256_file,
)


def attach_without_probe(board: Board, *, connection=None) -> None:
    """Use normal application serial settings; do not call Board.open's probe."""
    board.serial = connection if connection is not None else serial.Serial()
    board.serial.port = board.port_name
    board.serial.baudrate = BAUD_RATE
    board.serial.timeout = 0.25
    board.serial.write_timeout = 2.0
    board.serial.dsrdtr = False
    board.serial.rtscts = False
    board.serial.dtr = False
    board.serial.rts = False
    board.serial.open()
    board.serial.dtr = True
    board.evidence.use_port(board.port_name)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True)
    parser.add_argument("--expected-device-id", required=True)
    parser.add_argument("--expected-firmware-version", required=True)
    parser.add_argument("--prior-boot-id", required=True)
    parser.add_argument("--firmware-bin", type=Path, required=True)
    args = parser.parse_args()
    evidence = Evidence(
        default_output("post_reset_status_diagnostic").resolve(),
        "post_reset_status_diagnostic",
        args.port,
        args.firmware_bin.resolve(),
    )
    board = Board(args.port, evidence)
    # These are the already verified expected identity, not fresh observations.
    board.device_id = args.expected_device_id
    board.firmware_version = args.expected_firmware_version
    (evidence.output_dir / "diagnostic_scope.json").write_text(
        json.dumps(
            {
                "scope": "Post-attempt status only; NOT complete recording-reset acceptance",
                "driver_source_sha256": sha256_file(Path(__file__)),
                "prior_boot_id": args.prior_boot_id,
                "expected_device_id": args.expected_device_id,
                "host_commands": ["get_status"],
                "esptool_probe_or_reset_command": False,
                "physical_reset_boundary_hello_required_elsewhere": True,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    try:
        attach_without_probe(board)
        status = board.get_status()
        evidence.add(
            "post_attempt_status",
            status.device_id == args.expected_device_id
            and status.firmware_version == args.expected_firmware_version
            and status.boot_id != args.prior_boot_id
            and status.state == "IDLE"
            and status.session_id is None
            and status.sensor_state == status.event_n == status.dropped_events == 0,
            "same device/firmware, changed boot, clear IDLE with no session/counts/drops",
            status.model_dump_json(),
        )
        print(f"STATUS: {status.model_dump_json()}", flush=True)
    except Exception as exc:
        evidence.add(
            "status_query_failed",
            False,
            "responsive production firmware",
            f"{type(exc).__name__}: {exc}",
        )
        print(f"Status query failed: {type(exc).__name__}: {exc}", flush=True)
    finally:
        board.close()
        evidence.finalize()
    print(f"Evidence directory: {evidence.output_dir}", flush=True)
    print(f"Post-attempt status only: {'PASS' if evidence.passed else 'NOT PASS'}", flush=True)
    return 0 if evidence.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
