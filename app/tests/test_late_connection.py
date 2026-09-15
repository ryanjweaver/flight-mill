"""Software-only 0.1.5 discovery models through the real serial worker.

No OS port is opened, UI driven, hardware reset, or trial recorded. The device
boots before attachment; its bounded transmit queue has already lost that hello.
"""
from __future__ import annotations

from collections import deque
from uuid import UUID

import pytest

from flightmill.acquisition.state import TrialState
from flightmill.protocol.models import (
    AckMessage,
    ArmCommand,
    ErrorMessage,
    GetStatusCommand,
    HelloMessage,
    PingCommand,
    SetConfigCommand,
    StartCommand,
    StatusMessage,
    StopCommand,
    serialize_line,
)
from simulator.simulated_device import SESSION_ID, SimulatedDevice, command_fields
from test_serial_acquisition import Clock, Endpoint, make_service, pump


def fields(device, request=1):
    return {
        **command_fields(request),
        "device_id": device.device_id,
        "firmware_version": device.firmware_version,
    }


def discovery(device, request=1):
    return GetStatusCommand(**{
        **fields(device, request), "device_id": "*", "firmware_version": "unknown",
    })


class PoweredEndpoint(Endpoint):
    """An already running board model; opening its injected handle never boots it."""

    def __init__(self, clock, *, chunk_size=64, delay_seconds=300):
        super().__init__(clock, fresh=False)
        self.clock = clock
        self.device = SimulatedDevice(
            device_id="FM-MOCK-SOFTWARE-ONLY", clock=clock, hello_on_discovery=True,
        )
        self.device.firmware_version = "0.1.5-dev"
        self.initial_hello = self.device.boot("power_on")
        self.chunk_size = chunk_size
        self.open_count = 0
        self.responses = []
        self.detached_backlog(delay_seconds)

    def detached_backlog(self, seconds):
        # Match the finite TX queue's overwrite policy after minutes detached.
        # A cut within the oldest remaining line is normal on late attachment.
        fifo = deque(maxlen=8192)
        for _ in range(seconds):
            self.clock.advance(1)
            fifo.extend(serialize_line(self.device.heartbeat()))
        queued = bytes(fifo)
        if queued.startswith(b"{"):
            queued = queued[17:]
        assert len(queued) <= 8192 and not queued.startswith(b"{")
        assert b'"type":"hello"' not in queued
        self.feed(queued)

    def open(self):
        self.closed = False
        self.open_count += 1
        self.opened.set()

    def read(self, size):
        return super().read(min(size, self.chunk_size))

    def emit(self, *messages):
        self.responses.extend(messages)
        super().emit(*messages)


@pytest.mark.parametrize("chunk_size", [1, 7, 64, 256])
def test_ordinary_connect_after_minutes_and_reopen_discovers_same_boot(tmp_path, chunk_size):
    clock = Clock()
    endpoint = PoweredEndpoint(clock, chunk_size=chunk_size)
    service = make_service(tmp_path, endpoint, clock)
    try:
        service.action("select_source", {"source": "serial", "port": "MOCK9"})
        for attempt in (1, 2):
            if attempt == 2:
                service.action("disconnect")
                assert endpoint.closed
                endpoint.detached_backlog(600)
            service.action("connect")
            state = pump(service, lambda value: value["ready"], timeout=4)
            assert state["state"] == "IDLE"
            assert state["device"]["boot_id"] == endpoint.initial_hello.boot_id
            assert state["device"]["firmware_version"] == "0.1.5-dev"
            assert not state["warnings"]
            assert endpoint.device._boot_number == 1
            assert endpoint.open_count == attempt
            assert len(endpoint.writes) == attempt
            assert all(command.type == "get_status" and command.device_id == "*"
                       and command.firmware_version == "unknown" for command in endpoint.writes)
            hello, ack, status = endpoint.responses[-3:]
            assert isinstance(hello, HelloMessage)
            assert isinstance(ack, AckMessage) and ack.request_id == endpoint.writes[-1].request_id
            assert isinstance(status, StatusMessage) and status.uptime_ms >= 300_000
            assert hello.message_seq > endpoint.initial_hello.message_seq
            assert hello.boot_id == ack.boot_id == status.boot_id == endpoint.initial_hello.boot_id
            # Suppressed pre-identity framing noise is still retained as raw diagnostics.
            assert any(direction == "in" for direction, _ in service._prelog)
        assert not list(tmp_path.glob("*_journal.json"))
        assert not list(tmp_path.glob("*.partial.csv"))
    finally:
        service.close()


@pytest.mark.parametrize("fault", [
    "missing_hello", "missing_ack", "wrong_request", "wrong_command", "nonidle_ack",
    "missing_status", "status_before_ack", "status_sequence_gap", "stale_session", "changed_boot",
])
def test_late_discovery_still_requires_genuine_identity_and_correlated_idle(tmp_path, fault):
    clock = Clock()
    endpoint = PoweredEndpoint(clock)

    def corrupt(messages):
        hello, ack, status = messages
        if fault == "missing_hello":
            return [ack, status]
        if fault == "missing_ack":
            return [hello, status]
        if fault == "wrong_request":
            ack = ack.model_copy(update={"request_id": UUID(int=99)})
        elif fault == "wrong_command":
            ack = ack.model_copy(update={"command": "ping"})
        elif fault == "nonidle_ack":
            ack = ack.model_copy(update={"state": TrialState.RECORDING})
        elif fault == "missing_status":
            return [hello, ack]
        elif fault == "status_before_ack":
            return [hello, status, ack]
        elif fault == "status_sequence_gap":
            status = status.model_copy(update={"message_seq": status.message_seq + 1})
        elif fault == "stale_session":
            status = status.model_copy(update={"session_id": SESSION_ID})
        elif fault == "changed_boot":
            status = status.model_copy(update={"boot_id": "different-boot"})
        return [hello, ack, status]

    endpoint.response_filter = corrupt
    service = make_service(tmp_path, endpoint, clock)
    try:
        service.action("connect")
        pump(service, lambda value: bool(endpoint.responses) and not endpoint.in_waiting)
        service.tick()
        assert not service.snapshot()["ready"]
        clock.advance(6)
        service.tick()
        state = service.snapshot()
        assert not state["ready"] and not state["connected"]
        with pytest.raises(ValueError):
            service.action("arm")
        assert [command.type for command in endpoint.writes] == ["get_status"]
        assert not list(tmp_path.glob("*_journal.json"))
    finally:
        service.close()


