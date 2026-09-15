#pragma once

// Software-only hardware boundary for compiling the real firmware dispatcher.
// These stubs never enumerate, open, reset, or communicate with a USB device.
#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>

#define IRAM_ATTR
#define HIGH 1
#define LOW 0
#define INPUT_PULLUP 2
#define OUTPUT 3
#define RISING 4

using std::max;
using portMUX_TYPE = int;
constexpr portMUX_TYPE portMUX_INITIALIZER_UNLOCKED = 0;
inline void portENTER_CRITICAL(portMUX_TYPE *) {}
inline void portEXIT_CRITICAL(portMUX_TYPE *) {}
inline void portENTER_CRITICAL_ISR(portMUX_TYPE *) {}
inline void portEXIT_CRITICAL_ISR(portMUX_TYPE *) {}

class String : public std::string {
 public:
  using std::string::string;
  using std::string::operator=;
  bool isEmpty() const { return empty(); }
  bool operator==(const char *other) const { return compare(other) == 0; }
};

namespace native_hardware {
inline uint32_t nowMs = 3600000;
inline uint32_t digitalWrites = 0;
inline uint32_t delays = 0;
inline uint32_t pinModes = 0;
inline uint32_t interrupts = 0;
inline uint32_t serialBegins = 0;
inline uint32_t serialResizes = 0;
}  // namespace native_hardware

class NativeSerial {
 public:
  std::string output;
  size_t write(uint8_t value) {
    output.push_back(static_cast<char>(value));
    return 1;
  }
  size_t write(const uint8_t *values, size_t length) {
    output.append(reinterpret_cast<const char *>(values), length);
    return length;
  }
  size_t setRxBufferSize(size_t size) {
    ++native_hardware::serialResizes;
    return size;
  }
  size_t setTxBufferSize(size_t size) {
    ++native_hardware::serialResizes;
    return size;
  }
  void begin(uint32_t) { ++native_hardware::serialBegins; }
  explicit operator bool() const { return true; }
  int available() const { return 0; }
  int read() const { return -1; }
};
inline NativeSerial Serial;

class NativeEsp {
 public:
  uint64_t getEfuseMac() const { return 0x020000000001ULL; }
  uint32_t getFlashChipSize() const { return 4U * 1024U * 1024U; }
};
inline NativeEsp ESP;
inline bool psramFound() { return true; }
inline uint32_t millis() { return native_hardware::nowMs; }
inline void delay(uint32_t duration) {
  ++native_hardware::delays;
  native_hardware::nowMs += duration;
}
inline int digitalRead(uint8_t pin) { return pin == 4 ? LOW : HIGH; }
inline void digitalWrite(uint8_t, int) { ++native_hardware::digitalWrites; }
inline void pinMode(uint8_t, int) { ++native_hardware::pinModes; }
inline int digitalPinToInterrupt(uint8_t pin) { return pin; }
inline void attachInterrupt(int, void (*)(), int) { ++native_hardware::interrupts; }
