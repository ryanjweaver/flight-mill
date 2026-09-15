#pragma once

#include <Arduino.h>

namespace flightmill {

constexpr char PROTOCOL_NAME[] = "flightmill";
constexpr uint8_t PROTOCOL_VERSION = 1;
constexpr char FIRMWARE_VERSION[] = "0.1.5-dev";
constexpr uint32_t SERIAL_BAUD = 115200;
constexpr uint32_t SERIAL_WAIT_TIMEOUT_MS = 8000;
constexpr size_t SERIAL_LINE_CAPACITY = 768;
// Retain the boot pair and several idle heartbeats while the host attaches.
// This is finite startup headroom, not replay support for a late connection.
constexpr size_t SERIAL_TX_CAPACITY = 8192;
static_assert(SERIAL_TX_CAPACITY >= 10 * SERIAL_LINE_CAPACITY,
              "USB startup queue must hold the boot pair plus eight protocol-sized lines");

constexpr uint8_t SENSOR_GPIO = 4;
constexpr uint8_t START_STOP_BUTTON_GPIO = 5;
constexpr uint8_t READY_LED_GPIO = 8;
constexpr uint8_t REC_LED_GPIO = 9;
constexpr uint8_t EVENT_LED_GPIO = 10;

constexpr uint8_t SENSOR_CLEAR = 0;
constexpr uint8_t SENSOR_BLOCKED = 1;
constexpr uint32_t MIN_EVENT_INTERVAL_US = 150000;
constexpr uint32_t LEGACY_MIN_EVENT_INTERVAL_US = 50000;
constexpr size_t EVENT_BUFFER_SIZE = 256;
constexpr uint32_t EXPECTED_FLASH_BYTES = 4U * 1024U * 1024U;
constexpr uint32_t EXPECTED_PSRAM_BYTES = 2U * 1024U * 1024U;

constexpr uint32_t HEARTBEAT_INTERVAL_MS = 1000;
constexpr uint32_t BUTTON_DEBOUNCE_MS = 30;
constexpr uint32_t PHYSICAL_STOP_HOLD_MS = 1500;
constexpr uint32_t EVENT_LED_PULSE_MS = 40;
constexpr uint32_t WARNING_PATTERN_MS = 800;

enum class DeviceState : uint8_t {
  BOOTING,
  IDLE,
  ARMED,
  RECORDING,
  STOPPING,
  COMPLETE,
  ERROR_STATE,
};

inline const char *stateName(DeviceState state) {
  switch (state) {
    case DeviceState::BOOTING:
      return "BOOTING";
    case DeviceState::IDLE:
      return "IDLE";
    case DeviceState::ARMED:
      return "ARMED";
    case DeviceState::RECORDING:
      return "RECORDING";
    case DeviceState::STOPPING:
      return "STOPPING";
    case DeviceState::COMPLETE:
      return "COMPLETE";
    case DeviceState::ERROR_STATE:
      return "ERROR";
  }
  return "ERROR";
}

}  // namespace flightmill
