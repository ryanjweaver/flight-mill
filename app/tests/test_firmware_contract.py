from __future__ import annotations

import re
import unittest
from configparser import ConfigParser
from pathlib import Path

from flightmill.constants import (
    DEFAULT_MIN_EVENT_INTERVAL_US,
    EVENT_LED_GPIO,
    PHYSICAL_STOP_HOLD_MS,
    READY_LED_GPIO,
    REC_LED_GPIO,
    SENSOR_BLOCKED,
    SENSOR_CLEAR,
    SENSOR_EVENT_EDGE,
    SENSOR_GPIO,
    START_STOP_BUTTON_GPIO,
)


ROOT = Path(__file__).parents[2]
CONFIG = ROOT / "firmware" / "include" / "flightmill_config.hpp"
MAIN = ROOT / "firmware" / "src" / "main.cpp"
PLATFORMIO = ROOT / "firmware" / "platformio.ini"
FLASH_SCRIPT = ROOT / "scripts" / "flash_firmware.ps1"


def cpp_integer(name: str) -> int:
    text = CONFIG.read_text(encoding="utf-8")
    match = re.search(rf"\b{name}\s*=\s*(\d+)\s*;", text)
    if not match:
        raise AssertionError(f"missing integer firmware constant: {name}")
    return int(match.group(1))


class FirmwareContractTests(unittest.TestCase):
    def test_build_profile_matches_fh4r2_memory(self) -> None:
        parser = ConfigParser(interpolation=None)
        parser.read(PLATFORMIO, encoding="utf-8")
        profile = parser["env:supermini_hw747_v002"]
        self.assertEqual(profile["board_build.flash_mode"], "qio")
        self.assertEqual(profile["board_build.arduino.memory_type"], "qio_qspi")
        self.assertEqual(profile["board_build.partitions"], "default.csv")
        self.assertEqual(profile["board_upload.flash_size"], "4MB")
        self.assertEqual(profile.getint("board_upload.maximum_size"), 4 * 1024 * 1024)
        self.assertIn("-D BOARD_HAS_PSRAM", profile["build_flags"])

    def test_firmware_constants_match_host_contract(self) -> None:
        self.assertEqual(cpp_integer("SENSOR_GPIO"), SENSOR_GPIO)
        self.assertEqual(cpp_integer("START_STOP_BUTTON_GPIO"), START_STOP_BUTTON_GPIO)
        self.assertEqual(cpp_integer("READY_LED_GPIO"), READY_LED_GPIO)
        self.assertEqual(cpp_integer("REC_LED_GPIO"), REC_LED_GPIO)
        self.assertEqual(cpp_integer("EVENT_LED_GPIO"), EVENT_LED_GPIO)
        self.assertEqual(cpp_integer("SENSOR_CLEAR"), SENSOR_CLEAR)
        self.assertEqual(cpp_integer("SENSOR_BLOCKED"), SENSOR_BLOCKED)
        self.assertEqual(cpp_integer("MIN_EVENT_INTERVAL_US"), DEFAULT_MIN_EVENT_INTERVAL_US)
        self.assertEqual(cpp_integer("PHYSICAL_STOP_HOLD_MS"), PHYSICAL_STOP_HOLD_MS)

    def test_boot_fails_closed_on_wrong_memory_configuration(self) -> None:
        source = MAIN.read_text(encoding="utf-8")
        self.assertIn("ESP.getFlashChipSize() == fm::EXPECTED_FLASH_BYTES", source)
        self.assertIn("esp_spiram_get_size() == fm::EXPECTED_PSRAM_BYTES", source)
        self.assertIn('"memory_configuration_mismatch"', source)

    def test_requested_interval_is_applied_and_legacy_remains_explicit(self) -> None:
        source = MAIN.read_text(encoding="utf-8")
        self.assertEqual(cpp_integer("MIN_EVENT_INTERVAL_US"), 150_000)
        self.assertEqual(cpp_integer("LEGACY_MIN_EVENT_INTERVAL_US"), 50_000)
        self.assertEqual(source.count(
            'configuredMinEventIntervalUs = command["config"]["min_event_interval_us"].as<uint32_t>();'
        ), 2)
        self.assertIn("dt < configuredMinEventIntervalUs", source)

    def test_sensor_interrupt_preserves_confirmed_edge_and_pullup(self) -> None:
        source = MAIN.read_text(encoding="utf-8")
        self.assertEqual(SENSOR_EVENT_EDGE, "RISING")
        self.assertIn("pinMode(fm::SENSOR_GPIO, INPUT_PULLUP)", source)
        self.assertIn("attachInterrupt(digitalPinToInterrupt(fm::SENSOR_GPIO), onBeamBreak, RISING)", source)

    def test_native_usb_rx_queue_accepts_the_longest_protocol_line(self) -> None:
        source = MAIN.read_text(encoding="utf-8")
        resize = source.index("Serial.setRxBufferSize(fm::SERIAL_LINE_CAPACITY)")
        begin = source.index("Serial.begin(fm::SERIAL_BAUD)")
        self.assertLess(resize, begin)

    def test_isr_has_no_formatting_serial_led_or_delay_work(self) -> None:
        source = MAIN.read_text(encoding="utf-8")
        match = re.search(
            r"void IRAM_ATTR onBeamBreak\(\) \{(?P<body>.*?)\n\}",
            source,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(match)
        body = match.group("body")
        for forbidden in ("Serial", "Json", "serialize", "digitalWrite", "delay("):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, body)

    def test_all_protocol_device_message_types_are_implemented(self) -> None:
        source = MAIN.read_text(encoding="utf-8")
        for message_type in (
            "hello",
            "ack",
            "error",
            "status",
            "heartbeat",
            "sensor_state",
            "trial_armed",
            "trial_started",
            "event",
            "trial_stopped",
        ):
            with self.subTest(message_type=message_type):
                self.assertIn(f'"{message_type}"', source)

    def test_ui_start_ack_precedes_started_notification(self) -> None:
        source = MAIN.read_text(encoding="utf-8")
        branch = re.search(
            r'if \(strcmp\(type, "start"\) == 0\) \{(?P<body>.*?)\n  \}\n'
            r'  if \(strcmp\(type, "stop"\)',
            source,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(branch)
        body = branch.group("body")
        self.assertLess(body.index("sendAck(requestId, type)"), body.index('sendTrialStarted("ui")'))

    def test_controlled_flash_checks_exact_binary_before_and_after_upload(self) -> None:
        source = FLASH_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("[Parameter(Mandatory = $true)]", source)
        self.assertIn("$ExpectedSha256", source)
        self.assertIn("FIRMWARE_BIN_SHA256_BEFORE_UPLOAD", source)
        self.assertIn("--target upload --upload-port $Port", source)
        self.assertIn("FIRMWARE_BIN_SHA256_AFTER_UPLOAD", source)


if __name__ == "__main__":
    unittest.main()
