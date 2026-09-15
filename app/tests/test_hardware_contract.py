from __future__ import annotations

import unittest

from flightmill.constants import (
    EVENT_LED_GPIO,
    READY_LED_GPIO,
    REC_LED_GPIO,
    SENSOR_GPIO,
    START_STOP_BUTTON_GPIO,
)
from flightmill.hardware import (
    DEV_BOARD_ALONG_ROW_PIN_PITCH_MM,
    DEV_BOARD_CARRIER_MOUNTING,
    DEV_BOARD_HARDWARE_REVISION,
    DEV_BOARD_HEADER_ROWS,
    DEV_BOARD_LENGTH_WITH_USB_MM_APPROX,
    DEV_BOARD_PCB_LENGTH_MM_APPROX,
    DEV_BOARD_PIN_ROW_SPACING_MM_APPROX,
    DEV_BOARD_PINS_PER_HEADER_ROW,
    DEV_BOARD_MALE_HEADERS_INSTALLED,
    DEV_BOARD_WIDTH_MM_APPROX,
    EXPOSED_STRAPPING_PINS,
    ESP32_FLASH_INTERFACE,
    ESP32_IN_PACKAGE_FLASH_MB,
    ESP32_IN_PACKAGE_PSRAM_MB,
    ESP32_PART_NUMBER,
    ESP32_PSRAM_INTERFACE,
    LEFT_PINS_USB_UP,
    NATIVE_USB_GPIOS,
    RIGHT_PINS_USB_UP,
)

class HardwareContractTests(unittest.TestCase):
    def test_photographically_identified_silicon_and_memory(self) -> None:
        self.assertEqual(DEV_BOARD_HARDWARE_REVISION, "HW-747 V0.0.2")
        self.assertEqual(ESP32_PART_NUMBER, "ESP32-S3FH4R2")
        self.assertEqual(
            (
                ESP32_IN_PACKAGE_FLASH_MB,
                ESP32_IN_PACKAGE_PSRAM_MB,
                ESP32_FLASH_INTERFACE,
                ESP32_PSRAM_INTERFACE,
            ),
            (4, 2, "Quad SPI", "Quad SPI"),
        )

    def test_user_supplied_supermini_geometry_and_pin_order(self) -> None:
        self.assertEqual(
            (
                DEV_BOARD_PCB_LENGTH_MM_APPROX,
                DEV_BOARD_WIDTH_MM_APPROX,
                DEV_BOARD_LENGTH_WITH_USB_MM_APPROX,
                DEV_BOARD_PIN_ROW_SPACING_MM_APPROX,
                DEV_BOARD_ALONG_ROW_PIN_PITCH_MM,
            ),
            (23.5, 18.0, 25.0, 15.5, 2.54),
        )
        self.assertAlmostEqual(8 * DEV_BOARD_ALONG_ROW_PIN_PITCH_MM, 20.32)
        self.assertEqual((DEV_BOARD_HEADER_ROWS, DEV_BOARD_PINS_PER_HEADER_ROW), (2, 9))
        self.assertTrue(DEV_BOARD_MALE_HEADERS_INSTALLED)
        self.assertEqual(
            DEV_BOARD_CARRIER_MOUNTING,
            "two 1x9 male headers into two 1x9 female carrier sockets",
        )
        self.assertEqual(
            LEFT_PINS_USB_UP,
            ("TX", "RX", "GPIO1", "GPIO2", "GPIO3", "GPIO4", "GPIO5", "GPIO6", "GPIO7"),
        )
        self.assertEqual(
            RIGHT_PINS_USB_UP,
            ("5V", "GND", "3V3", "GPIO13", "GPIO12", "GPIO11", "GPIO10", "GPIO9", "GPIO8"),
        )

    def test_carrier_assignments_are_exposed_unique_and_unrestricted(self) -> None:
        assigned = {
            SENSOR_GPIO,
            START_STOP_BUTTON_GPIO,
            READY_LED_GPIO,
            REC_LED_GPIO,
            EVENT_LED_GPIO,
        }
        exposed_gpio = {
            int(pin.removeprefix("GPIO"))
            for pin in LEFT_PINS_USB_UP + RIGHT_PINS_USB_UP
            if pin.startswith("GPIO")
        }
        self.assertEqual(len(assigned), 5)
        self.assertTrue(assigned <= exposed_gpio)
        self.assertFalse(assigned & EXPOSED_STRAPPING_PINS)
        self.assertFalse(assigned & NATIVE_USB_GPIOS)


if __name__ == "__main__":
    unittest.main()
