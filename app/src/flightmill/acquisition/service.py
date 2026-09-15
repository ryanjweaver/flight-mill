"""One ordered acquisition owner shared by the desktop, browser, and tests."""

from __future__ import annotations

import copy
import logging
import math
import re
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from flightmill.acquisition.calculations import (
    SourceEvent, circumference_m, derive_event, speed_display_gap_limit_us,
)
from flightmill.acquisition.state import TrialState
from flightmill.acquisition.transport import FAULTS, PROFILES, SimulatedTransport
from flightmill.acquisition.serial_transport import SerialTransport, discover_ports, select_port
from flightmill.acquisition.usb_reconnect import USBReconnectWaiter
from flightmill.constants import (
    APPLICATION_VERSION, DEFAULT_MIN_EVENT_INTERVAL_US, PHYSICAL_STOP_HOLD_MS, PROTOCOL_NAME, PROTOCOL_VERSION,
)
from flightmill.protocol.integrity import FindingCode, IntegrityTracker
from flightmill.protocol.models import (
    AcquisitionConfig, AckMessage, ArmCommand, DeviceMessage, DisarmCommand, ErrorMessage, EventMessage,
    GetStatusCommand, HeartbeatMessage, HelloMessage, HostCommand, ProtocolError,
    SelfTestCommand, SensorStateMessage, StartCommand, StatusMessage, StopCommand,
    TrialArmedMessage, TrialStartedMessage, TrialStoppedMessage, parse_line, serialize_line,
)
from flightmill.storage.metadata import TrialMetadata
from flightmill.storage.naming import TrialName, TrialPaths, sanitize_token
from flightmill.storage.trial_writer import TrialWriter, discover_trials


def _now() -> datetime:
    return datetime.now().astimezone()


