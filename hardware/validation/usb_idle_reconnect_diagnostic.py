"""Physical USB unplug/reconnect in IDLE; no probe, reset, ARM, START, or STOP."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from serial import SerialException
from serial.tools import list_ports

from hardware.validation.checkpoint_02_runner import (
    Board, Evidence, EXPECTED_PID, EXPECTED_VID, default_output,
    operator_action, operator_yes_no, sha256_file, utc_now,
)
from hardware.validation.post_reset_status_diagnostic import attach_without_probe
from hardware.validation.reset_capture_board import reset_serial_connection
from hardware.validation.sensor_single_cycle_diagnostic import require
from flightmill.protocol.models import (
    DeviceMessage, HelloMessage, Message, StatusMessage, parse_line,
)


def matching_ports(usb_serial: str):
    return [
        port for port in list_ports.comports()
        if port.vid == EXPECTED_VID and port.pid == EXPECTED_PID
        and port.serial_number == usb_serial
    ]


def verify_initial_port(port_name: str, usb_serial: str) -> None:
    matches = matching_ports(usb_serial)
    if not usb_serial or len(matches) != 1 or matches[0].device != port_name:
        raise RuntimeError("Initial port is not the unique expected USB serial identity")


def wait_for_reconnect(board: Board, usb_serial: str, timeout_s: float = 300.0) -> None:
    """Attach as soon as USB enumerates, before waiting for the owner's DONE."""
    deadline = time.monotonic() + timeout_s
    attempts = []
    connected = False
    try:
        while time.monotonic() < deadline:
            matches = matching_ports(usb_serial)
            if len(matches) > 1:
                raise RuntimeError("Ambiguous USB serial identity; refusing reconnect")
            if len(matches) == 1:
                board.port_name = matches[0].device
                attempt = {"timestamp_utc": utc_now(), "port": board.port_name}
                attempts.append(attempt)
                try:
                    attach_without_probe(board, connection=reset_serial_connection())
                except (SerialException, OSError) as exc:
                    attempt["error"] = f"{type(exc).__name__}: {exc}"
                    board.close()
                else:
                    connected = True
                    board.allow_startup_noise = True
                    return
            time.sleep(0.05)
        raise TimeoutError("Identified USB device did not reconnect before timeout")
    finally:
        (board.evidence.output_dir / "usb_reconnect.json").write_text(
            json.dumps({
                "usb_serial_number": usb_serial, "attempts": attempts,
                "connected": connected, "probe_or_reset_command": False,
                "capture_before_operator_confirmation": True,
            }, indent=2) + "\n", encoding="utf-8",
        )


def clear_idle(status: StatusMessage, device: str, firmware: str) -> bool:
    return (
        status.device_id == device and status.firmware_version == firmware
        and status.state == "IDLE" and status.session_id is None
        and status.sensor_state == status.event_n == status.dropped_events == 0
    )


def reconnect_matches(messages: list[Message], old_boot: str, device: str, firmware: str) -> bool:
    hellos = [m for m in messages if isinstance(m, HelloMessage)]
    if len(hellos) != 1:
        return False
    hello = hellos[0]
    boundary = messages.index(hello)
    before = [m for m in messages[:boundary] if isinstance(m, DeviceMessage)]
    after = [m for m in messages[boundary:] if isinstance(m, DeviceMessage)]
    statuses = [m for m in after if isinstance(m, StatusMessage)]
    return (
        bool(before) and bool(statuses) and hello.message_seq == 0
        and hello.boot_id != old_boot
        and all(m.boot_id == old_boot for m in before)
        and all(m.boot_id == hello.boot_id for m in after)
        and all(m.device_id == device and m.firmware_version == firmware for m in before + after)
        and all(getattr(m, "state", "IDLE") == "IDLE" for m in before + after)
        and all(getattr(m, "session_id", None) is None for m in messages)
        and all(getattr(m, "event_n", 0) == getattr(m, "dropped_events", 0) == 0 for m in messages)
        and all(getattr(m, "sensor_state", 0) == 0 for m in messages)
        and all(clear_idle(m, device, firmware) for m in statuses)
        and all(m.type in {"get_status", "ack", "status", "heartbeat", "hello", "sensor_state"}
                for m in messages)
    )


