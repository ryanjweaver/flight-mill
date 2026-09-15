from __future__ import annotations

import unittest
from uuid import UUID

from flightmill.acquisition.state import (
    ButtonAction,
    DeviceStateMachine,
    TransitionError,
    TrialState,
)


SESSION = UUID("20000000-0000-4000-8000-000000000001")


class StateMachineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.machine = DeviceStateMachine()
        self.machine.boot_complete()

    def test_normal_lifecycle(self) -> None:
        self.machine.arm(SESSION)
        self.machine.start()
        self.machine.request_stop()
        self.machine.complete()
        self.machine.disarm()
        self.assertEqual(self.machine.state, TrialState.IDLE)
        self.assertIsNone(self.machine.session_id)

    def test_unarmed_button_warns_without_starting(self) -> None:
        outcome = self.machine.physical_button(100)
        self.assertEqual(outcome.action, ButtonAction.WARNING)
        self.assertEqual(self.machine.state, TrialState.IDLE)

    def test_short_button_starts_armed_trial(self) -> None:
        self.machine.arm(SESSION)
        outcome = self.machine.physical_button(100)
        self.assertEqual(outcome.action, ButtonAction.STARTED)
        self.assertEqual(self.machine.state, TrialState.RECORDING)

    def test_stop_hold_boundary_and_short_press(self) -> None:
        self.machine.arm(SESSION)
        self.machine.start()
        short = self.machine.physical_button(1_499)
        self.assertEqual(short.action, ButtonAction.IGNORED)
        self.assertEqual(self.machine.state, TrialState.RECORDING)
        long = self.machine.physical_button(1_500)
        self.assertEqual(long.action, ButtonAction.STOP_REQUESTED)
        self.assertEqual(self.machine.state, TrialState.STOPPING)

    def test_invalid_transition_fails_closed(self) -> None:
        with self.assertRaises(TransitionError):
            self.machine.start()


if __name__ == "__main__":
    unittest.main()
