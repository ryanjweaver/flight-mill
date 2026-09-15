# Flight Mill V1 product requirements

This document summarizes the V1 design requirements. Historical verification
records remain with the project owner; current release scope is in README.md.

## Product boundary

V1 is one OPB800W55Z sensor and one replaceable 18-pin ESP32-S3 SuperMini
`HW-747 V0.0.2` per USB-connected device. The board carries an
`ESP32-S3FH4R2` with 4 MB Quad-SPI flash and 2 MB Quad-SPI PSRAM. Routine use is
Windows-first through one shared
acquisition service exposed to a desktop shell, a loopback-only browser UI, and
a diagnostic CLI. Wireless networking, cloud services, accounts, SD logging,
and multiple channels or devices per application instance are out of scope.

## Electrical and mechanical invariants

- The carrier uses the development board's regulated 3.3 V rail.
- The sensor connector never receives 5 V.
- Red is the infrared LED anode fed through 150 ohms, 4% tolerance or better
  (D-012); black is its cathode to
  ground; white is the collector/signal connected to GPIO4; green is the
  phototransistor emitter to ground.
- A populated 10 kohm, 1% pull-up connects white/GPIO4 to 3.3 V.
- The development board remains replaceable through two soldered 1x9 male
  headers mating downward into two 1x9 female carrier sockets.
- The purchased board measures approximately 23.5 x 18 mm at the PCB and
  25 x 18 mm including its USB-C connector, with 15.5 mm center-to-center pin
  row spacing and 2.54 mm along-row pitch.
- GPIO4 is SENSOR, GPIO5 is START/STOP, GPIO8 is READY, GPIO9 is REC, and
  GPIO10 is EVENT. PWR is hardware-driven from the regulated supply.
- Exact mating header/socket part numbers, installed height, keepouts, connector
  access, and onboard GPIO loading must be verified before layout or fabrication
  output.

## Acquisition invariants

- Clear is 0, blocked is 1, and accepted interruptions are rising edges.
- Events are trial data only while RECORDING.
- The default minimum accepted interval is 150,000 microseconds (D-011,
  owner-requested 2026-09-10); explicit 50,000 remains supported for legacy
  compatibility. Other values remain gated. Values below the configured
  interval are rejected; equality is accepted. Validate the 400 rpm limit
  (one flag/turn) against the actual radius and required flight-speed range.
- The default arm radius is 0.10 m; it is configurable and recorded.
- Firmware reports integer event/timing data. The host owns floating-point
  calculations, metadata, file names, and durable storage.
- Unusual accepted events are retained. Analysis filters never rewrite raw
  acquisition data.

## Trial and data integrity

- Host commands have unique request identifiers and correlated acknowledgments
  or errors.
- Session and boot identity gate every recorded event.
- Reset, disconnect, malformed lines, duplicates, gaps, and nonmonotonic events
  are surfaced and logged.
- Existing raw, metadata, or protocol files are never overwritten or silently
  renamed.
- A clean stop produces final files atomically. A failure preserves partial data
  and marks the trial incomplete.
- A zero-event trial is valid and produces a header-only raw CSV plus complete
  metadata.

## Release truthfulness

Simulation, software tests, breadboard evidence, PCB design review, and
fabricated-board validation are distinct evidence classes. No PCB or instrument
is hardware-validated until recorded physical tests pass.
