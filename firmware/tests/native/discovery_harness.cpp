// Run the production command parser and JSON serializer on the host CPU.
// Hardware APIs are stubbed; this is not board/USB/timing acceptance evidence.
#include <iostream>
#include <string>

#include "../../src/main.cpp"

namespace {

void addSnapshot(JsonObject snapshot) {
  snapshot["device_id"] = deviceId;
  snapshot["boot_id"] = bootId;
  snapshot["state"] = fm::stateName(deviceState);
  snapshot["session_id"] = sessionId;
  snapshot["event_n"] = acceptedEventCount;
  snapshot["dropped_events"] = droppedEventCount;
  snapshot["buffer_head"] = bufferHead;
  snapshot["buffer_tail"] = bufferTail;
  snapshot["queued_event_n"] = eventBuffer[7].eventN;
  snapshot["queued_event_us"] = eventBuffer[7].eventUs;
  snapshot["queued_dt_us"] = eventBuffer[7].dtUs;
  snapshot["queued_dropped_events"] = eventBuffer[7].droppedEvents;
  snapshot["last_accepted_absolute_us"] = lastAcceptedAbsoluteUs;
  snapshot["trial_start_absolute_us"] = trialStartAbsoluteUs;
  snapshot["min_event_interval_us"] = configuredMinEventIntervalUs;
  snapshot["sensor_state"] = lastReportedSensorState;
  snapshot["heartbeat_ms"] = lastHeartbeatMs;
  snapshot["event_led_until_ms"] = eventLedUntilMs;
  snapshot["warning_started_ms"] = warningStartedMs;
  snapshot["button_hold_handled"] = buttonHoldHandled;
  snapshot["uptime_ms"] = native_hardware::nowMs;
  snapshot["digital_writes"] = native_hardware::digitalWrites;
  snapshot["delays"] = native_hardware::delays;
  snapshot["pin_modes"] = native_hardware::pinModes;
  snapshot["interrupts"] = native_hardware::interrupts;
  snapshot["serial_begins"] = native_hardware::serialBegins;
  snapshot["serial_resizes"] = native_hardware::serialResizes;
}

}  // namespace

int main(int argc, char **argv) {
  if (argc != 2) {
    std::cerr << "Usage: discovery_harness STATE\n";
    return 2;
  }
  const fm::DeviceState states[] = {
      fm::DeviceState::BOOTING, fm::DeviceState::IDLE, fm::DeviceState::ARMED,
      fm::DeviceState::RECORDING, fm::DeviceState::STOPPING,
      fm::DeviceState::COMPLETE, fm::DeviceState::ERROR_STATE};
  bool found = false;
  for (const fm::DeviceState candidate : states) {
    if (std::strcmp(argv[1], fm::stateName(candidate)) == 0) {
      deviceState = candidate;
      found = true;
      break;
    }
  }
  if (!found) {
    std::cerr << "Unknown state\n";
    return 2;
  }

  deviceId = "FM-S3-020000000001";
  bootId = "00000000-00000000-00000001";
  if (deviceState == fm::DeviceState::ARMED ||
      deviceState == fm::DeviceState::RECORDING ||
      deviceState == fm::DeviceState::STOPPING ||
      deviceState == fm::DeviceState::COMPLETE) {
    sessionId = "20000000-0000-4000-8000-000000000001";
  }
  acceptedEventCount = 42;
  droppedEventCount = 3;
  bufferHead = 8;
  bufferTail = 7;
  eventBuffer[7].eventN = 42;
  eventBuffer[7].eventUs = 54321000;
  eventBuffer[7].dtUs = 250000;
  eventBuffer[7].droppedEvents = 3;
  lastAcceptedAbsoluteUs = 654321000;
  trialStartAbsoluteUs = 600000000;
  configuredMinEventIntervalUs = fm::LEGACY_MIN_EVENT_INTERVAL_US;
  lastReportedSensorState = fm::SENSOR_CLEAR;
  lastHeartbeatMs = 3599000;
  eventLedUntilMs = 3599010;
  warningStartedMs = 3599020;
  buttonHoldHandled = true;
  messageSeq = 4000;

  JsonDocument report;
  addSnapshot(report["before"].to<JsonObject>());
  std::string line;
  while (std::getline(std::cin, line)) {
    processCommand(line.c_str());
  }
  addSnapshot(report["after"].to<JsonObject>());
  report["next_message_seq"] = messageSeq;
  report["firmware_version"] = fm::FIRMWARE_VERSION;
  std::cout << Serial.output;
  serializeJson(report, std::cerr);
  std::cerr << '\n';
  return 0;
}
