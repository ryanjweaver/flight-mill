"""Stateful detection of reset, sequence, session, and event anomalies."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID

from flightmill.protocol.models import DeviceMessage, EventMessage


class FindingCode(StrEnum):
    BOOT_CHANGED = "boot_changed"
    DUPLICATE_MESSAGE = "duplicate_message"
    MESSAGE_GAP = "message_gap"
    NONMONOTONIC_MESSAGE = "nonmonotonic_message"
    SESSION_MISMATCH = "session_mismatch"
    DUPLICATE_EVENT = "duplicate_event"
    EVENT_GAP = "event_gap"
    NONMONOTONIC_EVENT = "nonmonotonic_event"
    NONMONOTONIC_EVENT_TIME = "nonmonotonic_event_time"
    DROPPED_EVENTS_INCREASED = "dropped_events_increased"
    DROPPED_EVENTS_DECREASED = "dropped_events_decreased"


@dataclass(slots=True)
class IntegrityTracker:
    active_session_id: UUID | None = None
    boot_id: str | None = None
    last_message_seq: int | None = None
    last_event_n: int | None = None
    last_event_us: int | None = None
    last_dropped_events: int = 0
    findings: list[FindingCode] = field(default_factory=list)

    def arm(self, session_id: UUID) -> None:
        self.active_session_id = session_id
        self.last_event_n = None
        self.last_event_us = None
        self.last_dropped_events = 0

    def observe(self, message: DeviceMessage) -> list[FindingCode]:
        current: list[FindingCode] = []
        if self.boot_id is not None and message.boot_id != self.boot_id:
            current.append(FindingCode.BOOT_CHANGED)
            self.last_message_seq = None
            self.last_event_n = None
            self.last_event_us = None
        self.boot_id = message.boot_id

        if self.last_message_seq is not None:
            if message.message_seq == self.last_message_seq:
                current.append(FindingCode.DUPLICATE_MESSAGE)
            elif message.message_seq < self.last_message_seq:
                current.append(FindingCode.NONMONOTONIC_MESSAGE)
            elif message.message_seq > self.last_message_seq + 1:
                current.append(FindingCode.MESSAGE_GAP)
        self.last_message_seq = message.message_seq

        if isinstance(message, EventMessage):
            if self.active_session_id is None or message.session_id != self.active_session_id:
                current.append(FindingCode.SESSION_MISMATCH)
            if self.last_event_n is not None:
                if message.event_n == self.last_event_n:
                    current.append(FindingCode.DUPLICATE_EVENT)
                elif message.event_n < self.last_event_n:
                    current.append(FindingCode.NONMONOTONIC_EVENT)
                elif message.event_n > self.last_event_n + 1:
                    current.append(FindingCode.EVENT_GAP)
            if self.last_event_us is not None and message.event_us <= self.last_event_us:
                current.append(FindingCode.NONMONOTONIC_EVENT_TIME)
            if message.dropped_events > self.last_dropped_events:
                current.append(FindingCode.DROPPED_EVENTS_INCREASED)
            elif message.dropped_events < self.last_dropped_events:
                current.append(FindingCode.DROPPED_EVENTS_DECREASED)
            self.last_event_n = message.event_n
            self.last_event_us = message.event_us
            self.last_dropped_events = message.dropped_events

        self.findings.extend(current)
        return current
