# Flight Mill

Windows acquisition software and ESP32-S3 firmware for a single-channel flight mill.

Flight Mill connects over USB, records accepted revolutions with device timestamps,
and saves raw CSV data with metadata, protocol logs and integrity information.
The desktop provides trial setup, a live trace, an archive and a simulator.

## Install on Windows

Download the **Windows installer ZIP** from [Releases](https://github.com/ryanjweaver/flight-mill/releases/tag/v0.2.0-preview.1).
The GitHub **Code > Download ZIP** button downloads source code, not the installer.

1. Use Windows 11 on an Intel/AMD 64-bit computer.
2. Extract the installer ZIP to a local folder outside OneDrive, such as
   `C:\FlightMillInstall`. Alternatively, enter `%LOCALAPPDATA%` in File Explorer,
   create `FlightMillSetup` there, and extract into it.
3. Run **Install-FlightMill.cmd** from the extracted folder.
4. Open the new **Flight Mill** Desktop shortcut.

Python and application libraries are bundled. Setup checks Windows components
and installs Microsoft's WebView2 Runtime if absent; that step needs internet.
The application and installer are currently unsigned.
See the [installation guide](docs/WINDOWS_DISTRIBUTION.md) for details.

## Connect and run a trial

1. Plug the compatible flight mill into USB, then open the app.
2. Select **USB serial device**, choose its port if necessary, and click
   **Connect selected source**. Expect ready/IDLE, a device ID, firmware
   **0.1.5-dev**, and **Protocol 1**.
3. Enter the Trial setup fields and optional **Trial duration**, then validate.
4. Confirm instrument settings, arm, start, and stop the trial. Wait for saving
   to complete before starting a new trial or using the saved bundle.

**Connect after USB reconnect** provides guided unplug/replug recovery.
Firmware 0.1.5-dev supports connecting after the device has already been plugged
in for some time. Older firmware 0.1.4-dev has a finite boot-hello buffer and may
require guided reconnect. Moving a tested 0.1.5 device to another computer does
not require flashing it again.

Recordings normally go to `Documents\FlightMill\Trials`, separate from the
installation. The output folder is configurable under Instrument settings.

## Prototype status

This is a research prototype preview, application **0.2.0.dev0** and firmware
**0.1.5-dev / Protocol 1**. The operator approved the redesigned GUI and ordinary,
late and guided USB connections. On a second Windows 11 x64 computer, the operator
reported successful installation, USB connection, and recording/saving of a
manual-spin test. Those reports are distinct from automated software tests.

The supported prototype uses an ESP32-S3 SuperMini **HW-747 V0.0.2 / FH4R2**,
4 MB QIO flash, 2 MB QSPI PSRAM and one OPB800W55Z sensor. See
[wiring and pinout](docs/WIRING_AND_PINOUT.md). Carrier PCB fabrication and
broader instrument validation are separate work; this repository does not claim
that a carrier is fabrication-ready.

## Develop

See the [developer guide](docs/DEVELOPER_GUIDE.md),
[firmware guide](firmware/README.md), [Protocol 1](protocol/SERIAL_PROTOCOL_V1.md),
and [data dictionary](docs/DATA_DICTIONARY.md).
Simulation is available for [practice without hardware](docs/SIMULATION_QUICK_START.md).

This repository starts from the accepted working-tree source, including the
GUI and connectivity fixes. Local recordings, machine-specific flash helpers,
private diagnostic history and development environments are not included.

## License

Copyright 2026 Ryan Weaver. [PolyForm Noncommercial License 1.0.0](LICENSE).
Academic research, teaching and other permitted noncommercial uses are free.
The license expressly permits educational institutions and public research
organizations regardless of funding source. Uses outside its permitted purposes
require a separate license from the owner.

This is source-available software with noncommercial terms. Third-party
components retain their original licenses; see [NOTICE](NOTICE) and
[dependency notices](app/packaging/THIRD_PARTY_NOTICES.txt).
Research data and publications are not relicensed merely by using Flight Mill.
