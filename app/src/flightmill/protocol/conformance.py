"""Reusable transcript conformance checks for simulator and real firmware."""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from flightmill.acquisition.state import TrialState
from flightmill.protocol.integrity import IntegrityTracker
from flightmill.protocol.models import (
    AckMessage,
    ArmCommand,
    DeviceMessage,
    ErrorMessage,
    EventMessage,
    HostCommand,
    Message,
    ProtocolError,
    TrialArmedMessage,
    parse_line,
)


@dataclass(frozen=True, slots=True)
class ConformanceIssue:
    frame_index: int
    code: str
    detail: str


@dataclass(slots=True)
class ConformanceReport:
    parsed_messages: list[Message] = field(default_factory=list)
    issues: list[ConformanceIssue] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.issues


def check_transcript(frames: list[bytes | str | None]) -> ConformanceReport:
    """Check a bidirectional log or device-only output transcript.

    `None` represents a transport disconnect. If host commands appear, ACK/ERROR
    correlation is mandatory. Device-only streams still receive envelope,
    sequence, boot, session, event, and state checks.
    """

    report = ConformanceReport()
    tracker = IntegrityTracker()
    pending: dict[UUID, str] = {}
    saw_host_command = False
    device_state: TrialState | None = None
    unmatched_response_indices: set[int] = set()

    for index, frame in enumerate(frames, start=1):
        if frame is None:
            report.issues.append(ConformanceIssue(index, "disconnect", "transport disconnected"))
            continue
        try:
            message = parse_line(frame)
        except ProtocolError as exc:
            report.issues.append(ConformanceIssue(index, "malformed_line", str(exc)))
            continue
        report.parsed_messages.append(message)

        if isinstance(message, HostCommand):
            saw_host_command = True
            if message.request_id in pending:
                report.issues.append(
                    ConformanceIssue(index, "duplicate_request_id", str(message.request_id))
                )
            pending[message.request_id] = message.type
            if isinstance(message, ArmCommand):
                tracker.arm(message.session_id)
            continue

        if not isinstance(message, DeviceMessage):
            continue
        if isinstance(message, TrialArmedMessage) and not saw_host_command:
            tracker.arm(message.session_id)
        for finding in tracker.observe(message):
            report.issues.append(ConformanceIssue(index, finding.value, message.type))

        message_state = getattr(message, "state", None)
        if isinstance(message_state, str):
            device_state = TrialState(message_state)
        elif isinstance(message_state, TrialState):
            device_state = message_state
        if isinstance(message, EventMessage) and device_state != TrialState.RECORDING:
            report.issues.append(
                ConformanceIssue(index, "event_outside_recording", str(device_state))
            )

        if isinstance(message, (AckMessage, ErrorMessage)) and message.request_id is not None:
            expected = pending.pop(message.request_id, None)
            if expected is None:
                unmatched_response_indices.add(len(report.issues))
                report.issues.append(
                    ConformanceIssue(index, "unmatched_response", str(message.request_id))
                )
            elif message.command != expected:
                report.issues.append(
                    ConformanceIssue(
                        index,
                        "response_command_mismatch",
                        f"expected {expected}, received {message.command}",
                    )
                )

    if not saw_host_command and unmatched_response_indices:
        report.issues = [
            issue for position, issue in enumerate(report.issues) if position not in unmatched_response_indices
        ]
    if saw_host_command:
        for request_id, command in pending.items():
            report.issues.append(
                ConformanceIssue(len(frames), "missing_response", f"{command} {request_id}")
            )
    return report
