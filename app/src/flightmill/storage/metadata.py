"""Validated metadata schema and field/value CSV serialization."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from flightmill.acquisition.calculations import circumference_m
from flightmill.constants import (
    DEFAULT_ARM_RADIUS_M,
    DEFAULT_MIN_EVENT_INTERVAL_US,
    METADATA_SCHEMA_VERSION,
    PROTOCOL_VERSION,
    SUPPORTED_MIN_EVENT_INTERVAL_US,
)


class TrialMetadata(BaseModel):
    """Internal V1 metadata model; serialized externally as field/value CSV."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    metadata_schema_version: int = METADATA_SCHEMA_VERSION
    trial_uuid: UUID
    session_id: UUID
    created_at_local_iso8601: datetime
    started_at_local_iso8601: datetime | None = None
    stopped_at_local_iso8601: datetime | None = None
    actual_duration_s: float | None = Field(default=None, ge=0)
    planned_duration_s: float | None = Field(default=None, gt=0)
    stop_reason: str | None = None
    incomplete: bool = True

    species_code: str = Field(min_length=1)
    species_name: str | None = None
    trial_type: str = Field(min_length=1)
    trial_number: int = Field(gt=0)
    well_id: str = Field(min_length=1)
    individual_id: str | None = None
    attempt: int = Field(gt=0)
    operator: str | None = None
    notes: str | None = None

    arm_radius_m: float = Field(default=DEFAULT_ARM_RADIUS_M, gt=0)
    circumference_m: float
    min_event_interval_us: int = DEFAULT_MIN_EVENT_INTERVAL_US

    device_id: str = Field(min_length=1)
    exact_esp32s3_dev_board_model: str = Field(min_length=1)
    hardware_revision: str | None = None
    firmware_version: str = Field(min_length=1)
    protocol_version: int = PROTOCOL_VERSION
    application_version: str = Field(min_length=1)
    serial_port: str = Field(min_length=1)
    baud_rate: int = Field(gt=0)

    accepted_event_count: int = Field(default=0, ge=0)
    final_dropped_events: int = Field(default=0, ge=0)
    raw_csv_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_contract(self) -> Self:
        if self.metadata_schema_version != METADATA_SCHEMA_VERSION:
            raise ValueError("unsupported metadata_schema_version")
        if self.protocol_version != PROTOCOL_VERSION:
            raise ValueError("unsupported protocol_version")
        if self.trial_uuid != self.session_id:
            raise ValueError("trial_uuid and session_id must match in V1")
        if self.created_at_local_iso8601.utcoffset() is None:
            raise ValueError("created_at_local_iso8601 must include a UTC offset")
        for value in (self.started_at_local_iso8601, self.stopped_at_local_iso8601):
            if value is not None and value.utcoffset() is None:
                raise ValueError("trial timestamps must include a UTC offset")
        expected = circumference_m(self.arm_radius_m)
        if not math.isclose(self.circumference_m, expected, rel_tol=0, abs_tol=1e-12):
            raise ValueError("circumference_m does not match arm_radius_m")
        if self.min_event_interval_us not in SUPPORTED_MIN_EVENT_INTERVAL_US:
            raise ValueError("min_event_interval_us is not enabled for V1 validation")
        if not self.incomplete:
            required = {
                "started_at_local_iso8601": self.started_at_local_iso8601,
                "stopped_at_local_iso8601": self.stopped_at_local_iso8601,
                "actual_duration_s": self.actual_duration_s,
                "stop_reason": self.stop_reason,
                "raw_csv_sha256": self.raw_csv_sha256,
            }
            missing = [name for name, value in required.items() if value is None]
            if missing:
                raise ValueError("complete metadata is missing: " + ", ".join(missing))
        return self

    def field_value_rows(self) -> list[tuple[str, str]]:
        rows: list[tuple[str, str]] = [("field", "value")]
        for field_name in type(self).model_fields:
            value = getattr(self, field_name)
            if value is None:
                rendered = ""
            elif isinstance(value, bool):
                rendered = str(value).lower()
            elif isinstance(value, datetime):
                rendered = value.isoformat()
            else:
                rendered = str(value)
            rows.append((field_name, rendered))
        return rows
