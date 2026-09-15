#include <Arduino.h>
#include <ArduinoJson.h>
#include <esp32s3/spiram.h>
#include <esp_system.h>
#include <esp_timer.h>

#include "flightmill_config.hpp"

namespace fm = flightmill;

namespace {

struct EventRecord {
  uint32_t eventN;
  uint64_t eventUs;
  uint64_t dtUs;
  uint32_t droppedEvents;
};

volatile EventRecord eventBuffer[fm::EVENT_BUFFER_SIZE];
volatile uint16_t bufferHead = 0;
volatile uint16_t bufferTail = 0;
volatile uint32_t acceptedEventCount = 0;
volatile uint32_t droppedEventCount = 0;
volatile uint64_t lastAcceptedAbsoluteUs = 0;
volatile uint64_t trialStartAbsoluteUs = 0;
volatile fm::DeviceState deviceState = fm::DeviceState::BOOTING;
portMUX_TYPE eventMux = portMUX_INITIALIZER_UNLOCKED;

String deviceId;
String bootId;
String sessionId;
uint32_t messageSeq = 0;
volatile uint32_t configuredMinEventIntervalUs = fm::MIN_EVENT_INTERVAL_US;

char serialLine[fm::SERIAL_LINE_CAPACITY];
size_t serialLineLength = 0;
bool serialLineOverflow = false;

int lastReportedSensorState = -1;
uint32_t lastHeartbeatMs = 0;
uint32_t eventLedUntilMs = 0;
uint32_t warningStartedMs = 0;

bool rawButtonState = HIGH;
bool stableButtonState = HIGH;
uint32_t rawButtonChangedMs = 0;
uint32_t buttonPressedMs = 0;
bool buttonHoldHandled = false;

void sendHello();

bool isHex(char value) {
  return (value >= '0' && value <= '9') || (value >= 'a' && value <= 'f') ||
         (value >= 'A' && value <= 'F');
}

bool isUuid(const char *value) {
  if (value == nullptr || strlen(value) != 36) {
    return false;
  }
  for (size_t index = 0; index < 36; ++index) {
    const bool hyphen = index == 8 || index == 13 || index == 18 || index == 23;
    if (hyphen ? value[index] != '-' : !isHex(value[index])) {
      return false;
    }
  }
  return true;
}

void beginMessage(JsonDocument &message, const char *type) {
  message["protocol"] = fm::PROTOCOL_NAME;
  message["protocol_version"] = fm::PROTOCOL_VERSION;
  message["type"] = type;
  message["device_id"] = deviceId;
  message["firmware_version"] = fm::FIRMWARE_VERSION;
  message["message_seq"] = messageSeq++;
  message["boot_id"] = bootId;
}

void sendMessage(JsonDocument &message) {
  serializeJson(message, Serial);
  Serial.write('\n');
}

void sendError(const char *requestId, const char *command, const char *code,
               const char *text) {
  JsonDocument message;
  beginMessage(message, "error");
  if (requestId != nullptr && isUuid(requestId)) {
    message["request_id"] = requestId;
  }
  if (command != nullptr && command[0] != '\0') {
    message["command"] = command;
  }
  message["code"] = code;
  message["message"] = text;
  message["state"] = fm::stateName(deviceState);
  sendMessage(message);
}

void sendAck(const char *requestId, const char *command) {
  JsonDocument message;
  beginMessage(message, "ack");
  message["request_id"] = requestId;
  message["command"] = command;
  message["state"] = fm::stateName(deviceState);
  sendMessage(message);
}

void addSessionIfPresent(JsonDocument &message) {
  if (!sessionId.isEmpty()) {
    message["session_id"] = sessionId;
  }
}

void sendStatus(const char *type) {
  uint32_t eventSnapshot;
  uint32_t droppedSnapshot;
  portENTER_CRITICAL(&eventMux);
  eventSnapshot = acceptedEventCount;
  droppedSnapshot = droppedEventCount;
  portEXIT_CRITICAL(&eventMux);

  JsonDocument message;
  beginMessage(message, type);
  message["state"] = fm::stateName(deviceState);
  addSessionIfPresent(message);
  message["sensor_state"] = digitalRead(fm::SENSOR_GPIO);
  message["event_n"] = eventSnapshot;
  message["dropped_events"] = droppedSnapshot;
  message["uptime_ms"] = millis();
  sendMessage(message);
}

void sendSensorState(int sensorState) {
  JsonDocument message;
  beginMessage(message, "sensor_state");
  message["state"] = fm::stateName(deviceState);
  message["sensor_state"] = sensorState;
  sendMessage(message);
}

void sendTrialArmed() {
  JsonDocument message;
  beginMessage(message, "trial_armed");
  message["state"] = "ARMED";
  message["session_id"] = sessionId;
  message["config"]["min_event_interval_us"] = configuredMinEventIntervalUs;
  sendMessage(message);
}

void sendTrialStarted(const char *source) {
  JsonDocument message;
  beginMessage(message, "trial_started");
  message["state"] = "RECORDING";
  message["session_id"] = sessionId;
  message["start_source"] = source;
  sendMessage(message);
}

void sendTrialStopped(uint32_t count, uint32_t dropped, uint64_t durationUs,
                      const String &stoppedSession, const char *reason,
                      const char *source) {
  JsonDocument message;
  beginMessage(message, "trial_stopped");
  message["state"] = "COMPLETE";
  message["session_id"] = stoppedSession;
  message["accepted_event_count"] = count;
  message["dropped_events"] = dropped;
  message["duration_us"] = durationUs;
  message["stop_reason"] = reason;
  message["stop_source"] = source;
  sendMessage(message);
}

bool IRAM_ATTR pushEventFromIsr(const EventRecord &event) {
  const uint16_t nextHead = (bufferHead + 1) % fm::EVENT_BUFFER_SIZE;
  if (nextHead == bufferTail) {
    ++droppedEventCount;
    return false;
  }
  eventBuffer[bufferHead].eventN = event.eventN;
  eventBuffer[bufferHead].eventUs = event.eventUs;
  eventBuffer[bufferHead].dtUs = event.dtUs;
  eventBuffer[bufferHead].droppedEvents = event.droppedEvents;
  bufferHead = nextHead;
  return true;
}

void IRAM_ATTR onBeamBreak() {
  const uint64_t now = static_cast<uint64_t>(esp_timer_get_time());
  portENTER_CRITICAL_ISR(&eventMux);

  if (deviceState != fm::DeviceState::RECORDING) {
    portEXIT_CRITICAL_ISR(&eventMux);
    return;
  }

  const uint64_t dt = lastAcceptedAbsoluteUs == 0 ? 0 : now - lastAcceptedAbsoluteUs;
  if (lastAcceptedAbsoluteUs != 0 && dt < configuredMinEventIntervalUs) {
    portEXIT_CRITICAL_ISR(&eventMux);
    return;
  }

  lastAcceptedAbsoluteUs = now;
  const uint32_t eventN = ++acceptedEventCount;
  const EventRecord event = {
      eventN,
      now - trialStartAbsoluteUs,
      dt,
      droppedEventCount,
  };
  pushEventFromIsr(event);
  portEXIT_CRITICAL_ISR(&eventMux);
}

bool popEvent(EventRecord &event) {
  bool available = false;
  portENTER_CRITICAL(&eventMux);
  if (bufferTail != bufferHead) {
    event.eventN = eventBuffer[bufferTail].eventN;
    event.eventUs = eventBuffer[bufferTail].eventUs;
    event.dtUs = eventBuffer[bufferTail].dtUs;
    event.droppedEvents = eventBuffer[bufferTail].droppedEvents;
    bufferTail = (bufferTail + 1) % fm::EVENT_BUFFER_SIZE;
    available = true;
  }
  portEXIT_CRITICAL(&eventMux);
  return available;
}

void sendEvent(const EventRecord &event) {
  uint32_t droppedSnapshot;
  portENTER_CRITICAL(&eventMux);
  droppedSnapshot = droppedEventCount;
  portEXIT_CRITICAL(&eventMux);

  JsonDocument message;
  beginMessage(message, "event");
  message["state"] = "RECORDING";
  message["session_id"] = sessionId;
  message["event_n"] = event.eventN;
  message["event_us"] = event.eventUs;
  message["dt_us"] = event.dtUs;
  message["dropped_events"] = max(event.droppedEvents, droppedSnapshot);
  sendMessage(message);
  eventLedUntilMs = millis() + fm::EVENT_LED_PULSE_MS;
}

void drainEvents() {
  EventRecord event;
  while (popEvent(event)) {
    sendEvent(event);
  }
}

void clearTrialCountersForArm() {
  portENTER_CRITICAL(&eventMux);
  bufferHead = 0;
  bufferTail = 0;
  acceptedEventCount = 0;
  droppedEventCount = 0;
  lastAcceptedAbsoluteUs = 0;
  trialStartAbsoluteUs = 0;
  portEXIT_CRITICAL(&eventMux);
}

void startTrial() {
  portENTER_CRITICAL(&eventMux);
  bufferHead = 0;
  bufferTail = 0;
  acceptedEventCount = 0;
  droppedEventCount = 0;
  lastAcceptedAbsoluteUs = 0;
  trialStartAbsoluteUs = static_cast<uint64_t>(esp_timer_get_time());
  deviceState = fm::DeviceState::RECORDING;
  portEXIT_CRITICAL(&eventMux);
}

void stopTrial(const char *reason, const char *source, const char *requestId,
               bool acknowledge) {
  uint64_t durationUs;
  uint32_t count;
  uint32_t dropped;
  const String stoppedSession = sessionId;

  portENTER_CRITICAL(&eventMux);
  deviceState = fm::DeviceState::STOPPING;
  durationUs = static_cast<uint64_t>(esp_timer_get_time()) - trialStartAbsoluteUs;
  count = acceptedEventCount;
  dropped = droppedEventCount;
  portEXIT_CRITICAL(&eventMux);

  drainEvents();
  if (acknowledge) {
    sendAck(requestId, "stop");
  }
  deviceState = fm::DeviceState::COMPLETE;
  sendTrialStopped(count, dropped, durationUs, stoppedSession, reason, source);
}

bool isDiscoveryCommand(const char *command) {
  return strcmp(command, "ping") == 0 || strcmp(command, "get_status") == 0;
}

bool isKnownCommand(const char *command) {
  static const char *const commands[] = {
      "ping", "get_status", "arm", "start", "stop", "disarm", "set_config", "self_test"};
  for (const char *known : commands) {
    if (strcmp(command, known) == 0) {
      return true;
    }
  }
  return false;
}

bool commonEnvelopeValid(JsonObjectConst command) {
  const char *protocol = command["protocol"];
  const char *type = command["type"];
  const char *incomingDeviceId = command["device_id"];
  const char *incomingFirmware = command["firmware_version"];
  const char *requestId = command["request_id"];
  return protocol != nullptr && strcmp(protocol, fm::PROTOCOL_NAME) == 0 &&
         command["protocol_version"].is<uint8_t>() &&
         command["protocol_version"].as<uint8_t>() == fm::PROTOCOL_VERSION &&
         type != nullptr && isKnownCommand(type) && incomingDeviceId != nullptr &&
         incomingFirmware != nullptr && isUuid(requestId);
}

bool identityValid(JsonObjectConst command) {
  const char *type = command["type"];
  const char *incomingDeviceId = command["device_id"];
  const char *incomingFirmware = command["firmware_version"];
  const bool discovery = isDiscoveryCommand(type);
  const bool deviceMatches = strcmp(incomingDeviceId, deviceId.c_str()) == 0;
  const bool firmwareMatches = strcmp(incomingFirmware, fm::FIRMWARE_VERSION) == 0;
  if (discovery && strcmp(incomingDeviceId, "*") == 0 &&
      strcmp(incomingFirmware, "unknown") == 0) {
    return true;
  }
  return deviceMatches && firmwareMatches;
}

bool configValid(JsonObjectConst command) {
  if (!command["config"].is<JsonObjectConst>()) {
    return false;
  }
  const JsonObjectConst config = command["config"].as<JsonObjectConst>();
  return config.size() == 1 && config["min_event_interval_us"].is<uint32_t>() &&
         (config["min_event_interval_us"].as<uint32_t>() == fm::MIN_EVENT_INTERVAL_US ||
          config["min_event_interval_us"].as<uint32_t>() == fm::LEGACY_MIN_EVENT_INTERVAL_US);
}

bool sessionMatches(JsonObjectConst command) {
  const char *incomingSession = command["session_id"];
  return isUuid(incomingSession) && sessionId == incomingSession;
}

bool keyAllowed(const char *type, const char *key) {
  static const char *const common[] = {
      "protocol", "protocol_version", "type", "device_id", "firmware_version", "request_id"};
  for (const char *allowed : common) {
    if (strcmp(key, allowed) == 0) {
      return true;
    }
  }
  if ((strcmp(type, "arm") == 0) &&
      (strcmp(key, "session_id") == 0 || strcmp(key, "config") == 0)) {
    return true;
  }
  if ((strcmp(type, "start") == 0 || strcmp(type, "disarm") == 0) &&
      strcmp(key, "session_id") == 0) {
    return true;
  }
  if (strcmp(type, "stop") == 0 &&
      (strcmp(key, "session_id") == 0 || strcmp(key, "stop_reason") == 0)) {
    return true;
  }
  return strcmp(type, "set_config") == 0 && strcmp(key, "config") == 0;
}

bool hasOnlyAllowedKeys(JsonObjectConst command) {
  const char *type = command["type"];
  if (type == nullptr) {
    return false;
  }
  for (JsonPairConst pair : command) {
    if (!keyAllowed(type, pair.key().c_str())) {
      return false;
    }
  }
  return true;
}

bool hasKey(JsonObjectConst object, const char *key) {
  for (JsonPairConst pair : object) {
    if (strcmp(pair.key().c_str(), key) == 0) {
      return true;
    }
  }
  return false;
}

void processCommand(const char *line) {
  JsonDocument document;
  const DeserializationError parseError = deserializeJson(document, line);
  if (parseError || !document.is<JsonObject>()) {
    sendError(nullptr, nullptr, "malformed_command", "serial line is not a valid command object");
    return;
  }

  const JsonObjectConst command = document.as<JsonObjectConst>();
  const char *requestId = command["request_id"];
  const char *type = command["type"];
  if (!commonEnvelopeValid(command)) {
    sendError(requestId, nullptr, "invalid_envelope", "command envelope failed validation");
    return;
  }
  if (!hasOnlyAllowedKeys(command)) {
    sendError(requestId, type, "unexpected_field", "command contains an unexpected field");
    return;
  }
  if (!identityValid(command)) {
    sendError(requestId, type, "identity_mismatch", "device or firmware identity does not match");
    return;
  }

  if (strcmp(type, "ping") == 0) {
    sendAck(requestId, type);
    return;
  }
  if (strcmp(type, "get_status") == 0) {
    if (strcmp(command["device_id"].as<const char *>(), "*") == 0 &&
        strcmp(command["firmware_version"].as<const char *>(), "unknown") == 0) {
      // Late attachment must learn identity from the device itself. This is a
      // fresh message from the current boot, not a reset or a replay of seq 0.
      // Exact-identity status requests keep their existing ACK/status response.
      sendHello();
    }
    sendAck(requestId, type);
    sendStatus("status");
    return;
  }
  if (strcmp(type, "set_config") == 0) {
    if (deviceState != fm::DeviceState::IDLE) {
      sendError(requestId, type, "invalid_state", "set_config requires IDLE");
    } else if (!configValid(command)) {
      sendError(requestId, type, "invalid_config", "min_event_interval_us must be 50000 or 150000");
    } else {
      configuredMinEventIntervalUs = command["config"]["min_event_interval_us"].as<uint32_t>();
      sendAck(requestId, type);
    }
    return;
  }
  if (strcmp(type, "arm") == 0) {
    const char *incomingSession = command["session_id"];
    if (deviceState != fm::DeviceState::IDLE) {
      sendError(requestId, type, "invalid_state", "arm requires IDLE");
    } else if (!isUuid(incomingSession)) {
      sendError(requestId, type, "invalid_session", "session_id must be a UUID");
    } else if (!configValid(command)) {
      sendError(requestId, type, "invalid_config", "min_event_interval_us must be 50000 or 150000");
    } else {
      sessionId = incomingSession;
      configuredMinEventIntervalUs = command["config"]["min_event_interval_us"].as<uint32_t>();
      clearTrialCountersForArm();
      deviceState = fm::DeviceState::ARMED;
      sendAck(requestId, type);
      sendTrialArmed();
    }
    return;
  }
  if (strcmp(type, "start") == 0) {
    if (deviceState != fm::DeviceState::ARMED) {
      sendError(requestId, type, "invalid_state", "start requires ARMED");
    } else if (!sessionMatches(command)) {
      sendError(requestId, type, "session_mismatch", "session_id does not match the armed trial");
    } else {
      startTrial();
      sendAck(requestId, type);
      sendTrialStarted("ui");
    }
    return;
  }
  if (strcmp(type, "stop") == 0) {
    if (deviceState != fm::DeviceState::RECORDING) {
      sendError(requestId, type, "invalid_state", "stop requires RECORDING");
    } else if (!sessionMatches(command)) {
      sendError(requestId, type, "session_mismatch", "session_id does not match the active trial");
    } else {
      const bool reasonPresent = hasKey(command, "stop_reason");
      if (reasonPresent && !command["stop_reason"].is<const char *>()) {
        sendError(requestId, type, "invalid_stop_reason", "stop_reason must be text");
        return;
      }
      const char *reason = command["stop_reason"] | "ui";
      if (reason[0] == '\0' || strlen(reason) > 64) {
        sendError(requestId, type, "invalid_stop_reason", "stop_reason must contain 1 to 64 characters");
      } else {
        stopTrial(reason, "ui", requestId, true);
      }
    }
    return;
  }
  if (strcmp(type, "disarm") == 0) {
    if (deviceState != fm::DeviceState::ARMED && deviceState != fm::DeviceState::COMPLETE) {
      sendError(requestId, type, "invalid_state", "disarm requires ARMED or COMPLETE");
    } else if (!sessionMatches(command)) {
      sendError(requestId, type, "session_mismatch", "session_id does not match the trial");
    } else {
      deviceState = fm::DeviceState::IDLE;
      sessionId = "";
      clearTrialCountersForArm();
      sendAck(requestId, type);
    }
    return;
  }
  if (strcmp(type, "self_test") == 0) {
    if (deviceState != fm::DeviceState::IDLE && deviceState != fm::DeviceState::ARMED) {
      sendError(requestId, type, "invalid_state", "self_test requires IDLE or ARMED");
    } else {
      sendAck(requestId, type);
      sendSensorState(digitalRead(fm::SENSOR_GPIO));
    }
  }
}

void readSerialCommands() {
  while (Serial.available() > 0) {
    const char value = static_cast<char>(Serial.read());
    if (value == '\n') {
      if (serialLineOverflow) {
        sendError(nullptr, nullptr, "line_too_long", "serial command exceeded the input limit");
      } else if (serialLineLength > 0) {
        serialLine[serialLineLength] = '\0';
        processCommand(serialLine);
      }
      serialLineLength = 0;
      serialLineOverflow = false;
    } else if (value != '\r' && !serialLineOverflow) {
      if (serialLineLength + 1 < fm::SERIAL_LINE_CAPACITY) {
        serialLine[serialLineLength++] = value;
      } else {
        serialLineOverflow = true;
      }
    }
  }
}

void beginWarningPattern() {
  warningStartedMs = millis();
}

void updateButton() {
  const uint32_t now = millis();
  const bool currentRaw = digitalRead(fm::START_STOP_BUTTON_GPIO);
  if (currentRaw != rawButtonState) {
    rawButtonState = currentRaw;
    rawButtonChangedMs = now;
  }
  if (now - rawButtonChangedMs >= fm::BUTTON_DEBOUNCE_MS &&
      stableButtonState != rawButtonState) {
    stableButtonState = rawButtonState;
    if (stableButtonState == LOW) {
      buttonPressedMs = now;
      buttonHoldHandled = false;
    } else {
      const uint32_t heldMs = now - buttonPressedMs;
      if (!buttonHoldHandled && deviceState == fm::DeviceState::ARMED &&
          heldMs < fm::PHYSICAL_STOP_HOLD_MS) {
        startTrial();
        sendTrialStarted("physical_button");
      } else if (!buttonHoldHandled && deviceState == fm::DeviceState::IDLE) {
        sendError(nullptr, nullptr, "button_unarmed", "physical button cannot start an unarmed trial");
        beginWarningPattern();
      }
      buttonHoldHandled = true;
    }
  }
  if (stableButtonState == LOW && !buttonHoldHandled &&
      deviceState == fm::DeviceState::RECORDING &&
      now - buttonPressedMs >= fm::PHYSICAL_STOP_HOLD_MS) {
    buttonHoldHandled = true;
    stopTrial("physical_button", "physical_button", nullptr, false);
  }
}

void updateLeds() {
  const uint32_t now = millis();
  const fm::DeviceState state = deviceState;
  bool readyOn = false;
  if (state == fm::DeviceState::ARMED || state == fm::DeviceState::RECORDING) {
    readyOn = true;
  } else if (state == fm::DeviceState::IDLE || state == fm::DeviceState::COMPLETE) {
    readyOn = ((now / 500) % 2) == 0;
  } else if (state == fm::DeviceState::ERROR_STATE) {
    readyOn = ((now / 100) % 2) == 0;
  }
  digitalWrite(fm::READY_LED_GPIO, readyOn ? HIGH : LOW);
  digitalWrite(fm::REC_LED_GPIO, state == fm::DeviceState::RECORDING ? HIGH : LOW);

  const uint32_t warningElapsed = now - warningStartedMs;
  const bool warningActive = warningStartedMs != 0 && warningElapsed < fm::WARNING_PATTERN_MS;
  const bool warningOn = warningActive && warningElapsed < 600 && ((warningElapsed / 100) % 2) == 0;
  const bool eventOn = static_cast<int32_t>(eventLedUntilMs - now) > 0;
  digitalWrite(fm::EVENT_LED_GPIO, (warningOn || (!warningActive && eventOn)) ? HIGH : LOW);
}

const char *resetReasonName() {
  switch (esp_reset_reason()) {
    case ESP_RST_POWERON:
      return "power_on";
    case ESP_RST_SW:
      return "software";
    case ESP_RST_PANIC:
      return "panic";
    case ESP_RST_INT_WDT:
      return "interrupt_watchdog";
    case ESP_RST_TASK_WDT:
      return "task_watchdog";
    case ESP_RST_WDT:
      return "watchdog";
    case ESP_RST_DEEPSLEEP:
      return "deep_sleep";
    case ESP_RST_BROWNOUT:
      return "brownout";
    default:
      return "other";
  }
}

void establishIdentity() {
  char deviceBuffer[24];
  char bootBuffer[40];
  const uint64_t mac = ESP.getEfuseMac();
  snprintf(deviceBuffer, sizeof(deviceBuffer), "FM-S3-%012llX",
           static_cast<unsigned long long>(mac & 0xFFFFFFFFFFFFULL));
  snprintf(bootBuffer, sizeof(bootBuffer), "%08lX-%08lX-%08lX",
           static_cast<unsigned long>(esp_random()), static_cast<unsigned long>(esp_random()),
           static_cast<unsigned long>(millis()));
  deviceId = deviceBuffer;
  bootId = bootBuffer;
}

void sendHello() {
  JsonDocument message;
  beginMessage(message, "hello");
  message["state"] = fm::stateName(deviceState);
  message["reset_reason"] = resetReasonName();
  sendMessage(message);
}

bool memoryConfigurationValid() {
  return ESP.getFlashChipSize() == fm::EXPECTED_FLASH_BYTES && psramFound() &&
         esp_spiram_get_size() == fm::EXPECTED_PSRAM_BYTES;
}

}  // namespace

