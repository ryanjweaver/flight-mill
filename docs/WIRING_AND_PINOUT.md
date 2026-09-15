# Wiring and pinout

## Sensor circuit

```text
3V3  -- 150 ohm -- red   (infrared LED anode)
GND  ------------- black (infrared LED cathode)
GPIO4------------- white (phototransistor collector / signal)
GND  ------------- green (phototransistor emitter)
3V3  -- 10 kohm ---+
                    +---- white/GPIO4 signal node
```

The 10 kohm pull-up is a required populated component. Firmware may also enable
`INPUT_PULLUP` as a fail-safe, but normal operation and validation do not depend
on the ESP32-S3's internal pull-up. The sensor connector and GPIO signal use
3.3 V only; 5 V must not reach any sensor-connector pin or GPIO.

Confirmed logic is beam clear `0`, beam blocked `1`, accepted edge `RISING`.

## ESP32-S3 SuperMini orientation and pins

V1 uses an 18-pin ESP32-S3 SuperMini `HW-747 V0.0.2` carrying an
`ESP32-S3FH4R2` SoC. Hold the board with component labels upright and USB-C at
the top:

| Left, top to bottom | Right, top to bottom |
|---|---|
| TX | 5V - carrier does not route this to the sensor |
| RX | GND |
| GPIO1 | 3V3 |
| GPIO2 | GPIO13 |
| GPIO3 - reserved/unused strapping pin | GPIO12 |
| GPIO4 - SENSOR | GPIO11 |
| GPIO5 - START/STOP | GPIO10 - EVENT |
| GPIO6 | GPIO9 - REC |
| GPIO7 | GPIO8 - READY |

The carrier's PWR indicator is powered from 3V3 rather than a firmware GPIO.
GPIO4 and GPIO5 are unrestricted general-purpose pins in Espressif's ESP32-S3
GPIO table. GPIO8 through GPIO10 are also unrestricted there. The selected
allocation avoids exposed GPIO3, which is a strapping pin.

GPIO5 uses an active-low START/STOP button. READY, REC and EVENT outputs are
GPIO8, GPIO9 and GPIO10 respectively. Use suitable current-limiting resistors
for external LEDs. This describes the tested prototype, not a fabrication-ready
carrier PCB.

## Mechanical status

The purchased unit measures approximately 23.5 x 18 mm for the PCB and
25 x 18 mm including the USB-C connector. The pin rows measure approximately
15.5 mm apart center-to-center, with 2.54 mm along-row pitch and a 20.32 mm
first-to-ninth pin-center span. Socket/header dimensions, detailed USB cable
clearance, component height, control access, and the keepout around the visible
bottom-edge ceramic antenna remain open. The SuperMini will carry two 1x9 male
headers that plug downward into female carrier sockets.
