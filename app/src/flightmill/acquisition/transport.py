"""Clock-driven simulated serial transport; all telemetry travels as protocol bytes."""

from __future__ import annotations

import math
import random
import time
from collections import deque
from collections.abc import Callable
from typing import Protocol

from flightmill.acquisition.state import TrialState
from flightmill.protocol.models import (
    AckMessage, DeviceMessage, EventMessage, HostCommand, TrialStartedMessage,
    TrialStoppedMessage, parse_line, serialize_line,
)
from simulator.simulated_device import SimulatedDevice

PROFILES = ("steady", "ramp", "flight_rest", "zero", "stress", "manual")
FAULTS = (
    "duplicate", "sequence_gap", "malformed", "fragmented", "drops", "disconnect",
    "reset", "heartbeat_loss", "missing_ack", "delayed_ack", "missing_summary",
    "mismatched_summary", "wrong_session", "event_gap",
)


class Transport(Protocol):
    connected: bool

    def open(self) -> None: ...
    def close(self) -> None: ...
    def write(self, raw: bytes) -> None: ...
    def read(self) -> bytes: ...
    def tick(self) -> None: ...


class SimulatedTransport:
    """Real-time 1x transport with an injectable monotonic clock for fast tests.

    Work is bounded at 10,000 scheduled pulses per tick and 1 MiB of queued wire
    data. An exceeded bound becomes explicit transport loss, never silent loss.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.device = SimulatedDevice(clock=clock)
        self.connected = False
        self.profile = "steady"
        self.seed = 42
        self.rate_hz = 1.0
        self._random = random.Random(self.seed)
        self._queue: deque[bytes] = deque()
        self._queue_size = 0
        self._last_event: bytes | None = None
        self._next_event_us = 1_000_000
        self._next_heartbeat = clock() + 1
        self._heartbeat_enabled = True
        self._next_fault: str | None = None
        self._delayed: tuple[float, list[bytes]] | None = None
        self.loss_reason: str | None = None

    def open(self) -> None:
        self._queue.clear()
        self._queue_size = 0
        self.connected = True
        self.loss_reason = None
        self._heartbeat_enabled = True
        self._next_fault = None
        self._delayed = None
        self._next_heartbeat = self.clock() + 1
        self._emit(self.device.boot("simulated_connect"))
        self._emit(self.device.status())

    def close(self) -> None:
        self.connected = False

    def configure(self, *, profile: str, seed: int, rate_hz: float) -> None:
        self.validate_configuration(profile=profile, seed=seed, rate_hz=rate_hz)
        self.profile, self.seed, self.rate_hz = profile, seed, rate_hz
        self._random = random.Random(seed)
        self._next_event_us = self.device.trial_elapsed_us + self._interval_us(
            self.device.trial_elapsed_us / 1_000_000
        )

    @staticmethod
    def validate_configuration(*, profile: str, seed: int, rate_hz: float) -> None:
        if profile not in PROFILES:
            raise ValueError("Unknown simulation profile")
        if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
            raise ValueError("Simulation seed must be a nonnegative integer")
        if not math.isfinite(rate_hz) or not 0.05 <= rate_hz <= 20:
            raise ValueError("Simulation rate must be between 0.05 and 20 revolutions/s")

    def write(self, raw: bytes) -> None:
        if not self.connected:
            raise ConnectionError("Simulation transport is disconnected")
        command = parse_line(raw)
        if not isinstance(command, HostCommand):
            raise ValueError("Transport accepts host commands only")
        messages = self.device.process(command)
        if any(isinstance(message, TrialStartedMessage) for message in messages):
            self._next_event_us = self._interval_us(0)
        self._deliver_responses(messages)

    def _deliver_responses(self, messages: list[DeviceMessage]) -> None:
        pending = self._next_fault
        if pending in {"missing_ack", "delayed_ack", "missing_summary", "mismatched_summary"}:
            relevant = pending in {"missing_ack", "delayed_ack"} or any(
                isinstance(message, TrialStoppedMessage) for message in messages
            )
            if relevant:
                self._next_fault = None
            else:
                pending = None
        if pending == "delayed_ack":
            self._delayed = (self.clock() + 1.5, [serialize_line(m) for m in messages])
            return
        for message in messages:
            if pending == "missing_ack" and isinstance(message, AckMessage):
                continue
            if isinstance(message, TrialStoppedMessage):
                if pending == "missing_summary":
                    continue
                if pending == "mismatched_summary":
                    message = message.model_copy(update={
                        "accepted_event_count": message.accepted_event_count + 1
                    })
            self._emit(message)

    def read(self) -> bytes:
        if not self._queue:
            return b""
        raw = self._queue.popleft()
        self._queue_size -= len(raw)
        return raw

    def tick(self) -> None:
        if not self.connected:
            return
        self.device.advance_clock()
        if self._delayed is not None:
            due, frames = self._delayed
            if self.clock() < due:
                return
            self._delayed = None
            for frame in frames:
                self._enqueue(frame)
        if self.device.machine.state == TrialState.RECORDING:
            elapsed_us = self.device.trial_elapsed_us
            if self.profile not in {"zero", "manual"}:
                count = 0
                while self._next_event_us <= elapsed_us:
                    count += 1
                    if count > 10_000:
                        self.loss_reason = "Simulation scheduler overrun"
                        self.close()
                        return
                    event_us = self._next_event_us
                    t = event_us / 1_000_000
                    # Synthetic bouts: 8 s of pulses, then 5 s of silence.
                    if self.profile != "flight_rest" or t % 13 < 8:
                        self.pulse(event_us=event_us)
                    self._next_event_us += self._interval_us(t)
        if self._heartbeat_enabled and self.clock() >= self._next_heartbeat:
            self._emit(self.device.heartbeat())
            self._next_heartbeat = self.clock() + 1

    def pulse(self, *, event_us: int | None = None) -> None:
        if not self.connected:
            raise ValueError("Connect the simulator first")
        for blocked in (False, True, False):
            for message in self.device.set_sensor_state(blocked, event_us=event_us):
                self._emit(message)

    def sensor(self, blocked: bool) -> None:
        for message in self.device.set_sensor_state(blocked):
            self._emit(message)

    def button(self, held_ms: int) -> None:
        messages = self.device.physical_button(held_ms)
        if any(isinstance(message, TrialStartedMessage) for message in messages):
            self._next_event_us = self._interval_us(0)
        self._deliver_responses(messages)

    def fault(self, kind: str) -> None:
        if kind not in FAULTS:
            raise ValueError("Unknown simulation fault")
        if kind == "duplicate":
            if self._last_event is None:
                raise ValueError("Record a pulse before injecting a duplicate")
            self._enqueue(self._last_event)
        elif kind == "malformed":
            self._enqueue(b"{malformed-json}\n")
        elif kind == "fragmented":
            raw = serialize_line(self.device.status())
            mid = len(raw) // 2
            self._enqueue(raw[:mid])
            self._enqueue(raw[mid:])
        elif kind == "sequence_gap":
            self.device._message_seq += 1
            self._emit(self.device.heartbeat())
        elif kind == "event_gap":
            self.device._event_n += 1
        elif kind == "wrong_session":
            if self._last_event is None:
                raise ValueError("Record a pulse before injecting a wrong-session event")
            from uuid import uuid4
            event = parse_line(self._last_event).model_copy(update={
                "session_id": uuid4(), "message_seq": self.device._message_seq + 1
            })
            self.device._message_seq += 1
            self._enqueue(serialize_line(event))
        elif kind == "drops":
            self.device.inject_dropped_events(1)
            self._emit(self.device.heartbeat())
        elif kind == "reset":
            self._emit(self.device.boot("simulated_reset"))
        elif kind == "disconnect":
            self.loss_reason = "Injected simulated disconnect"
            self.close()
        elif kind == "heartbeat_loss":
            self._heartbeat_enabled = False
        else:
            self._next_fault = kind

    def _interval_us(self, elapsed_s: float) -> int:
        rate = self.rate_hz
        if self.profile == "stress":
            rate = 20.0
        elif self.profile == "ramp":
            rate *= 0.3 + 1.7 * (0.5 + 0.5 * math.sin(elapsed_s / 5 - math.pi / 2))
            rate *= self._random.uniform(0.98, 1.02)
        return max(50_000, round(1_000_000 / max(rate, 0.05)))

    def _emit(self, message: DeviceMessage) -> None:
        raw = serialize_line(message)
        if isinstance(message, EventMessage):
            self._last_event = raw
        self._enqueue(raw)

    def _enqueue(self, raw: bytes) -> None:
        if self._queue_size + len(raw) > 1_048_576:
            self.loss_reason = "Simulation wire buffer overrun"
            self.close()
            return
        self._queue.append(raw)
        self._queue_size += len(raw)