def put_device_in_state(device, state):
    if state in {TrialState.ARMED, TrialState.RECORDING, TrialState.STOPPING, TrialState.COMPLETE}:
        device.process(ArmCommand(session_id=SESSION_ID, **fields(device, 2)))
    if state in {TrialState.RECORDING, TrialState.STOPPING, TrialState.COMPLETE}:
        device.process(StartCommand(session_id=SESSION_ID, **fields(device, 3)))
        device.emit_event(0)
        device.emit_event(200_000)
        device.inject_dropped_events(3)
    if state == TrialState.STOPPING:
        device.machine.request_stop()
    if state == TrialState.COMPLETE:
        device.process(StopCommand(session_id=SESSION_ID, **fields(device, 4)))
    if state == TrialState.ERROR:
        device.machine.fail()


@pytest.mark.parametrize("device_state", [
    TrialState.ARMED, TrialState.RECORDING, TrialState.STOPPING, TrialState.COMPLETE,
    TrialState.ERROR,
])
def test_late_connection_does_not_adopt_an_unowned_trial_or_error(tmp_path, device_state):
    clock = Clock()
    endpoint = PoweredEndpoint(clock)
    put_device_in_state(endpoint.device, device_state)
    service = make_service(tmp_path, endpoint, clock)
    try:
        service.action("connect")
        state = pump(service, lambda value: bool(endpoint.responses) and not value["connected"])
        assert not state["ready"]
        assert endpoint.device.machine.state == device_state
        with pytest.raises(ValueError):
            service.action("arm")
        assert [command.type for command in endpoint.writes] == ["get_status"]
        assert not list(tmp_path.glob("*_journal.json"))
        assert not list(tmp_path.glob("*.partial.csv"))
    finally:
        service.close()


@pytest.mark.parametrize("device_state", list(TrialState)[1:])
def test_model_discovery_reports_live_boot_state_without_resetting_trial(device_state):
    clock = Clock()
    device = SimulatedDevice(clock=clock, hello_on_discovery=True)
    initial_hello = device.boot("brownout")
    put_device_in_state(device, device_state)
    device.min_event_interval_us = 50_000
    clock.advance(300)
    device.advance_clock()
    counters = (device._event_n, device._dropped_events, device._duration_us,
                device._last_accepted_us, device.machine.session_id, device.min_event_interval_us)
    previous_seq = device._message_seq
    command = discovery(device, 5)
    hello, ack, status = device.process(command)
    assert isinstance(hello, HelloMessage) and hello.reset_reason == "brownout"
    assert hello.boot_id == ack.boot_id == status.boot_id == initial_hello.boot_id
    assert [message.message_seq for message in (hello, ack, status)] == list(
        range(previous_seq + 1, previous_seq + 4)
    )
    assert hello.state == ack.state == status.state == device_state
    assert ack.request_id == command.request_id
    assert status.session_id == device.machine.session_id and status.uptime_ms == 300_000
    assert counters == (device._event_n, device._dropped_events, device._duration_us,
                        device._last_accepted_us, device.machine.session_id,
                        device.min_event_interval_us)
    assert device._boot_number == 1


@pytest.mark.parametrize("identity", ["exact", "wildcard_exact_firmware", "exact_unknown_firmware"])
def test_model_existing_get_status_queries_have_no_extra_hello(identity):
    device = SimulatedDevice(hello_on_discovery=True)
    device.boot()
    query_fields = fields(device)
    if identity == "wildcard_exact_firmware":
        query_fields["device_id"] = "*"
    elif identity == "exact_unknown_firmware":
        query_fields["firmware_version"] = "unknown"
    assert [message.type for message in device.process(GetStatusCommand(**query_fields))] == [
        "ack", "status",
    ]


def test_model_legacy_firmware_retains_boot_only_hello_behavior():
    device = SimulatedDevice()
    device.boot()
    assert [message.type for message in device.process(discovery(device))] == ["ack", "status"]


def test_model_ping_and_rejected_commands_do_not_emit_discovery_hello():
    device = SimulatedDevice(hello_on_discovery=True)
    device.boot()
    wildcard = {**fields(device), "device_id": "*", "firmware_version": "unknown"}
    assert [message.type for message in device.process(PingCommand(**wildcard))] == ["ack"]
    rejected = [
        GetStatusCommand(**{**wildcard, "device_id": "OTHER"}),
        GetStatusCommand(**{**wildcard, "firmware_version": "wrong"}),
        ArmCommand(session_id=SESSION_ID, **wildcard),
        SetConfigCommand(config={"min_event_interval_us": 50_000}, **wildcard),
    ]
    for command in rejected:
        replies = device.process(command)
        assert len(replies) == 1 and isinstance(replies[0], ErrorMessage)
        assert device.machine.state == TrialState.IDLE and device.machine.session_id is None
    assert device._boot_number == 1