void setup() {
  const bool serialRxBufferValid =
      Serial.setRxBufferSize(fm::SERIAL_LINE_CAPACITY) >= fm::SERIAL_LINE_CAPACITY;
  const bool serialTxBufferValid =
      Serial.setTxBufferSize(fm::SERIAL_TX_CAPACITY) >= fm::SERIAL_TX_CAPACITY;
  Serial.begin(fm::SERIAL_BAUD);
  const uint32_t waitStartedMs = millis();
  while (!Serial && millis() - waitStartedMs < fm::SERIAL_WAIT_TIMEOUT_MS) {
    delay(10);
  }

  pinMode(fm::SENSOR_GPIO, INPUT_PULLUP);
  pinMode(fm::START_STOP_BUTTON_GPIO, INPUT_PULLUP);
  pinMode(fm::READY_LED_GPIO, OUTPUT);
  pinMode(fm::REC_LED_GPIO, OUTPUT);
  pinMode(fm::EVENT_LED_GPIO, OUTPUT);
  digitalWrite(fm::READY_LED_GPIO, LOW);
  digitalWrite(fm::REC_LED_GPIO, LOW);
  digitalWrite(fm::EVENT_LED_GPIO, LOW);

  establishIdentity();
  rawButtonState = digitalRead(fm::START_STOP_BUTTON_GPIO);
  stableButtonState = rawButtonState;
  rawButtonChangedMs = millis();
  lastReportedSensorState = digitalRead(fm::SENSOR_GPIO);
  attachInterrupt(digitalPinToInterrupt(fm::SENSOR_GPIO), onBeamBreak, RISING);

  const bool memoryValid = memoryConfigurationValid();
  deviceState = memoryValid && serialRxBufferValid && serialTxBufferValid
                    ? fm::DeviceState::IDLE : fm::DeviceState::ERROR_STATE;
  sendHello();
  if (!memoryValid) {
    sendError(nullptr, nullptr, "memory_configuration_mismatch",
              "expected 4 MB flash and 2 MB PSRAM; upload profile or hardware does not match");
  }
  if (!serialRxBufferValid) {
    sendError(nullptr, nullptr, "serial_rx_buffer_allocation_failed",
              "could not allocate the protocol-sized native USB receive queue");
  }
  if (!serialTxBufferValid) {
    sendError(nullptr, nullptr, "serial_tx_buffer_allocation_failed",
              "could not allocate the startup-sized native USB transmit queue");
  }
  sendSensorState(lastReportedSensorState);
  lastHeartbeatMs = millis();
}

void loop() {
  readSerialCommands();
  updateButton();
  drainEvents();

  const int sensorState = digitalRead(fm::SENSOR_GPIO);
  if (sensorState != lastReportedSensorState) {
    lastReportedSensorState = sensorState;
    sendSensorState(sensorState);
  }

  const uint32_t now = millis();
  if (now - lastHeartbeatMs >= fm::HEARTBEAT_INTERVAL_MS) {
    lastHeartbeatMs = now;
    sendStatus("heartbeat");
  }
  updateLeds();
  delay(1);
}
