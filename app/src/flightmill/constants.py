"""Frozen product constants shared across host components."""

from __future__ import annotations

from flightmill.hardware import (
    EVENT_LED_GPIO,
    READY_LED_GPIO,
    REC_LED_GPIO,
    SENSOR_INPUT_GPIO as SENSOR_GPIO,
    START_STOP_BUTTON_GPIO,
)

__all__ = [
    "APPLICATION_VERSION", "PROTOCOL_NAME", "PROTOCOL_VERSION", "METADATA_SCHEMA_VERSION",
    "DEFAULT_ARM_RADIUS_M", "DEFAULT_MIN_EVENT_INTERVAL_US", "SUPPORTED_MIN_EVENT_INTERVAL_US",
    "PHYSICAL_STOP_HOLD_MS", "SENSOR_CLEAR", "SENSOR_BLOCKED", "SENSOR_EVENT_EDGE",
    "RAW_CSV_HEADER", "EVENT_LED_GPIO", "READY_LED_GPIO", "REC_LED_GPIO", "SENSOR_GPIO",
    "START_STOP_BUTTON_GPIO",
]

APPLICATION_VERSION = "0.2.0-dev"
PROTOCOL_NAME = "flightmill"
PROTOCOL_VERSION = 1
METADATA_SCHEMA_VERSION = 1

DEFAULT_ARM_RADIUS_M = 0.10
DEFAULT_MIN_EVENT_INTERVAL_US = 150_000
SUPPORTED_MIN_EVENT_INTERVAL_US = frozenset({50_000, DEFAULT_MIN_EVENT_INTERVAL_US})
PHYSICAL_STOP_HOLD_MS = 1_500

SENSOR_CLEAR = 0
SENSOR_BLOCKED = 1
SENSOR_EVENT_EDGE = "RISING"

RAW_CSV_HEADER = (
    "event_n",
    "time_ms",
    "time_s",
    "dt_ms",
    "dt_s",
    "revolutions",
    "cumulative_distance_m",
    "speed_m_s",
    "dropped_events",
)
