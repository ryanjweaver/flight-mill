"""Frozen trial state machine used by simulator and host validation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from flightmill.constants import PHYSICAL_STOP_HOLD_MS


class TrialState(StrEnum):
    BOOTING = "BOOTING"
    IDLE = "IDLE"
    ARMED = "ARMED"
    RECORDING = "RECORDING"
    STOPPING = "STOPPING"
    COMPLETE = "COMPLETE"
    ERROR = "ERROR"


class TransitionError(RuntimeError):
    """Raised when an action is unsafe in the current state."""


class ButtonAction(StrEnum):
    STARTED = "started"
    STOP_REQUESTED = "stop_requested"
    IGNORED = "ignored"
    WARNING = "warning"


@dataclass(frozen=True, slots=True)
class ButtonOutcome:
    action: ButtonAction
    message: str


@dataclass(slots=True)
class DeviceStateMachine:
    state: TrialState = TrialState.BOOTING
    session_id: UUID | None = None

    def boot_complete(self) -> None:
        self._require(TrialState.BOOTING)
        self.state = TrialState.IDLE

    def arm(self, session_id: UUID) -> None:
        self._require(TrialState.IDLE)
        self.session_id = session_id
        self.state = TrialState.ARMED

    def start(self) -> None:
        self._require(TrialState.ARMED)
        if self.session_id is None:
            raise TransitionError("cannot start without an armed session")
        self.state = TrialState.RECORDING

    def request_stop(self) -> None:
        self._require(TrialState.RECORDING)
        self.state = TrialState.STOPPING

    def complete(self) -> None:
        self._require(TrialState.STOPPING)
        self.state = TrialState.COMPLETE

    def disarm(self) -> None:
        if self.state not in {TrialState.ARMED, TrialState.COMPLETE}:
            raise TransitionError(f"cannot disarm from {self.state}")
        self.session_id = None
        self.state = TrialState.IDLE

    def fail(self) -> None:
        self.state = TrialState.ERROR

    def reset(self) -> None:
        self.state = TrialState.BOOTING
        self.session_id = None

    def physical_button(self, held_ms: int) -> ButtonOutcome:
        if held_ms < 0:
            raise ValueError("held_ms must be nonnegative")
        if self.state == TrialState.IDLE:
            return ButtonOutcome(
                ButtonAction.WARNING,
                "Device is not armed; no unrecorded trial was started.",
            )
        if self.state == TrialState.ARMED:
            if held_ms >= PHYSICAL_STOP_HOLD_MS:
                return ButtonOutcome(ButtonAction.IGNORED, "Long press ignored while armed.")
            self.start()
            return ButtonOutcome(ButtonAction.STARTED, "Armed trial started.")
        if self.state == TrialState.RECORDING:
            if held_ms < PHYSICAL_STOP_HOLD_MS:
                return ButtonOutcome(
                    ButtonAction.IGNORED,
                    "Short press ignored while recording.",
                )
            self.request_stop()
            return ButtonOutcome(ButtonAction.STOP_REQUESTED, "Trial stop requested.")
        return ButtonOutcome(ButtonAction.IGNORED, f"Button ignored while {self.state}.")

    def _require(self, expected: TrialState) -> None:
        if self.state != expected:
            raise TransitionError(f"expected {expected}, found {self.state}")
