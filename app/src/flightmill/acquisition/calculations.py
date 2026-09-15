"""Authoritative host calculations for Flight Mill raw CSV rows."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SourceEvent:
    """Lossless integer event data received from the device."""

    event_n: int
    event_us: int
    dt_us: int
    dropped_events: int

    def __post_init__(self) -> None:
        if self.event_n < 1:
            raise ValueError("event_n must be a positive integer")
        if self.event_us < 0 or self.dt_us < 0 or self.dropped_events < 0:
            raise ValueError("event timing and dropped_events must be nonnegative")
        if self.event_n == 1 and self.dt_us != 0:
            raise ValueError("the first event must have dt_us=0")
        if self.event_n > 1 and self.dt_us == 0:
            raise ValueError("events after the first must have dt_us>0")


@dataclass(frozen=True, slots=True)
class DerivedEvent:
    """One event represented in the exact raw CSV column order."""

    event_n: int
    time_ms: float
    time_s: float
    dt_ms: float
    dt_s: float
    revolutions: int
    cumulative_distance_m: float
    speed_m_s: float | None
    dropped_events: int


def circumference_m(radius_m: float) -> float:
    """Return distance per revolution for a positive arm radius."""

    if not math.isfinite(radius_m) or radius_m <= 0:
        raise ValueError("arm radius must be a finite value greater than zero")
    return 2.0 * math.pi * radius_m


def speed_display_gap_limit_us(previous_dt_us: int) -> int:
    """Display inactivity hint only; never a flight-bout or raw-data filter."""

    return max(2_000_000, 2 * previous_dt_us)


def derive_event(source: SourceEvent, radius_m: float) -> DerivedEvent:
    """Derive every floating-point acquisition value in one place."""

    distance_per_revolution = circumference_m(radius_m)
    dt_s = source.dt_us / 1_000_000.0
    speed = None if source.dt_us == 0 else distance_per_revolution / dt_s
    return DerivedEvent(
        event_n=source.event_n,
        time_ms=source.event_us / 1_000.0,
        time_s=source.event_us / 1_000_000.0,
        dt_ms=source.dt_us / 1_000.0,
        dt_s=dt_s,
        revolutions=source.event_n,
        cumulative_distance_m=source.event_n * distance_per_revolution,
        speed_m_s=speed,
        dropped_events=source.dropped_events,
    )


def raw_csv_row(event: DerivedEvent) -> tuple[str, ...]:
    """Serialize one derived event with locale-independent decimal points.

    Microsecond timing remains exact at three decimal places in milliseconds or
    six in seconds. Derived distance and speed use ten decimal places to match
    the published golden fixtures without pretending to be source measurements.
    """

    return (
        str(event.event_n),
        f"{event.time_ms:.3f}",
        f"{event.time_s:.6f}",
        f"{event.dt_ms:.3f}",
        f"{event.dt_s:.6f}",
        str(event.revolutions),
        f"{event.cumulative_distance_m:.10f}",
        "" if event.speed_m_s is None else f"{event.speed_m_s:.10f}",
        str(event.dropped_events),
    )
