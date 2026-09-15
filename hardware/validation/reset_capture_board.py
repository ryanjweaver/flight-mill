"""One expected native-USB reset reconnection during physical acceptance only."""

from __future__ import annotations

import json
import sys
import time

import serial
from serial import SerialException
from serial.tools import list_ports

from hardware.validation.checkpoint_02_runner import (
    EXPECTED_PID,
    EXPECTED_VID,
    Board,
    utc_now,
)
from hardware.validation.post_reset_status_diagnostic import attach_without_probe
from flightmill.protocol.models import HelloMessage, Message


def reset_serial_connection():
    if sys.platform == "win32":
        from hardware.validation.windows_reset_serial import WindowsResetSerial

        return WindowsResetSerial()
    return serial.Serial()


class ResetCaptureBoard(Board):
    """Recover the read handle before the operator answers, not after it.

    Only the explicitly prepared reset phase may reconnect. Require the same
    unique USB serial identity and capture a real hello; never synthesize one
    or invoke Board.open(), whose esptool probe would reset the board again.
    """

    RECOVERY_TIMEOUT_S = 15.0
    RETRY_INTERVAL_S = 0.05

    def __init__(self, port, evidence):
        super().__init__(port, evidence)
        self._reset_recovery_enabled = False
        self._reset_old_boot: str | None = None
        self._reset_usb_serial: str | None = None
        self._reset_reconnect_count = 0

    def enable_reset_recovery(self) -> None:
        ports = [p for p in list_ports.comports() if p.device == self.port_name]
        if (
            len(ports) != 1
            or ports[0].vid != EXPECTED_VID
            or ports[0].pid != EXPECTED_PID
            or not ports[0].serial_number
            or self.hello is None
            or self._reset_reconnect_count
        ):
            raise RuntimeError(
                "Cannot prepare reset recovery without one verified USB serial identity"
            )
        self._reset_usb_serial = ports[0].serial_number
        self._reset_old_boot = self.hello.boot_id
        self._reset_recovery_enabled = True

    def _recover_reset_usb(self, error: SerialException) -> None:
        started = time.monotonic()
        deadline = started + self.RECOVERY_TIMEOUT_S
        recovery = {
            "started_at_utc": utc_now(),
            "old_port": self.port_name,
            "usb_serial_number": self._reset_usb_serial,
            "prior_boot_id": self._reset_old_boot,
            "read_error": str(error),
            "partial_frame_hex": self._serial_partial.hex(),
            "probe_or_reset_command": False,
            "open_attempts": 0,
            "open_errors": [],
            "reconnected": False,
            "preserve_windows_input_queue": sys.platform == "win32",
        }
        try:
            self.close()
            if self._serial_partial:
                self.evidence.ambiguous(
                    "partial_frame_at_reset",
                    "complete serial frames across reset",
                    "partial bytes retained in raw capture and usb_reset_recovery.json",
                )
                self._serial_partial.clear()
            # Do not clear queued messages or any captured frames from the old boot.
            while time.monotonic() < deadline:
                matches = [
                    p
                    for p in list_ports.comports()
                    if p.vid == EXPECTED_VID
                    and p.pid == EXPECTED_PID
                    and p.serial_number == self._reset_usb_serial
                ]
                if len(matches) > 1:
                    raise RuntimeError("Ambiguous USB identity after reset; refusing to attach")
                if len(matches) == 1:
                    self.port_name = matches[0].device
                    recovery["open_attempts"] += 1
                    try:
                        attach_without_probe(self, connection=reset_serial_connection())
                    except (SerialException, OSError) as exc:
                        recovery["open_errors"].append(str(exc))
                        self.close()
                    else:
                        self._reset_reconnect_count += 1
                        self.allow_startup_noise = True
                        recovery["new_port"] = self.port_name
                        recovery["reconnected"] = True
                        self.evidence.add(
                            "reset_usb_handle_recovered",
                            True,
                            "same unique USB identity; preserve Windows input; no probe/reset command",
                            f"port={self.port_name}; usb_serial={self._reset_usb_serial}",
                        )
                        return
                time.sleep(self.RETRY_INTERVAL_S)
            raise TimeoutError(
                "USB reset recovery timed out without reopening the identified board"
            )
        finally:
            recovery["elapsed_s"] = time.monotonic() - started
            recovery["ended_at_utc"] = utc_now()
            (self.evidence.output_dir / "usb_reset_recovery.json").write_text(
                json.dumps(recovery, indent=2) + "\n", encoding="utf-8"
            )

    def read_live_message(self, timeout_s: float, *, conforming: bool = True) -> Message | None:
        try:
            message = super().read_live_message(timeout_s, conforming=conforming)
        except SerialException as exc:
            if not self._reset_recovery_enabled or self._reset_reconnect_count:
                raise
            self._recover_reset_usb(exc)
            message = super().read_live_message(timeout_s, conforming=conforming)
        if isinstance(message, HelloMessage) and message.boot_id != self._reset_old_boot:
            self._reset_recovery_enabled = False
        return message