def action(evidence: Evidence, name: str, prompt: str, **kwargs) -> None:
    if not operator_action(evidence, name, prompt, **kwargs):
        raise RuntimeError(f"{name} was not confirmed")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True)
    parser.add_argument("--expected-usb-serial", required=True)
    parser.add_argument("--expected-device-id", required=True)
    parser.add_argument("--expected-firmware-version", required=True)
    parser.add_argument("--firmware-bin", type=Path, required=True)
    args = parser.parse_args()
    evidence = Evidence(
        default_output("usb_idle_reconnect_diagnostic").resolve(),
        "usb_idle_reconnect_diagnostic", args.port, args.firmware_bin.resolve(),
    )
    board = Board(args.port, evidence)
    board.device_id = args.expected_device_id
    board.firmware_version = args.expected_firmware_version
    (evidence.output_dir / "diagnostic_scope.json").write_text(
        json.dumps({
            "scope": "Physical USB unplug/reconnect in IDLE; NOT full Checkpoint 02",
            "expected_usb_serial": args.expected_usb_serial,
            "expected_device_id": args.expected_device_id,
            "host_commands": ["get_status"], "probe_or_reset_command": False,
            "source_sha256": {name: sha256_file(Path(__file__).with_name(name)) for name in (
                "usb_idle_reconnect_diagnostic.py", "post_reset_status_diagnostic.py",
                "reset_capture_board.py", "windows_reset_serial.py",
            )},
        }, indent=2) + "\n", encoding="utf-8",
    )
    try:
        verify_initial_port(args.port, args.expected_usb_serial)
        attach_without_probe(board, connection=reset_serial_connection())
        status = board.get_status()
        old_boot = status.boot_id
        require(evidence, "clear_idle_before_unplug",
                clear_idle(status, args.expected_device_id, args.expected_firmware_version),
                "same device/firmware; clear IDLE with no session/counts/drops",
                status.model_dump_json())
        action(evidence, "physical_usb_unplug",
               "Unplug the board's USB cable and leave it disconnected; leave the arm/buttons alone",
               allow_disconnect=True)
        matches = matching_ports(args.expected_usb_serial)
        require(evidence, "usb_identity_disappeared", not matches,
                "identified USB serial absent while unplugged", f"matching_ports={len(matches)}")
        board.close()
        require(evidence, "no_partial_frame_at_unplug", not board._serial_partial,
                "complete frames up to disconnect", f"partial_hex={board._serial_partial.hex()}")
        # Consume the old queued messages before enabling startup-noise handling.
        # Their original frames remain in the evidence and the full final audit.
        while board._pending_messages:
            board.read_message(0)
        print("PASS: identified USB device disappeared", flush=True)
        print("\nACTION: Reconnect the same USB cable. Leave the flag clear and buttons untouched. "
              "The recorder will attach immediately; confirm DONE when prompted.", flush=True)
        # Do not wait for DONE before attaching: the startup hello can already be queued.
        wait_for_reconnect(board, args.expected_usb_serial)
        board.wait_for_hello(15.0)
        board.get_status()
        action(evidence, "physical_usb_reconnect",
               "Confirm the same USB cable is reconnected and the flag remains clear")
        board.get_status()
        messages = [parse_line(frame) for frame in evidence.protocol_frames]
        require(evidence, "idle_usb_reconnect_capture",
                reconnect_matches(messages, old_boot, args.expected_device_id,
                                  args.expected_firmware_version)
                and not evidence.sequence_issues and not evidence.malformed_after_handshake,
                "real new hello; same device/firmware; clear IDLE; no trial or reset commands",
                f"old_boot={old_boot}; new_boot={board.hello.boot_id}; messages={len(messages)}")
        if not operator_yes_no(evidence, "led_idle_after_usb_reconnect",
                               "Are red PWR steady, green READY slowly blinking, "
                               "blue REC and yellow EVENT off?"):
            raise RuntimeError("Reconnected IDLE LED pattern was not confirmed")
    except (EOFError, KeyboardInterrupt):
        evidence.ambiguous("diagnostic_interrupted", "completed physical USB check", "interrupted")
    except Exception as exc:
        evidence.ambiguous("usb_capture_incomplete", "complete physical unplug/reconnect evidence",
                           f"{type(exc).__name__}: {exc}")
        print(f"Diagnostic stopped: {type(exc).__name__}: {exc}", flush=True)
    finally:
        board.close()
        evidence.finalize()
    print(f"Evidence directory: {evidence.output_dir}", flush=True)
    print(f"Idle USB diagnostic: {'PASS' if evidence.passed else 'NOT PASS'}", flush=True)
    return 0 if evidence.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
