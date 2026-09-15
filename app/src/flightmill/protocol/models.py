"""Strict executable models for FlightMill Serial Protocol v1."""

from __future__ import annotations

import json
from typing import Annotated, Literal, Self, TypeAlias
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

from flightmill.acquisition.state import TrialState
from flightmill.constants import (
    DEFAULT_MIN_EVENT_INTERVAL_US,
)


CommandType: TypeAlias = Literal[
    "ping",
    "get_status",
    "arm",
    "start",
    "stop",
    "disarm",
    "set_config",
    "self_test",
]


class ProtocolError(ValueError):
    """A serial line was not a valid protocol message."""


class CommonMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    protocol: Literal["flightmill"]
    protocol_version: Literal[1]
    type: str
    device_id: str = Field(min_length=1)
    firmware_version: str = Field(min_length=1)


class AcquisitionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_event_interval_us: Literal[50000, 150000] = DEFAULT_MIN_EVENT_INTERVAL_US


class HostCommand(CommonMessage):
    request_id: UUID


class PingCommand(HostCommand):
    type: Literal["ping"] = "ping"


class GetStatusCommand(HostCommand):
    type: Literal["get_status"] = "get_status"


class ArmCommand(HostCommand):
    type: Literal["arm"] = "arm"
    session_id: UUID
    config: AcquisitionConfig = Field(default_factory=AcquisitionConfig)


class StartCommand(HostCommand):
    type: Literal["start"] = "start"
    session_id: UUID


class StopCommand(HostCommand):
    type: Literal["stop"] = "stop"
    session_id: UUID
    stop_reason: str = Field(default="ui", min_length=1, max_length=64)


class DisarmCommand(HostCommand):
    type: Literal["disarm"] = "disarm"
    session_id: UUID


class SetConfigCommand(HostCommand):
    type: Literal["set_config"] = "set_config"
    config: AcquisitionConfig


class SelfTestCommand(HostCommand):
    type: Literal["self_test"] = "self_test"


class DeviceMessage(CommonMessage):
    message_seq: int = Field(ge=0)
    boot_id: str = Field(min_length=1, max_length=128)


class HelloMessage(DeviceMessage):
    """Current boot identity, emitted at boot or on 0.1.5+ wildcard discovery."""

    type: Literal["hello"] = "hello"
    state: TrialState
    reset_reason: str = Field(min_length=1, max_length=64)


class AckMessage(DeviceMessage):
    type: Literal["ack"] = "ack"
    request_id: UUID
    command: CommandType
    state: TrialState


class ErrorMessage(DeviceMessage):
    type: Literal["error"] = "error"
    request_id: UUID | None = None
    command: CommandType | None = None
    code: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=256)
    state: TrialState


class StatusMessage(DeviceMessage):
    type: Literal["status"] = "status"
    state: TrialState
    session_id: UUID | None = None
    sensor_state: Literal[0, 1]
    event_n: int = Field(ge=0)
    dropped_events: int = Field(ge=0)
    uptime_ms: int = Field(ge=0)


class HeartbeatMessage(DeviceMessage):
    type: Literal["heartbeat"] = "heartbeat"
    state: TrialState
    session_id: UUID | None = None
    sensor_state: Literal[0, 1]
    event_n: int = Field(ge=0)
    dropped_events: int = Field(ge=0)
    uptime_ms: int = Field(ge=0)


class SensorStateMessage(DeviceMessage):
    type: Literal["sensor_state"] = "sensor_state"
    state: TrialState
    sensor_state: Literal[0, 1]


class TrialArmedMessage(DeviceMessage):
    type: Literal["trial_armed"] = "trial_armed"
    state: Literal[TrialState.ARMED] = TrialState.ARMED
    session_id: UUID
    config: AcquisitionConfig


class TrialStartedMessage(DeviceMessage):
    type: Literal["trial_started"] = "trial_started"
    state: Literal[TrialState.RECORDING] = TrialState.RECORDING
    session_id: UUID
    start_source: Literal["ui", "physical_button"]


class EventMessage(DeviceMessage):
    type: Literal["event"] = "event"
    state: Literal[TrialState.RECORDING] = TrialState.RECORDING
    session_id: UUID
    event_n: int = Field(gt=0)
    event_us: int = Field(ge=0)
    dt_us: int = Field(ge=0)
    dropped_events: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_event_timing(self) -> Self:
        if self.event_n == 1 and self.dt_us != 0:
            raise ValueError("the first event must have dt_us=0")
        if self.event_n > 1 and self.dt_us == 0:
            raise ValueError("events after the first must have dt_us>0")
        if self.dt_us > self.event_us:
            raise ValueError("dt_us cannot exceed event_us")
        return self


class TrialStoppedMessage(DeviceMessage):
    type: Literal["trial_stopped"] = "trial_stopped"
    state: Literal[TrialState.COMPLETE] = TrialState.COMPLETE
    session_id: UUID
    accepted_event_count: int = Field(ge=0)
    dropped_events: int = Field(ge=0)
    duration_us: int = Field(ge=0)
    stop_reason: str = Field(min_length=1, max_length=64)
    stop_source: Literal["ui", "physical_button", "automatic"]


Message: TypeAlias = Annotated[
    PingCommand
    | GetStatusCommand
    | ArmCommand
    | StartCommand
    | StopCommand
    | DisarmCommand
    | SetConfigCommand
    | SelfTestCommand
    | HelloMessage
    | AckMessage
    | ErrorMessage
    | StatusMessage
    | HeartbeatMessage
    | SensorStateMessage
    | TrialArmedMessage
    | TrialStartedMessage
    | EventMessage
    | TrialStoppedMessage,
    Field(discriminator="type"),
]

MESSAGE_ADAPTER = TypeAdapter(Message)


def parse_line(raw: bytes | str) -> Message:
    """Parse one strict UTF-8 JSON line without silently repairing it."""

    if isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ProtocolError("serial line is not valid UTF-8") from exc
    else:
        text = raw
    text = text.rstrip("\r\n")
    if not text:
        raise ProtocolError("serial line is empty")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProtocolError("serial line is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ProtocolError("protocol message must be a JSON object")
    try:
        return MESSAGE_ADAPTER.validate_python(payload)
    except ValidationError as exc:
        raise ProtocolError(f"protocol message failed validation: {exc}") from exc


def serialize_line(message: Message) -> bytes:
    """Serialize exactly one newline-terminated UTF-8 JSON message."""

    data = MESSAGE_ADAPTER.dump_json(message, exclude_none=True)
    return data + b"\n"