class AcquisitionService:
    """Thread-safe authoritative service. Call tick regularly from one app worker.

    The 3.5-second heartbeat deadline distinguishes silence in events from lost
    device liveness. Speed staleness is display-only: max(2 s, 2 * last interval).
    Rejected protocol input never rolls the trusted integrity baseline backward.
    """

    def __init__(
        self, output_dir: Path, *, clock: Callable[[], float] = time.monotonic,
        serial_factory: Callable = SerialTransport, port_enumerator: Callable = discover_ports,
    ) -> None:
        self._lock = threading.RLock()
        self._clock = clock
        self.output_dir = Path(output_dir).expanduser().resolve()
        # The GUI starts on USB without enumerating or opening a device. An
        # explicit connection action resolves the port if none was selected.
        self.transport = serial_factory("")
        self.source = "serial"
        self._serial_factory = serial_factory
        self._port_enumerator = port_enumerator
        self._ports: list[dict] = []
        self._connection = "disconnected"
        self._handshake_deadline = 0.0
        self._handshake_trace_count = 0
        self._preidentity_notices: set[str] = set()
        self._usb_reconnect: USBReconnectWaiter | None = None
        self._usb_poll_at = 0.0
        self._discovery_request: UUID | None = None
        self._discovery_ack = False
        self._discovery_ack_sequence: int | None = None
        self._confirmed_interval: int | None = None
        self._emergency: dict | None = None
        self._device_stop_confirmed: bool | None = None
        self._discarding_line = False
        self._state = TrialState.IDLE
        self._identity: dict[str, Any] = {}
        self._sensor_blocked = False
        self._sensor_known = False
        self._tracker = IntegrityTracker()
        self._buffer = bytearray()
        self._pending: dict[str, Any] | None = None
        self._last_heartbeat: float | None = None
        self._started_clock: float | None = None
        self._elapsed_final = 0.0
        self._last_event: EventMessage | None = None
        self._last_speed: float | None = None
        self._speed_after_gap = False
        self._measurement_radius_m = 0.10
        self._event_count = 0
        self._accepted_received_count = 0
        self._row_count = 0
        self._dropped = 0
        self._chart: deque[dict[str, Any]] = deque(maxlen=7200)
        self._warnings: dict[str, str] = {}
        self._diagnostics: deque[dict[str, Any]] = deque(maxlen=250)
        self._prelog: deque[tuple[str, bytes]] = deque(maxlen=200)
        self._integrity_incomplete = False
        self._writer: TrialWriter | None = None
        self._metadata: TrialMetadata | None = None
        self._trial: dict[str, Any] | None = None
        self._notes: list[dict[str, Any]] = []
        self._save_status = "none"
        self._recent_trials: list[dict[str, Any]] = []
        self._last_checkpoint = 0.0
        self._checkpoint_timestamp: str | None = None
        self._durable_row_count = 0
        self._setup: dict[str, Any] = {
            "species_code": "Mrot", "trial_type": "prelim", "trial_number": 1,
            "well_id": "A1", "attempt": 1, "species_name": "", "individual_id": "",
            "operator": "", "notes": "", "arm_radius_cm": 10.0,
            "planned_duration_s": None,
            "interval_mode": "default_150ms",
        }
        self._validation: dict[str, Any] = {}
        self._validate({})
        self._refresh_trials()

    def action(self, action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Validate and execute user intent; invalid actions raise ValueError."""
        with self._lock:
            try:
                return self._action(action, payload)
            except (OSError, RuntimeError) as exc:
                # Commands and manual pulses share the same fatal storage policy as
                # the scheduled acquisition loop. A failed audit write is data loss.
                if self._writer is not None:
                    self._fail(f"Acquisition/storage failure: {exc}", "storage_failure")
                else:
                    # A failed create may already own a partial bundle, before
                    # the writer can be assigned. Surface it in Review immediately.
                    self._refresh_trials()
                raise

    def _action(self, action: str, payload: dict[str, Any] | None) -> dict[str, Any]:
        with self._lock:
            payload = payload or {}
            if action == "disconnect" and self._usb_reconnect is not None:
                # Cancellation takes precedence over a returning port at this tick.
                self._usb_reconnect = None
                self._connection = "disconnected"
            self.tick()
            if self._usb_reconnect is not None and action != "disconnect":
                raise ValueError("USB reconnect is pending; cancel it before another action")
            if action == "discover":
                self._ports = self._port_enumerator()
            elif action == "select_source":
                self._require_editable()
                if self.transport.connected or getattr(self.transport, "opening", False):
                    raise ValueError("Disconnect before selecting an acquisition source")
                source = payload.get("source")
                if source not in {"simulation", "serial"}:
                    raise ValueError("Choose Simulation or USB serial")
                # Build the new selection before publishing it: enumeration,
                # candidate resolution or construction may fail. Keep the
                # previous source and transport paired if selection is rejected.
                ports = self._ports
                if source == "simulation":
                    transport = SimulatedTransport(clock=self._clock)
                else:
                    ports = self._port_enumerator()
                    transport = self._serial_factory(select_port(ports, payload.get("port")))
                self.source = source
                self.transport = transport
                self._ports = ports
                self._identity = {}
                self._confirmed_interval = None
                self._connection = "disconnected"
            elif action == "connect":
                if self._writer is not None:
                    raise ValueError("An active trial already owns the connection")
                if not self.transport.connected:
                    if getattr(self.transport, "opening", False):
                        raise ValueError("Serial port open is still pending")
                    if self.source == "serial" and not self.transport.port:
                        self._ports = self._port_enumerator()
                        self.transport = self._serial_factory(select_port(self._ports))
                    self._begin_connection()
            elif action == "connect_after_replug":
                if (self.source != "serial" or self._writer is not None or self.transport.connected
                        or getattr(self.transport, "opening", False)):
                    raise ValueError("USB reconnect requires a disconnected USB source with no active trial")
                self._ports = self._port_enumerator()
                if not self.transport.port:
                    self.transport = self._serial_factory(select_port(self._ports))
                self._usb_reconnect = USBReconnectWaiter(self._ports, self.transport.port, clock=self._clock)
                self._connection = self._usb_reconnect.phase
                self._usb_poll_at = self._clock() + 0.1
                self._diagnostic("info", f"Waiting for operator USB unplug/replug of {self.transport.port}; port not opened")
            elif action == "disconnect":
                self._usb_reconnect = None
                if self._writer is not None:
                    self._fail("Disconnected during an active trial", "disconnect")
                self.transport.close()
                self._pending = None
                self._connection = "disconnected"
                self._confirmed_interval = None
            elif action == "validate":
                self._require_editable()
                self._validate(payload.get("setup", payload))
            elif action == "arm":
                self._arm(payload.get("setup", payload))
            elif action == "start":
                self._require_state(TrialState.ARMED)
                self._command(StartCommand, "start")
            elif action == "stop":
                self._require_state(TrialState.RECORDING)
                self._stop(str(payload.get("reason", payload.get("stop_reason", "ui"))))
            elif action == "disarm":
                self._require_state(TrialState.ARMED, TrialState.COMPLETE)
                if self._state == TrialState.ARMED and self._writer is not None:
                    self._finish_incomplete("disarmed_before_start")
                self._command(DisarmCommand, "disarm")
            elif action == "self_test":
                self._require_state(TrialState.IDLE, TrialState.ARMED)
                self._command(SelfTestCommand, "self_test", session=False)
            elif action == "configure_simulation":
                self._require_simulation()
                if not self.transport.connected:
                    raise ValueError("Connect the simulator first")
                configuration = {
                    "profile": str(payload.get("profile", self.transport.profile)),
                    "seed": self._integer(payload.get("seed", self.transport.seed), "seed", minimum=0),
                    "rate_hz": float(payload.get("rate_hz", self.transport.rate_hz)),
                }
                self.transport.validate_configuration(**configuration)
                self._log_action(action, {**self._simulation(), **configuration})
                self.transport.configure(**configuration)
            elif action in {"pulse", "sensor", "button", "fault"}:
                self._require_simulation()
                if not self.transport.connected:
                    raise ValueError("Connect the simulator first")
                self._log_action(action, payload)
                if action == "pulse":
                    self.transport.pulse()
                elif action == "sensor":
                    blocked = payload.get("blocked")
                    if blocked is None and payload.get("value") in (0, 1):
                        blocked = bool(payload["value"])
                    if not isinstance(blocked, bool):
                        raise ValueError("Sensor blocked must be true or false")
                    self.transport.sensor(blocked)
                elif action == "button":
                    if self._pending is not None:
                        raise ValueError("Wait for the pending command before pressing the button")
                    held_ms = self._integer(payload.get("held_ms", 100), "held_ms", 0)
                    if self._state == TrialState.RECORDING and held_ms >= PHYSICAL_STOP_HOLD_MS:
                        self._await_external_stop("physical_button")
                    self.transport.button(held_ms)
                else:
                    self.transport.fault(str(payload.get("kind", "")))
                self._drain()
                if not self.transport.connected and self._writer is not None:
                    self._fail(self.transport.loss_reason or "Transport disconnected", "disconnect")
            elif action == "note":
                text = str(payload.get("text", "")).strip()
                if not text or len(text) > 4000:
                    raise ValueError("Enter a note between 1 and 4,000 characters")
                if self._writer is None:
                    raise ValueError("Notes can be added while a trial is armed or recording")
                note = {"time": _now().isoformat(), "elapsed_s": self._elapsed(), "text": text}
                self._log_action("note", note)
                self._notes.append(note)
            else:
                raise ValueError(f"Unknown action: {action}")
            self.tick()
            return self.snapshot()

    def _begin_connection(self) -> None:
        self._buffer.clear()
        self._discarding_line = False
        self._identity = {}
        self._tracker = IntegrityTracker()
        self._pending = None
        self._last_heartbeat = None
        self._sensor_known = False
        self._confirmed_interval = None
        self._discovery_request = None
        self._discovery_ack = False
        self._discovery_ack_sequence = None
        self._prelog.clear()
        self._handshake_trace_count = 0
        self._preidentity_notices.clear()
        self._connection = "opening"
        self._handshake_deadline = self._clock() + 5
        self.transport.open()
        self._drain()
        port = f" on {self.transport.port}" if self.source == "serial" else ""
        self._diagnostic("info", f"Connecting to {self.source}{port}; awaiting compatible identity")

    def _poll_usb_reconnect(self) -> None:
        if self._usb_reconnect is None:
            return
        if self.transport.connected:
            # Retry eligibility ends permanently after a successful open.
            self._usb_reconnect = None
            return
        if self._connection == "opening":
            if self.transport.loss_reason:
                if not self._defer_usb_open():
                    self._fail(self.transport.loss_reason, "connection_failed")
            return
        if self._clock() < self._usb_poll_at:
            return
        self._usb_poll_at = self._clock() + 0.1
        try:
            self._ports = self._port_enumerator()
            port = self._usb_reconnect.poll(self._ports)
            if self._connection != self._usb_reconnect.phase:
                self._connection = self._usb_reconnect.phase
                if self._connection == "waiting_for_replug":
                    self._diagnostic("info", "USB unplug observed; waiting for the same USB device to return")
            if port is not None:
                self.transport = self._serial_factory(port)
                self._diagnostic("info", f"Same USB device available on {port}; port-open attempt {self._usb_reconnect.open_attempts}")
                self._begin_connection()
        except (OSError, RuntimeError, ValueError) as exc:
            self._fail(f"USB reconnect failed: {exc}", "connection_failed")

    def _defer_usb_open(self) -> bool:
        if self._usb_reconnect is None or not getattr(self.transport, "port_not_ready", False):
            return False
        reason = self.transport.loss_reason
        self.transport.close()  # Join and release this failed worker before another attempt.
        if self.transport.loss_reason != reason:
            return False
        self._connection = "waiting_for_port"
        self._usb_poll_at = self._clock() + 0.1
        self._diagnostic("info", f"USB port not ready before handle acquisition; waiting briefly: {reason}")
        return True

    def tick(self) -> None:
        with self._lock:
            try:
                self._poll_usb_reconnect()
                self.transport.tick()
                self._drain()
                if self._emergency and self._clock() >= self._emergency["deadline"]:
                    self._warn("stop_unconfirmed", "Storage failed; device STOP remains unconfirmed")
                    self._emergency = None
                    self.transport.close()
                if self.transport.connected and self._connection == "opening":
                    self._usb_reconnect = None
                    self._connection = "handshaking"
                    self._discovery_request = uuid4()
                    query = GetStatusCommand(
                        protocol=PROTOCOL_NAME, protocol_version=PROTOCOL_VERSION,
                        device_id=self._identity.get("device_id", "*"),
                        firmware_version=self._identity.get("firmware_version", "unknown"),
                        request_id=self._discovery_request,
                    )
                    raw = serialize_line(query)
                    self._log_protocol("out", raw)
                    self.transport.write(raw)
                    self._drain()
                if self._connection in {"opening", "handshaking"}:
                    if self.transport.loss_reason:
                        if not self._defer_usb_open():
                            self._fail(self.transport.loss_reason, "connection_failed")
                    elif self._clock() >= self._handshake_deadline:
                        if not self._identity:
                            reason = ("Device hello was not received; ARM is blocked. "
                                      "Firmware 0.1.5+ supports identity discovery after startup. "
                                      "For older firmware, use Connect after USB reconnect "
                                      "with the instrument idle, or install the firmware update.")
                        else:
                            reason = ("Device hello received, but a fresh correlated get_status "
                                      "reply confirming IDLE was not received; ARM is blocked. "
                                      "Check Diagnostics before reconnecting.")
                        # Warning codes persist across attempts; record each
                        # outcome even when the same code was already reported.
                        target = f"serial on {self.transport.port}" if self.source == "serial" else self.source
                        self._diagnostic("info", f"Handshake timed out for {target}: {reason}")
                        self._fail(reason, "handshake_timeout")
                if self._writer is not None and not self.transport.connected:
                    self._fail(self.transport.loss_reason or "Transport disconnected", "disconnect")
                if self.transport.connected and self._last_heartbeat is not None:
                    if self._clock() - self._last_heartbeat > 3.5:
                        self._warn("heartbeat_loss", "No heartbeat for over 3.5 seconds; device state is uncertain")
                        if self._writer is not None:
                            self._fail("Device heartbeat lost", "heartbeat_loss")
                        else:
                            self.transport.close()
                if self._pending is not None and self._clock() >= self._pending["deadline"]:
                    action = self._pending["action"]
                    if action == "disarm" and not self._pending.get("reconciling"):
                        # Query identity/state once; never repeat a state-changing command.
                        self._pending.update(reconciling=True, deadline=self._clock() + 1)
                        query = GetStatusCommand(
                            protocol=PROTOCOL_NAME, protocol_version=PROTOCOL_VERSION,
                            device_id=self._identity["device_id"],
                            firmware_version=self._identity["firmware_version"], request_id=uuid4(),
                        )
                        raw = serialize_line(query)
                        self._log_protocol("out", raw)
                        self.transport.write(raw)
                        self._drain()
                    else:
                        self._pending = None
                        if self._writer is not None:
                            self._fail(f"Timed out waiting for {action} confirmation", f"{action}_timeout")
                        else:
                            self._warn("command_timeout", f"No confirmed response to {action}; reconnect to reconcile state")
                            self.transport.close()
                if self._state == TrialState.RECORDING and self._metadata is not None:
                    duration = self._metadata.planned_duration_s
                    if duration is not None and self._elapsed() >= duration and self._pending is None:
                        self._stop("planned_duration")
                if self._writer is not None:
                    self._writer.checkpoint()
                    if self._writer.last_checkpoint_at != self._checkpoint_timestamp:
                        self._checkpoint_timestamp = self._writer.last_checkpoint_at
                        self._durable_row_count = self._writer.durable_row_count
                        self._last_checkpoint = self._elapsed()
            except (OSError, RuntimeError, ValueError) as exc:
                self._fail(f"Acquisition/storage failure: {exc}", "storage_failure")

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            elapsed = self._elapsed()
            last = self._last_event
            age = max(0.0, elapsed - last.event_us / 1_000_000) if last else None
            stale = (last is None or age is None or self._speed_after_gap
                     or age > speed_display_gap_limit_us(last.dt_us) / 1_000_000)
            trial = copy.deepcopy(self._trial)
            if trial is not None:
                trial.update(notes=copy.deepcopy(self._notes), duration_s=(elapsed if self._writer is not None
                             else self._metadata.actual_duration_s if self._metadata else trial.get("duration_s")),
                             event_count=self._event_count, row_count=self._row_count)
            return {
                "application_version": APPLICATION_VERSION,
                "min_event_interval_us": (
                    self._metadata.min_event_interval_us if self._metadata
                    else self._requested_interval()
                ),
                "source": self.source, "ports": copy.deepcopy(self._ports),
                "selected_port": getattr(self.transport, "port", None),
                "connection_state": self._connection if self._usb_reconnect is not None or self.transport.connected or
                    getattr(self.transport, "opening", False) else "disconnected",
                "ready": self.transport.connected and self._connection == "ready",
                "device_stop_confirmed": self._device_stop_confirmed,
                "configuration": {"requested_interval_us": self._requested_interval(),
                                  "confirmed_interval_us": self._confirmed_interval,
                                  "radius_cm": self._setup["arm_radius_cm"],
                                  "radius_owner": "host"},
                "connected": self.transport.connected,
                "state": str(self._state) if self.transport.connected else "DISCONNECTED",
                "device": dict(self._identity), "setup": copy.deepcopy(self._setup),
                "filename": self._validation.get("filename", ""),
                "output_dir": str(self.output_dir), "simulation": self._simulation(),
                "metrics": {
                    "elapsed_s": elapsed, "event_count": self._event_count,
                    "row_count": self._row_count,
                    "accepted_received_count": self._accepted_received_count,
                    "distance_m": self._event_count * circumference_m(self._measurement_radius_m),
                    "arm_radius_cm": self._measurement_radius_m * 100,
                    "speed_m_s": None if self._speed_after_gap else self._last_speed,
                    "interval_speed_m_s": self._last_speed,
                    "speed_after_gap": self._speed_after_gap,
                    "last_interval_s": last.dt_us / 1_000_000 if last else None,
                    "last_pulse_age_s": age, "speed_stale": stale,
                    "dropped_events": self._dropped,
                },
                "chart": list(self._chart),
                "warnings": [{"code": code, "message": message} for code, message in self._warnings.items()],
                "diagnostics": list(self._diagnostics), "trial": trial,
                "recent_trials": copy.deepcopy(self._recent_trials),
                "sensor_state": ("blocked" if self._sensor_blocked else "clear") if self._sensor_known else "unknown",
                "sensor_blocked": self._sensor_blocked,
                "pending": None if self._pending is None else {
                    "action": self._pending["action"], "request_id": str(self._pending["request_id"])
                },
                "save_status": self._save_status,
                "validation": copy.deepcopy(self._validation),
                "latest_checkpoint_s": self._last_checkpoint,
                "last_checkpoint_at": self._checkpoint_timestamp,
                "durable_row_count": self._durable_row_count,
            }

    def close(self) -> None:
        with self._lock:
            self._usb_reconnect = None
            try:
                if self._state == TrialState.RECORDING and self._pending is None:
                    self._stop("application_exit")
                if self.source == "serial":
                    deadline = time.monotonic() + 3.2
                    while (self._writer is not None or self._emergency) and self.transport.connected and time.monotonic() < deadline:
                        self.tick()
                        time.sleep(0.01)
                if self._writer is not None:
                    self._finish_incomplete("application_exit_unconfirmed")
            except (OSError, RuntimeError, ValueError) as exc:
                self._fail(f"Shutdown could not save the complete trial: {exc}", "storage_failure")
            finally:
                self.transport.close()

    def _validate(self, payload: dict[str, Any]) -> bool:
        setup = dict(self._setup)
        setup.update({key: value for key, value in payload.items() if key in setup})
        errors: list[str] = []
        sanitized: dict[str, str] = {}
        filename = ""
        try:
            for key in ("species_code", "trial_type", "well_id"):
                setup[key] = str(setup[key] or "").strip()
                if not setup[key]:
                    raise ValueError(f"{key.replace('_', ' ')} is required")
                safe = sanitize_token(setup[key], field=key)
                if safe != setup[key]:
                    sanitized[key] = safe
            for key in ("trial_number", "attempt"):
                setup[key] = self._integer(setup[key], key)
            radius = float(setup["arm_radius_cm"])
            if not math.isfinite(radius) or radius <= 0:
                raise ValueError("Arm radius must be a finite positive number")
            setup["arm_radius_cm"] = radius
            if "min_event_interval_us" in payload:
                if payload["min_event_interval_us"] != 150000 and payload.get("interval_mode") != "legacy_50ms":
                    raise ValueError("Stored 50 ms settings require explicit legacy_50ms selection")
            if setup["interval_mode"] not in {"default_150ms", "legacy_50ms"}:
                raise ValueError("Select default_150ms or explicit legacy_50ms")
            if "min_event_interval_us" in payload:
                expected = 50000 if setup["interval_mode"] == "legacy_50ms" else 150000
                if payload["min_event_interval_us"] != expected:
                    raise ValueError("Numeric interval conflicts with the explicitly selected mode")
            duration = setup.get("planned_duration_s")
            if duration in (None, ""):
                setup["planned_duration_s"] = None
            else:
                duration = float(duration)
                if not math.isfinite(duration) or duration <= 0:
                    raise ValueError("Planned duration must be a finite positive number")
                setup["planned_duration_s"] = duration
            for key in ("species_name", "individual_id", "operator", "notes"):
                setup[key] = str(setup.get(key) or "").strip()
            output = payload.get("output_dir")
            directory = Path(str(output)).expanduser().resolve() if output else self.output_dir
            if directory.exists() and not directory.is_dir():
                raise ValueError("Output location must be a directory")
            name = self._name(setup)
            filename = name.raw_filename
            TrialPaths.build(directory, name).assert_available()
            # The writer checks its companion/journal reservations again exclusively.
            for suffix in ("_simulation.json", "_simulation.partial.json", "_journal.json"):
                if (directory / f"{name.base}{suffix}").exists():
                    raise ValueError("Trial files already exist; change attempt or destination")
            changed_directory = directory != self.output_dir
            self.output_dir = directory
            self._setup = setup
            if changed_directory:
                self._refresh_trials()
        except (ValueError, TypeError, OSError) as exc:
            errors.append(str(exc))
        self._validation = {"valid": not errors, "errors": errors,
                            "sanitized": sanitized, "filename": filename}
        return not errors

    def _arm(self, payload: dict[str, Any]) -> None:
        self._require_state(TrialState.IDLE)
        if not self._validate(payload):
            raise ValueError("; ".join(self._validation["errors"]))
        if self._sensor_blocked and not payload.get("acknowledge_blocked", False):
            raise ValueError("Clear the sensor before arming")
        session = uuid4()
        radius_m = self._setup["arm_radius_cm"] / 100
        fields = {key: value or None for key, value in self._setup.items()
                  if key not in {"arm_radius_cm", "interval_mode"}}
        metadata = TrialMetadata(
            trial_uuid=session, session_id=session, created_at_local_iso8601=_now(),
            arm_radius_m=radius_m, circumference_m=circumference_m(radius_m),
            device_id=self._identity["device_id"], firmware_version=self._identity["firmware_version"],
            exact_esp32s3_dev_board_model=("SIMULATED: no physical development board"
                if self.source == "simulation" else "Model unconfirmed by Protocol v1"),
            application_version=APPLICATION_VERSION,
            serial_port="SIMULATED" if self.source == "simulation" else self.transport.port,
            min_event_interval_us=self._requested_interval(), baud_rate=115200,
            **fields,
        )
        writer = TrialWriter.create(self.output_dir, name=self._name(self._setup),
                                    metadata=metadata,
                                    simulation=self._simulation() if self.source == "simulation" else None,
                                    source=self.source)
        self._writer, self._metadata = writer, metadata
        self._device_stop_confirmed = None
        self._measurement_radius_m = radius_m
        self._warnings.clear()
        self._integrity_incomplete = False
        self._last_event = None
        self._last_speed = None
        self._speed_after_gap = False
        self._event_count = self._row_count = self._dropped = 0
        self._accepted_received_count = 0
        self._elapsed_final = self._last_checkpoint = 0.0
        self._checkpoint_timestamp = writer.last_checkpoint_at
        self._durable_row_count = writer.durable_row_count
        self._started_clock = None
        self._chart.clear()
        self._notes = []
        self._tracker.arm(session)
        self._trial = {"id": str(session), "filename": self._name(self._setup).raw_filename,
                       "acquisition_mode": self.source,
                       "incomplete": True, "stop_reason": None,
                       "files": self._public_files(writer.files())}
        self._save_status = "recording"
        try:
            for direction, raw in self._prelog:
                writer.log_protocol(direction, raw)
            self._log_action("arm", {"setup": self._setup})
            self._confirmed_interval = None
            self._command(ArmCommand, "arm", config=AcquisitionConfig(
                min_event_interval_us=metadata.min_event_interval_us))
        except Exception:
            self._fail("Arming could not be confirmed", "arm_failed")
            raise

    def _command(self, model: type[HostCommand], action: str, *, session: bool = True,
                 **fields: Any) -> None:
        if self._pending is not None:
            raise ValueError(f"Wait for pending {self._pending['action']} confirmation")
        request_id = uuid4()
        if session:
            if self._metadata is None:
                raise ValueError("There is no armed trial")
            fields["session_id"] = self._metadata.session_id
        command = model(
            protocol=PROTOCOL_NAME, protocol_version=PROTOCOL_VERSION,
            device_id=self._identity["device_id"],
            firmware_version=self._identity["firmware_version"], request_id=request_id, **fields,
        )
        self._pending = {"action": action, "request_id": request_id,
                         "deadline": self._clock() + 3.0, "ack": False}
        raw = serialize_line(command)
        self._log_protocol("out", raw)
        self.transport.write(raw)
        self._drain()

    def _stop(self, reason: str) -> None:
        if not reason.strip() or len(reason) > 64:
            raise ValueError("Stop reason must be between 1 and 64 characters")
        self._log_action("stop", {"reason": reason})
        self._state = TrialState.STOPPING
        self._command(StopCommand, "stop", stop_reason=reason)

    def _drain(self) -> None:
        while raw := self.transport.read():
            # Capture chunks before decoding or framing; fragmented/rejected bytes survive.
            self._log_protocol("in", raw)
            if self._discarding_line:
                if b"\n" not in raw:
                    continue
                raw = raw.split(b"\n", 1)[1]
                self._discarding_line = False
            self._buffer.extend(raw)
            if len(self._buffer) > 65536 and b"\n" not in self._buffer:
                self._buffer.clear()
                self._discarding_line = True
                self._warn("oversize_line", "Rejected serial line exceeding 64 KiB", incomplete=True)
                continue
            while b"\n" in self._buffer:
                line, _, rest = self._buffer.partition(b"\n")
                self._buffer = bytearray(rest)
                if len(line) > 65536:
                    self._warn("oversize_line", "Rejected serial line exceeding 64 KiB", incomplete=True)
                    continue
                try:
                    message = parse_line(bytes(line))
                except ProtocolError as exc:
                    detail = str(exc).splitlines()[0][:200]
                    if (self.source == "serial" and not self._identity
                            and self._connection in {"opening", "handshaking"}):
                        # Attachment may start mid-line. Retain the bytes and
                        # diagnostic; no trial data or identity is accepted yet.
                        self._preidentity_notice("framing", f"Pre-discovery serial line rejected: {detail}")
                    else:
                        self._warn("malformed_protocol", detail, incomplete=True)
                    continue
                if not isinstance(message, DeviceMessage):
                    self._warn("unexpected_host_message", "Rejected host command on device input", incomplete=True)
                    continue
                self._receive(message)

    def _receive(self, message: DeviceMessage) -> None:
        if not self._identity:
            if not isinstance(message, HelloMessage):
                self._preidentity_notice("identity", "Discovery awaiting a genuine device hello; pre-identity telemetry is not accepted.")
                return
            if self.source == "serial":
                version = re.fullmatch(r"0\.1\.(\d+)(?:[-+][\w.-]+)?", message.firmware_version)
                if not message.device_id.startswith("FM-") or not version or int(version[1]) < 3:
                    self._fail("Unsupported device identity or firmware; requires FlightMill 0.1.3+ / Protocol 1",
                               "incompatible_device")
                    return
            self._identity = {key: getattr(message, key) for key in (
                "device_id", "firmware_version", "protocol_version", "boot_id"
            )}
            self._last_heartbeat = self._clock()
            self._state = TrialState(message.state)
        elif any(getattr(message, key) != value for key, value in self._identity.items()):
            self._warn("identity_changed", "Device identity or boot changed; the active trial cannot resume", incomplete=True)
            if self._writer is not None:
                self._fail("Device reset or identity changed", "device_reset")
            else:
                self.transport.close()
            return
        if not self._accept(message):
            return
        if isinstance(message, AckMessage):
            if message.request_id == self._discovery_request and message.command == "get_status":
                if message.state != TrialState.IDLE:
                    self._fail("Device is not IDLE; this application cannot adopt an existing trial",
                               "unowned_trial")
                    return
                self._discovery_ack = True
                self._discovery_ack_sequence = message.message_seq
            if (self._pending and message.request_id == self._pending["request_id"]
                    and message.command == self._pending["action"]):
                self._pending["ack"] = True
                if self._pending["action"] == "disarm" and message.state == TrialState.IDLE:
                    self._state = TrialState.IDLE
                    self._pending = None
                    self._metadata = None
                    self._confirmed_interval = None
                elif self._pending["action"] == "self_test":
                    self._pending = None
        elif isinstance(message, ErrorMessage):
            self._warn(message.code, message.message)
            if self._pending and message.request_id == self._pending["request_id"]:
                pending_action = self._pending["action"]
                self._pending = None
                if pending_action in {"arm", "start", "stop"}:
                    self._fail(message.message, "command_rejected")
        elif isinstance(message, (HeartbeatMessage, StatusMessage)):
            self._last_heartbeat = self._clock()
            self._sensor_blocked = bool(message.sensor_state)
            self._sensor_known = True
            if self._connection in {"opening", "handshaking"} and isinstance(message, StatusMessage):
                if self.source == "simulation" or self._discovery_ack:
                    if (self.source == "serial" and self._discovery_ack_sequence is not None
                            and message.message_seq != self._discovery_ack_sequence + 1):
                        self._fail("Discovery status did not immediately follow its correlated ACK; ARM is blocked",
                                   "discovery_status_mismatch")
                        return
                    if message.state != TrialState.IDLE or message.session_id is not None:
                        self._fail("Device owns an existing trial; this application cannot adopt its start",
                                   "unowned_trial")
                        return
                    self._state = TrialState.IDLE
                    self._connection = "ready"
                    if self.source == "serial":
                        self._diagnostic("info", f"USB handshake ready on {self.transport.port}: "
                                         f"{message.device_id}, firmware {message.firmware_version}, "
                                         f"Protocol {message.protocol_version}, boot {message.boot_id}; "
                                         "genuine hello and correlated get_status confirmed IDLE.")
            elif self._writer is None and message.state in {TrialState.ARMED, TrialState.RECORDING, TrialState.STOPPING}:
                self._fail("Unowned device trial detected; no files will be attached", "unowned_trial")
                return
            if self._metadata is not None and message.session_id == self._metadata.session_id:
                self._event_count = max(self._event_count, message.event_n)
                self._observe_drops(message.dropped_events)
            if self._writer is not None and self._state == TrialState.RECORDING:
                if self._metadata is not None and message.session_id != self._metadata.session_id:
                    self._fail("Device session changed during recording", "session_mismatch")
                    return
                if message.state in {TrialState.STOPPING, TrialState.COMPLETE}:
                    self._warn("unconfirmed_stop", "Device stopped; waiting for the matching final summary")
                    self._await_external_stop("device_status")
                elif message.state != TrialState.RECORDING:
                    self._fail("Device state changed without a matching trial transition", "unexpected_device_state")
                    return
            if (isinstance(message, StatusMessage) and self._pending
                    and self._pending["action"] == "disarm" and self._pending.get("reconciling")
                    and message.state == TrialState.IDLE and message.session_id is None):
                self._warn("disarm_reconciled", "Disarm ACK was missing; IDLE state confirmed by status query")
                self._pending = None
                self._state = TrialState.IDLE
                self._metadata = None
                self._confirmed_interval = None
        elif isinstance(message, SensorStateMessage):
            self._sensor_blocked = bool(message.sensor_state)
            self._sensor_known = True
            if self._pending and self._pending["action"] == "self_test":
                self._confirm("self_test")
            if self._pending is None and self._state in {TrialState.IDLE, TrialState.ARMED}:
                self._diagnostic("info", f"Sensor is {'blocked' if self._sensor_blocked else 'clear'}")
        elif isinstance(message, TrialArmedMessage):
            if self._matches_trial(message.session_id) and self._pending and self._pending["action"] == "arm":
                if message.config.min_event_interval_us != self._metadata.min_event_interval_us:
                    self._fail("ARM configuration echo differs from requested interval; recording blocked",
                               "configuration_mismatch")
                    return
                self._confirmed_interval = message.config.min_event_interval_us
                self._confirm("arm")
                self._state = TrialState.ARMED
        elif isinstance(message, TrialStartedMessage):
            if self._matches_trial(message.session_id) and self._state == TrialState.ARMED:
                self._confirm("start")
                self._state = TrialState.RECORDING
                # Delay affects UI receipt, not the actual simulated trial start clock.
                delay = self.transport.device.trial_elapsed_us / 1_000_000 if self.source == "simulation" else 0
                self._started_clock = self._clock() - delay
                self._metadata = self._updated_metadata(started_at_local_iso8601=_now())
                assert self._writer is not None
                self._writer.update_metadata(self._metadata)
                self._log_action("recording_confirmed", {"source": message.start_source})
        elif isinstance(message, EventMessage):
            # Host STOPPING is a bounded receive/drain phase. Firmware drains
            # events captured in the confirmed recording before its STOP ACK
            # and terminal summary; identity/session/timing checks still apply.
            if (self._writer is None or self._started_clock is None
                    or self._state not in {TrialState.RECORDING, TrialState.STOPPING}):
                self._warn("event_outside_recording", "Rejected event outside a confirmed recording", incomplete=True)
                return
            assert self._metadata is not None
            derived = derive_event(SourceEvent(message.event_n, message.event_us,
                                               message.dt_us, message.dropped_events),
                                   self._metadata.arm_radius_m)
            self._accepted_received_count += 1
            self._event_count = max(self._event_count, message.event_n)
            self._writer.append_event(derived)
            self._row_count = self._writer.row_count
            self._event_count = max(self._event_count, message.event_n)
            # Use source intervals, not host receipt/poll timing, so batched USB
            # delivery gives the same display hint. The raw interval stays intact.
            self._speed_after_gap = bool(
                self._last_event is not None
                and message.dt_us > speed_display_gap_limit_us(self._last_event.dt_us)
            )
            self._last_event = message
            self._last_speed = derived.speed_m_s
            self._observe_drops(message.dropped_events)
            self._chart.append({"event_n": derived.event_n, "time_s": derived.time_s,
                                "speed_m_s": None if self._speed_after_gap else derived.speed_m_s,
                                "interval_speed_m_s": derived.speed_m_s,
                                "speed_after_gap": self._speed_after_gap,
                                "dt_s": derived.dt_s,
                                "cumulative_distance_m": derived.cumulative_distance_m})
        elif isinstance(message, TrialStoppedMessage):
            if self._emergency and message.session_id == self._emergency["session_id"]:
                self._device_stop_confirmed = True
                self._diagnostic("warning", "Device STOP confirmed after storage failure; trial remains incomplete")
                self._emergency = None
                self.transport.close()
                return
            if self._matches_trial(message.session_id) and self._state in {TrialState.RECORDING, TrialState.STOPPING}:
                self._confirm("stop")
                self._finish(message)

    def _accept(self, message: DeviceMessage) -> bool:
        candidate = copy.deepcopy(self._tracker)
        findings = candidate.observe(message)
        rejected = {
            FindingCode.DUPLICATE_MESSAGE, FindingCode.NONMONOTONIC_MESSAGE,
            FindingCode.SESSION_MISMATCH, FindingCode.DUPLICATE_EVENT,
            FindingCode.NONMONOTONIC_EVENT, FindingCode.NONMONOTONIC_EVENT_TIME,
            FindingCode.DROPPED_EVENTS_DECREASED,
        }
        for finding in findings:
            self._warn(str(finding), str(finding).replace("_", " ").capitalize(),
                       incomplete=finding in {FindingCode.EVENT_GAP, FindingCode.DROPPED_EVENTS_INCREASED,
                                              FindingCode.DROPPED_EVENTS_DECREASED})
        if any(finding in rejected for finding in findings):
            return False
        if isinstance(message, EventMessage):
            previous = self._last_event
            if previous is None and message.event_n != 1:
                self._warn("event_gap", "Recording begins after missing event data", incomplete=True)
            if previous is not None and message.event_n == previous.event_n + 1:
                if message.dt_us != message.event_us - previous.event_us:
                    self._warn("interval_mismatch", "Event interval disagrees with trusted timestamps; event rejected", incomplete=True)
                    return False
            minimum_interval = (self._metadata.min_event_interval_us if self._metadata
                                else DEFAULT_MIN_EVENT_INTERVAL_US)
            if message.dt_us and message.dt_us < minimum_interval:
                self._warn("interval_below_minimum", f"Event interval below configured {minimum_interval / 1000:g} ms minimum; event rejected", incomplete=True)
                return False
        # Keep the bounded public diagnostics; don't retain every historical finding.
        candidate.findings.clear()
        self._tracker = candidate
        return True

    def _finish(self, summary: TrialStoppedMessage) -> None:
        assert self._writer is not None and self._metadata is not None
        self._device_stop_confirmed = True
        self._elapsed_final = summary.duration_us / 1_000_000
        expected_duration = self._elapsed()
        if self._last_event and summary.duration_us < self._last_event.event_us:
            self._warn("summary_duration", "Final duration precedes recorded event data", incomplete=True)
        if summary.accepted_event_count != self._row_count:
            self._warn("summary_count", "Device accepted count and persisted row count disagree", incomplete=True)
        if summary.accepted_event_count < self._event_count:
            self._warn("summary_count_regressed", "Final device count is below the trusted device count", incomplete=True)
        if abs(expected_duration - self._elapsed_final) > 2:
            self._warn("summary_clock", "Device duration differs from host receipt timing; device terminal duration retained",
                       incomplete=self.source == "simulation")
        self._observe_drops(summary.dropped_events)
        self._event_count = max(self._event_count, summary.accepted_event_count)
        self._started_clock = None
        # Retain the validated terminal observation before any checksum or
        # persistence I/O can fail. This is provisional incomplete metadata;
        # a complete bundle still requires its real checksum and durable commit.
        self._metadata = self._updated_metadata(
            stopped_at_local_iso8601=_now(), actual_duration_s=self._elapsed_final,
            stop_reason=summary.stop_reason, accepted_event_count=summary.accepted_event_count,
            final_dropped_events=summary.dropped_events,
            incomplete=True,
        )
        metadata = self._updated_metadata(
            raw_csv_sha256=self._writer.raw_sha256(), incomplete=self._integrity_incomplete,
        )
        self._metadata = metadata
        self._save_status = "saving"
        if metadata.incomplete:
            files = self._writer.abort(metadata)
            self._save_status = "incomplete"
        else:
            files = self._writer.finalize(metadata)
            self._save_status = "saved"
        self._writer.close()
        self._durable_row_count = self._row_count
        self._checkpoint_timestamp = self._writer.last_checkpoint_at
        self._last_checkpoint = self._elapsed_final
        self._writer = None
        self._state = TrialState.COMPLETE
        assert self._trial is not None
        self._trial.update(incomplete=metadata.incomplete, stop_reason=summary.stop_reason,
                           duration_s=metadata.actual_duration_s,
                           files=self._public_files(files))
        self._refresh_trials()
        self._diagnostic("warning" if metadata.incomplete else "info",
                         "Trial saved with incomplete data" if metadata.incomplete else "Trial saved")

    def _finish_incomplete(self, reason: str) -> None:
        if self._writer is None or self._metadata is None:
            return
        self._elapsed_final = self._elapsed()
        self._started_clock = None
        terminal_known = self._device_stop_confirmed and self._metadata.actual_duration_s is not None
        self._metadata = self._updated_metadata(
            incomplete=True,
            actual_duration_s=self._metadata.actual_duration_s if terminal_known else None,
            stop_reason=reason,
            accepted_event_count=(self._metadata.accepted_event_count
                                  if terminal_known else self._event_count),
            final_dropped_events=self._metadata.final_dropped_events if terminal_known else self._dropped,
        )
        writer = self._writer
        # append_event can write a row before its following checkpoint fails.
        # Written rows remain distinct from the last successful checkpoint.
        self._row_count = writer.row_count
        self._writer = None
        try:
            files = writer.abort(self._metadata)
        except (OSError, RuntimeError, ValueError) as exc:
            files = writer.files()
            self._diagnostic("error", f"Partial-file checkpoint failed; preserve recovery artifacts: {exc}")
        finally:
            writer.close()
        if self._trial is not None:
            self._trial.update(incomplete=True, stop_reason=reason,
                               duration_s=self._metadata.actual_duration_s,
                               files=self._public_files(files))
        self._save_status = "incomplete"
        self._refresh_trials()

    def _fail(self, message: str, reason: str) -> None:
        self._usb_reconnect = None
        previous_state = self._state
        request_stop = (self.source == "serial" and reason == "storage_failure"
                        and self.transport.connected and self._writer is not None
                        and previous_state in {TrialState.RECORDING, TrialState.STOPPING}
                        and not self._device_stop_confirmed)
        self._warn(reason, message, incomplete=True)
        self._pending = None
        self._state = TrialState.ERROR
        self._connection = "error"
        self._confirmed_interval = None
        if not request_stop:
            self.transport.close()
        try:
            self._finish_incomplete(reason)
        except Exception as exc:
            self._save_status = "incomplete"
            self._diagnostic("error", f"Partial-file final checkpoint failed: {exc}")
            if self._trial is not None:
                self._trial.update(incomplete=True, stop_reason=reason)
        if request_stop:
            self._emergency = {"session_id": self._metadata.session_id, "deadline": self._clock() + 3}
            self._device_stop_confirmed = False
            try:
                if previous_state == TrialState.RECORDING:
                    command = StopCommand(protocol=PROTOCOL_NAME, protocol_version=PROTOCOL_VERSION,
                        device_id=self._identity["device_id"], firmware_version=self._identity["firmware_version"],
                        request_id=uuid4(), session_id=self._metadata.session_id, stop_reason="storage_failure")
                    raw = serialize_line(command)
                    self._log_protocol("out", raw)  # Memory audit if storage itself is unusable.
                    self.transport.write(raw)
                self._warn("stop_pending", "Storage unusable; requested STOP, device outcome pending")
            except OSError as exc:
                self._warn("stop_unconfirmed", f"Could not request device STOP: {exc}")
                self._emergency = None
                self.transport.close()

    def _confirm(self, action: str) -> None:
        if self._pending and self._pending["action"] == action:
            if not self._pending["ack"]:
                self._warn("missing_ack", f"{action.capitalize()} state confirmed by device transition despite missing ACK")
            self._pending = None

    def _await_external_stop(self, source: str) -> None:
        """Physical/status stop paths require a terminal summary just like UI STOP."""
        self._state = TrialState.STOPPING
        self._pending = {"action": "stop", "request_id": uuid4(),
                         "deadline": self._clock() + 3.0, "ack": True, "source": source}

    def _matches_trial(self, session_id: UUID) -> bool:
        match = self._metadata is not None and session_id == self._metadata.session_id
        if not match:
            self._warn("session_mismatch", "Rejected message for a different trial session")
        return match

    def _observe_drops(self, dropped: int) -> None:
        if dropped > self._dropped:
            self._warn("dropped_events", "Device reported lost events; data are incomplete", incomplete=True)
        if dropped < self._dropped:
            self._warn("dropped_events_regressed", "Device drop counter moved backward", incomplete=True)
        self._dropped = max(self._dropped, dropped)

    def _updated_metadata(self, **updates: Any) -> TrialMetadata:
        assert self._metadata is not None
        return TrialMetadata.model_validate({**self._metadata.model_dump(), **updates})

    def _elapsed(self) -> float:
        if self._started_clock is not None:
            return max(0.0, self._clock() - self._started_clock)
        return self._elapsed_final

    def _simulation(self) -> dict[str, Any]:
        if self.source != "simulation":
            return {"mode": "serial", "notice": "USB serial acquisition; keep metadata and protocol evidence with CSV."}
        return {"mode": "simulation", "profile": self.transport.profile,
                "seed": self.transport.seed, "rate_hz": self.transport.rate_hz,
                "profiles": list(PROFILES), "faults": list(FAULTS),
                "clock_mode": "real_time_1x" if self._clock is time.monotonic else "injected_test_clock",
                "scenario_version": 1,
                "notice": "Simulated data. Keep simulation and metadata sidecars with raw CSV."}

    def _require_state(self, *states: TrialState) -> None:
        if not self.transport.connected:
            raise ValueError("Connect the selected device first")
        if self._connection != "ready":
            raise ValueError("Wait for compatible identity and IDLE status before control")
        if self._pending is not None:
            raise ValueError(f"Wait for pending {self._pending['action']} confirmation")
        if self._state not in states:
            raise ValueError(f"Action unavailable while {self._state}; expected {', '.join(states)}")

    def _require_editable(self) -> None:
        if self._writer is not None or self._pending is not None or self._emergency is not None or self._state not in {TrialState.IDLE, TrialState.ERROR}:
            raise ValueError("Disarm before changing trial setup")

    def _requested_interval(self) -> int:
        return 50000 if self._setup["interval_mode"] == "legacy_50ms" else 150000

    def _require_simulation(self) -> None:
        if self.source != "simulation":
            raise ValueError("Simulation controls are unavailable for USB serial")

    def _warn(self, code: str, message: str, *, incomplete: bool = False) -> None:
        if code not in self._warnings:
            self._diagnostic("warning", message)
        self._warnings[code] = message
        if incomplete and self._writer is not None:
            self._integrity_incomplete = True

    def _preidentity_notice(self, code: str, message: str) -> None:
        if code not in self._preidentity_notices:
            self._preidentity_notices.add(code)
            self._diagnostic("info", message)

    def _diagnostic(self, level: str, message: str) -> None:
        self._diagnostics.append({"time": _now().isoformat(), "level": level, "message": message})
        logging.getLogger(__name__).log(getattr(logging, level.upper(), logging.INFO), message)

    def _log_protocol(self, direction: str, raw: bytes) -> None:
        if self._writer is not None:
            self._writer.log_protocol(direction, raw)
        else:
            self._prelog.append((direction, raw))
            if (self.source == "serial" and self._connection in {"opening", "handshaking"}
                    and self._handshake_trace_count < 16):
                # Bounded, unverified startup bytes help diagnose missed/truncated hello.
                # Keep original chunks in the protocol prelog; never repair or trust them here.
                self._handshake_trace_count += 1
                clipped = "; truncated to first 1024 bytes" if len(raw) > 1024 else ""
                self._diagnostic("info", f"Serial handshake {direction} ({len(raw)} bytes{clipped}): "
                                 f"{raw[:1024]!r}")

    def _log_action(self, action: str, details: dict[str, Any]) -> None:
        self._diagnostic("info", action.replace("_", " ").capitalize())
        if self._writer is not None:
            self._writer.log_action(action, details, elapsed_s=self._elapsed())

    def _refresh_trials(self) -> None:
        try:
            self._recent_trials = []
            for item in discover_trials(self.output_dir)[:50]:
                metadata = item.get("metadata", {})
                simulation = item.get("simulation", {})
                notes = [action["details"] for action in simulation.get("actions", [])
                         if action.get("action") == "note"]
                # Live metadata begins with count=0 and has no terminal time.
                # Recovery must not present that placeholder as a final device
                # count; validated raw rows remain independently inspectable.
                has_terminal_observation = (
                    metadata.get("actual_duration_s") is not None
                    and bool(metadata.get("stop_reason"))
                )
                self._recent_trials.append({
                    **item, "filename": f"{item['base']}.csv",
                    "event_count": metadata.get("accepted_event_count") if has_terminal_observation else None,
                    "duration_s": metadata.get("actual_duration_s"),
                    "stop_reason": metadata.get("stop_reason") or item.get("reason"),
                    "notes": notes, "files": self._public_files(item["files"]),
                })
        except (OSError, ValueError) as exc:
            self._diagnostic("warning", f"Could not list saved trials: {exc}")

    @staticmethod
    def _name(setup: dict[str, Any]) -> TrialName:
        return TrialName(**{key: setup[key] for key in (
            "species_code", "trial_type", "trial_number", "well_id", "attempt"
        )})

    @staticmethod
    def _integer(value: Any, field: str, minimum: int = 1) -> int:
        try:
            number = float(value)
        except (ValueError, TypeError):
            raise ValueError(f"{field} must be an integer of at least {minimum}") from None
        if isinstance(value, bool) or not math.isfinite(number) or number != int(number) or number < minimum:
            raise ValueError(f"{field} must be an integer of at least {minimum}")
        return int(number)

    @staticmethod
    def _public_files(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{"kind": item["kind"], "name": item["name"]} for item in files]
