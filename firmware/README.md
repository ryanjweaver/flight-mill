# Flight Mill V1 firmware

This PlatformIO/Arduino firmware implements the frozen serial protocol for the
18-pin ESP32-S3 SuperMini `HW-747 V0.0.2` and its selected logical pin map.

## Safety and behavior contracts

- SENSOR is GPIO4 with `INPUT_PULLUP`; the mandatory physical 10 kohm pull-up
  remains part of the carrier circuit.
- Clear is 0, blocked is 1, and the interrupt edge is `RISING`.
- Firmware 0.1.5-dev defaults to a 150,000 microsecond minimum event interval
  (owner-requested decision D-011). Explicit 50,000 remains supported for
  compatibility; no other value is accepted. ARM's requested value is applied
  and echoed in `trial_armed` rather than silently replaced by the default.
- Reject intervals strictly below the configured minimum since the last
  accepted edge; equality is accepted, rejected edges do not restart the
  interval, and the first event has dt=0. This is not a blocking delay.
- At one flag/revolution, 150 ms limits lossless counting to 400 rpm. At the
  default 0.10 m insect-path radius that is about 4.19 m/s. Faster rotations
  can be undercounted. Species/radius suitability and physical acceptance are
  separate from a successful build or software replay.
- Native USB CDC is assigned a receive queue at least as large as the 768-byte
  protocol line limit before USB serial starts. The framework's 256-byte
  default is too small for a valid `arm` frame with UUIDs and configuration.
- Prototype firmware 0.1.4-dev also allocates an 8,192-byte transmit queue before
  USB starts. The reviewed Arduino 2.0.17 HWCDC default is 256 bytes and its
  disconnected-write policy evicts older bytes. The healthy boot hello plus
  initial sensor-state message occupy 442 bytes on this prototype; a missing
  186-byte prefix matches the observed 256-byte startup tail. This larger queue
  provides finite room for startup and idle telemetry while the app attaches.
  Allocation failure puts the device in ERROR. Firmware 0.1.4 passed its dated
  guided connection checks; its finite queue alone cannot support arbitrary
  late attachment.
- Firmware 0.1.5-dev adds on-demand discovery: a validated wildcard `get_status`
  emits a fresh device hello, correlated ACK and status, with the current boot
  and advancing sequence. It never resets the board or changes a trial to make
  connection succeed. Exact-identity status queries are unchanged. This enables
  plug in, open app, then ordinary Connect even after the startup hello is gone.
  The host still requires genuine identity and fresh correlated IDLE/no-session
  status, and refuses an unowned active/terminal trial. See
  [Protocol 1 discovery](../protocol/SERIAL_PROTOCOL_V1.md).
  The operator accepted delayed ordinary Connect, ordinary reconnect without
  unplugging, and guided reconnect on September 15, 2026. Read-only logs confirm
  same-boot ordinary discovery and a new boot after guided unplug/replug. The underlying dated records remain with the project owner.
- The interrupt routine timestamps, filters, counts, and pushes to a ring
  buffer. JSON, USB writes, and LED operations run only in the main loop.
- GPIO5 is the active-low START/STOP button. A short press starts only from
  ARMED; a 1,500 ms hold stops only from RECORDING.
- READY on GPIO8 blinks slowly while unarmed and is solid while ARMED or
  RECORDING. REC on GPIO9 is solid only while RECORDING. EVENT on GPIO10 pulses
  for an accepted event and uses three fast flashes for a button warning.
- PWR is a separately wired hardware indicator and is not firmware-controlled.

## Build status

The current source identifies itself as **0.1.5-dev / Protocol 1**. The operator
accepted the prototype installation and delayed ordinary Connect, reconnect
without unplugging, and guided unplug/replug on September 15, 2026.

This repository includes firmware source. The Windows app installer does not
flash firmware. An already working 0.1.5 device needs no flash when moved to
another computer. Build and validate a new binary before using it on a different
board; the configuration below is specific to the supported FH4R2 prototype.

Install PlatformIO Core 6.1.19 in the development environment before building.
Platform and ArduinoJson versions are pinned in `platformio.ini`.

`supermini_hw747_v002` uses the photographed `FH4R2` silicon marking: 4 MB
in-package Quad-SPI flash and 2 MB in-package Quad-SPI PSRAM. PlatformIO's
generic ESP32-S3 DevKitC definition supplies the common ESP32-S3 framework,
while the project overrides its memory settings and uses the standard 4 MB
partition table to match that silicon.

The boot gate checks physical PSRAM capacity with ESP-IDF's
`esp_spiram_get_size()`. Arduino's `ESP.getPsramSize()` reports heap-usable
bytes after allocator overhead and is not an exact package-capacity query.

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_firmware.ps1
```

The wrapper uses a temporary drive mapping because the ESP32 compiler can
exceed Windows process-path limits when this repository is stored in a deeply
nested directory. It removes the mapping after the build.

The generated output remains a development build. Before creating release
`.bin`, `.elf`, map, and checksum artifacts, flash the photographed board and
record successful boot, 2 MB PSRAM detection, USB serial, protocol-conformance,
button, LED, and sensor-input tests.

After a clean build, upload only to a positively identified port and provide
the expected binary hash so the exact image is checked before and after the
verified write:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\flash_firmware.ps1 -Port COM3 -ExpectedSha256 <64-character-sha256>
```

The port and hash are required operator inputs for a controlled run; `COM3` is
only an example and must not be assumed for another connection.
