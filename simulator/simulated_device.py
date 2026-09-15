"""Deterministic single-channel Flight Mill V1 device simulator."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar
from uuid import UUID

from flightmill.acquisition.state import ButtonAction, DeviceStateMachine, TrialState
from flightmill.constants import (
    DEFAULT_MIN_EVENT_INTERVAL_US,
    PROTOCOL_NAME,
    PROTOCOL_VERSION,
    SENSOR_CLEAR,
)
from flightmill.protocol.models import (
    AckMessage,
    ArmCommand,
    CommandType,
    DeviceMessage,
    DisarmCommand,
    ErrorMessage,
    EventMessage,
    GetStatusCommand,
    HeartbeatMessage,
    HelloMessage,
    HostCommand,
    PingCommand,
    SelfTestCommand,
    SensorStateMessage,
    SetConfigCommand,
    StartCommand,
    StatusMessage,
    StopCommand,
    TrialArmedMessage,
    TrialStartedMessage,
    TrialStoppedMessage,
    serialize_line,
)


_DeviceT = TypeVar("_DeviceT", bound=DeviceMessage)
SESSION_ID = UUID("20000000-0000-4000-8000-000000000001")


class SimulatedDevice:
    """Protocol-conformant deterministic model, not physical-hardware evidence."""

    def __init__(
        self, *, device_id: str = "SIM-FM-0001", clock: Callable[[], float] | None = None,
        hello_on_discovery: bool = False,
    ) -> None:
        self.device_id = device_id
        self.firmware_version = "sim-0.1.0"
        self.machine = DeviceStateMachine()
        self.sensor_state = SENSOR_CLEAR
        self._boot_number = 0
        # Optional firmware 0.1.5 capability. Legacy scenarios deliberately
        # retain boot-only hello behavior unless a test explicitly enables it.
        self.hello_on_discovery = hello_on_discovery
        self._reset_reason = "power_on"
        self._message_seq = 0
        self._event_n = 0
        self._last_accepted_us: int | None = None
        self._dropped_events = 0
        self._duration_us = 0
        self.min_event_interval_us = DEFAULT_MIN_EVENT_INTERVAL_US
        self._clock = clock
        self._boot_started_at = clock() if clock is not None else 0.0
        self._trial_started_at: float | None = None

    @property
    def trial_elapsed_us(self) -> int:
        self.advance_clock()
        return self._duration_us

    def advance_clock(self) -> None:
        """Advance independent trial time, including silence after the last pulse."""
        if self._clock is not None and self._trial_started_at is not None:
            if self.machine.state in {TrialState.RECORDING, TrialState.STOPPING}:
                elapsed = round((self._clock() - self._trial_started_at) * 1_000_000)
                self._duration_us = max(self._duration_us, elapsed)

    def _uptime_ms(self) -> int:
        if self._clock is None:
            return self._duration_us // 1_000
        return max(0, round((self._clock() - self._boot_started_at) * 1_000))

    @property
    def boot_id(self) -> str:
        return f"sim-boot-{self._boot_number:04d}"

    def boot(self, reset_reason: str = "power_on") -> HelloMessage:
        self._boot_number += 1
        self._reset_reason = reset_reason
        self._message_seq = 0
        self._event_n = 0
        self._last_accepted_us = None
        self._dropped_events = 0
        self._duration_us = 0
        self._trial_started_at = None
        if self._clock is not None:
            self._boot_started_at = self._clock()
        self.machine.reset()
        self.machine.boot_complete()
        return self._emit(HelloMessage, state=self.machine.state, reset_reason=reset_reason)

    def process(self, command: HostCommand) -> list[DeviceMessage]:
        self.advance_clock()
        identity_error = self._identity_error(command)
        if identity_error is not None:
            return [identity_error]
        try:
            if isinstance(command, PingCommand):
                return [self._ack(command.type, command.request_id)]
            if isinstance(command, GetStatusCommand):
                messages: list[DeviceMessage] = []
                if (self.hello_on_discovery and command.device_id == "*"
                        and command.firmware_version == "unknown"):
                    messages.append(self._emit(
                        HelloMessage, state=self.machine.state, reset_reason=self._reset_reason,
                    ))
                return [*messages, self._ack(command.type, command.request_id), self.status()]
            if isinstance(command, SetConfigCommand):
                if self.machine.state != TrialState.IDLE:
                    return [self._state_error(command)]
                self.min_event_interval_us = command.config.min_event_interval_us
                return [self._ack(command.type, command.request_id)]
            if isinstance(command, ArmCommand):
                self.machine.arm(command.session_id)
                self.min_event_interval_us = command.config.min_event_interval_us
                self._reset_trial_counters()
                return [
                    self._ack(command.type, command.request_id),
                    self._emit(
                        TrialArmedMessage,
                        session_id=command.session_id,
                        config=command.config,
                    ),
                ]
            if isinstance(command, StartCommand):
                self._require_session(command.session_id)
                self.machine.start()
                self._reset_trial_counters()
                self._trial_started_at = self._clock() if self._clock is not None else None
                return [
                    self._ack(command.type, command.request_id),
                    self._emit(
                        TrialStartedMessage,
                        session_id=command.session_id,
                        start_source="ui",
                    ),
                ]
            if isinstance(command, StopCommand):
                self._require_session(command.session_id)
                self.machine.request_stop()
                ack = self._ack(command.type, command.request_id)
                stopped = self._finish_stop(command.stop_reason, "ui")
                return [ack, stopped]
            if isinstance(command, DisarmCommand):
                self._require_session(command.session_id)
                self.machine.disarm()
                return [self._ack(command.type, command.request_id)]
            if isinstance(command, SelfTestCommand):
                if self.machine.state not in {TrialState.IDLE, TrialState.ARMED}:
                    return [self._state_error(command)]
                return [
                    self._ack(command.type, command.request_id),
                    self._emit(
                        SensorStateMessage,
                        state=self.machine.state,
                        sensor_state=self.sensor_state,
                    ),
                ]
        except (RuntimeError, ValueError) as exc:
            return [
                self._emit(
                    ErrorMessage,
                    request_id=command.request_id,
                    command=command.type,
                    code="invalid_transition",
                    message=str(exc),
                    state=self.machine.state,
                )
            ]
        raise AssertionError(f"unhandled command: {command.type}")

    def status(self) -> StatusMessage:
        return self._emit(
            StatusMessage,
            state=self.machine.state,
            session_id=self.machine.session_id,
            sensor_state=self.sensor_state,
            event_n=self._event_n,
            dropped_events=self._dropped_events,
            uptime_ms=self._uptime_ms(),
        )

    def heartbeat(self) -> HeartbeatMessage:
        return self._emit(
            HeartbeatMessage,
            state=self.machine.state,
            session_id=self.machine.session_id,
            sensor_state=self.sensor_state,
            event_n=self._event_n,
            dropped_events=self._dropped_events,
            uptime_ms=self._uptime_ms(),
        )

    def emit_event(self, event_us: int) -> EventMessage | None:
        if self.machine.state != TrialState.RECORDING:
            raise RuntimeError("events are emitted only while RECORDING")
        if event_us < 0:
            raise ValueError("event_us must be nonnegative")
        if self._last_accepted_us is not None:
            interval = event_us - self._last_accepted_us
            if interval < 0:
                raise ValueError("event_us must be monotonic")
            if interval < self.min_event_interval_us:
                return None
            dt_us = interval
        else:
            dt_us = 0
        self._last_accepted_us = event_us
        self._duration_us = max(self._duration_us, event_us)
        self._event_n += 1
        assert self.machine.session_id is not None
        return self._emit(
            EventMessage,
            session_id=self.machine.session_id,
            event_n=self._event_n,
            event_us=event_us,
            dt_us=dt_us,
            dropped_events=self._dropped_events,
        )

    def inject_dropped_events(self, count: int) -> None:
        if count < 1:
            raise ValueError("count must be positive")
        self._dropped_events += count

    def physical_button(self, held_ms: int) -> list[DeviceMessage]:
        self.advance_clock()
        outcome = self.machine.physical_button(held_ms)
        if outcome.action == ButtonAction.STARTED:
            self._reset_trial_counters()
            self._trial_started_at = self._clock() if self._clock is not None else None
            assert self.machine.session_id is not None
            return [
                self._emit(
                    TrialStartedMessage,
                    session_id=self.machine.session_id,
                    start_source="physical_button",
                )
            ]
        if outcome.action == ButtonAction.STOP_REQUESTED:
            return [self._finish_stop("physical_button", "physical_button")]
        if outcome.action == ButtonAction.WARNING:
            return [
                self._emit(
                    ErrorMessage,
                    code="button_unarmed",
                    message=outcome.message,
                    state=self.machine.state,
                )
            ]
        return []

    def set_sensor_state(self, blocked: bool, *, event_us: int | None = None) -> list[DeviceMessage]:
        """A blocked (rising) edge is the single-revolution event source in V1."""
        state = int(blocked)
        if state == self.sensor_state:
            return []
        self.sensor_state = state
        messages: list[DeviceMessage] = [self._emit(
            SensorStateMessage, state=self.machine.state, sensor_state=state
        )]
        if blocked and self.machine.state == TrialState.RECORDING:
            event = self.emit_event(self.trial_elapsed_us if event_us is None else event_us)
            if event is not None:
                messages.append(event)
        return messages

    def _finish_stop(
        self,
        reason: str,
        source: str,
    ) -> TrialStoppedMessage:
        assert self.machine.session_id is not None
        session_id = self.machine.session_id
        self.machine.complete()
        return self._emit(
            TrialStoppedMessage,
            session_id=session_id,
            accepted_event_count=self._event_n,
            dropped_events=self._dropped_events,
            duration_us=self._duration_us,
            stop_reason=reason,
            stop_source=source,
        )

    def _reset_trial_counters(self) -> None:
        self._event_n = 0
        self._last_accepted_us = None
        self._dropped_events = 0
        self._duration_us = 0
        self._trial_started_at = None

    def _require_session(self, session_id: UUID) -> None:
        if self.machine.session_id != session_id:
            raise ValueError("session_id does not match the armed trial")

    def _identity_error(self, command: HostCommand) -> ErrorMessage | None:
        is_discovery = isinstance(command, (PingCommand, GetStatusCommand))
        if command.device_id == "*" and not is_discovery:
            return self._emit(
                ErrorMessage,
                request_id=command.request_id,
                command=command.type,
                code="identity_required",
                message="wildcard device identity is allowed only for discovery",
                state=self.machine.state,
            )
        if command.device_id not in {"*", self.device_id}:
            return self._emit(
                ErrorMessage,
                request_id=command.request_id,
                command=command.type,
                code="device_id_mismatch",
                message="command device_id does not match this device",
                state=self.machine.state,
            )
        if command.firmware_version == "unknown" and not is_discovery:
            return self._emit(
                ErrorMessage,
                request_id=command.request_id,
                command=command.type,
                code="identity_required",
                message="firmware identity is required after discovery",
                state=self.machine.state,
            )
        if command.firmware_version not in {"unknown", self.firmware_version}:
            return self._emit(
                ErrorMessage,
                request_id=command.request_id,
                command=command.type,
                code="firmware_version_mismatch",
                message="command firmware_version does not match this device",
                state=self.machine.state,
            )
        return None

    def _state_error(self, command: HostCommand) -> ErrorMessage:
        return self._emit(
            ErrorMessage,
            request_id=command.request_id,
            command=command.type,
            code="invalid_state",
            message=f"{command.type} is not allowed while {self.machine.state}",
            state=self.machine.state,
        )

    def _ack(self, command: CommandType, request_id: UUID) -> AckMessage:
        return self._emit(
            AckMessage,
            request_id=request_id,
            command=command,
            state=self.machine.state,
        )

    def _emit(self, model: Callable[..., _DeviceT], **fields: object) -> _DeviceT:
        self._message_seq += 1
        return model(
            protocol=PROTOCOL_NAME,
            protocol_version=PROTOCOL_VERSION,
            device_id=self.device_id,
            firmware_version=self.firmware_version,
            message_seq=self._message_seq,
            boot_id=self.boot_id,
            **fields,
        )


def command_fields(request_number: int) -> dict[str, object]:
    return {
        "protocol": PROTOCOL_NAME,
        "protocol_version": PROTOCOL_VERSION,
        "device_id": "SIM-FM-0001",
        "firmware_version": "sim-0.1.0",
        "request_id": UUID(f"10000000-0000-4000-8000-{request_number:012d}"),
    }


SCENARIO_NAMES = (
    "normal",
    "zero_event",
    "known_timing",
    "chatter_below_50ms",
    "reported_drops",
    "malformed_line",
    "duplicate_event",
    "skipped_message_sequence",
    "device_reset_during_trial",
    "disconnect_reconnect",
    "physical_button_start",
    "physical_button_stop",
)


def run_scenario(name: str) -> list[bytes | None]:
    """Return serialized frames; `None` is a deliberate transport disconnect."""

    if name not in SCENARIO_NAMES:
        raise KeyError(f"unknown scenario: {name}")
    sim = SimulatedDevice()
    frames: list[bytes | None] = [serialize_line(sim.boot())]
    arm = ArmCommand(session_id=SESSION_ID, **command_fields(1))
    if name == "chatter_below_50ms":
        # Preserve this explicitly named legacy scenario.
        arm.config.min_event_interval_us = 50_000
    arm_messages = sim.process(arm)
    frames.extend(serialize_line(message) for message in arm_messages)

    physical_start = name in {"physical_button_start", "physical_button_stop"}
    if physical_start:
        started = sim.physical_button(100)
    else:
        started = sim.process(StartCommand(session_id=SESSION_ID, **command_fields(2)))
    frames.extend(serialize_line(message) for message in started)

    if name not in {"zero_event"}:
        first = sim.emit_event(0)
        assert first is not None
        frames.append(serialize_line(first))

    if name in {"normal", "known_timing", "physical_button_start", "physical_button_stop"}:
        second = sim.emit_event(1_000_000)
        assert second is not None
        frames.append(serialize_line(second))
    elif name == "chatter_below_50ms":
        assert sim.emit_event(49_999) is None
        boundary = sim.emit_event(50_000)
        assert boundary is not None
        frames.append(serialize_line(boundary))
    elif name == "reported_drops":
        sim.inject_dropped_events(2)
        second = sim.emit_event(1_000_000)
        assert second is not None
        frames.append(serialize_line(second))
    elif name == "malformed_line":
        frames.append(b"{malformed-json}\n")
    elif name == "duplicate_event":
        frames.append(frames[-1])
    elif name == "skipped_message_sequence":
        sim._message_seq += 1
        frames.append(serialize_line(sim.heartbeat()))
    elif name == "device_reset_during_trial":
        frames.append(serialize_line(sim.boot("software_reset")))
        return frames
    elif name == "disconnect_reconnect":
        frames.append(None)
        frames.append(serialize_line(sim.boot("usb_reconnect")))
        return frames

    if name == "physical_button_stop":
        stopped = sim.physical_button(1_500)
    else:
        stopped = sim.process(
            StopCommand(
                session_id=SESSION_ID,
                stop_reason="scenario_complete",
                **command_fields(3),
            )
        )
    frames.extend(serialize_line(message) for message in stopped)
    return frames


if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", choices=SCENARIO_NAMES, default="normal", nargs="?")
    args = parser.parse_args()
    for frame in run_scenario(args.scenario):
        if frame is None:
            print("# DISCONNECT", flush=True)
        else:
            sys.stdout.buffer.write(frame)
            sys.stdout.buffer.flush()
