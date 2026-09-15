from __future__ import annotations

import math
import unittest

from flightmill.acquisition.calculations import (
    SourceEvent,
    circumference_m,
    derive_event,
    raw_csv_row,
)


class CalculationTests(unittest.TestCase):
    def test_default_circumference_golden_value(self) -> None:
        self.assertEqual(circumference_m(0.10), 0.6283185307179586)

    def test_first_event_has_blank_speed(self) -> None:
        event = derive_event(SourceEvent(1, 0, 0, 0), 0.10)
        row = raw_csv_row(event)
        self.assertEqual(row, ("1", "0.000", "0.000000", "0.000", "0.000000", "1", "0.6283185307", "", "0"))

    def test_second_event_one_second_later(self) -> None:
        event = derive_event(SourceEvent(2, 1_000_000, 1_000_000, 0), 0.10)
        self.assertTrue(math.isclose(event.speed_m_s or 0, 0.6283185307179586))
        self.assertEqual(raw_csv_row(event)[7], "0.6283185307")

    def test_twenty_revolutions_golden_distance(self) -> None:
        event = derive_event(SourceEvent(20, 20_000_000, 1_000_000, 0), 0.10)
        self.assertEqual(raw_csv_row(event)[6], "12.5663706144")

    def test_invalid_radius_and_event_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            circumference_m(0)
        with self.assertRaises(ValueError):
            SourceEvent(2, 1, 0, 0)


if __name__ == "__main__":
    unittest.main()
