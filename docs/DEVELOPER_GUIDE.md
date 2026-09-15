# Developer guide

## Windows environment

Use 64-bit Python 3.12. The Windows build was verified with Python 3.12.14.
Run from the repository root:

```powershell
py -3.12 -m venv .venv
.venv/Scripts/python.exe -m pip install -r app/packaging/requirements-windows.lock
.venv/Scripts/python.exe -m pip install --no-deps -e .
$env:PYTHONPATH = "app/src;."
```

The lock pins the Windows build/test dependencies. It contains version pins,
not artifact hashes. WebView2 and .NET Framework 4.8 or later are required for
the Windows desktop renderer. The packaged installer checks these components.

## Automated checks

These commands do not drive the UI or open a USB device:

```powershell
$env:PYTHONPATH = "app/src;.;app/tests"
.venv/Scripts/python.exe -m pytest app/tests -q
node --test app/src/flightmill/ui/tests/*.test.cjs
powershell -NoProfile -ExecutionPolicy Bypass -File app/packaging/windows/Test-InstallerHelpers.ps1
```

Node is needed only for the JavaScript regression tests. Native firmware tests
compile the actual command handler with test stubs and ArduinoJson 7.4.3 headers.
They need `g++` or `clang++` (`CXX` may override it) and headers from PlatformIO
or `FLIGHTMILL_ARDUINOJSON_INCLUDE`; otherwise those tests skip. Tests use synthetic
inputs. A private board-photo hash check and machine-specific flashing helper
are excluded from this public source tree.

## Run from source

After installing the environment, the operator can run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/start_gui.ps1
```

Use `-Browser` for the local browser interface. The source launcher defaults to
`data/simulations`; select the intended output directory explicitly for physical
trials. The shared acquisition service supports USB serial and simulation.

## Build the Windows distribution

```powershell
.venv/Scripts/python.exe scripts/build_windows_package.py
```

This builds a fresh PyInstaller directory, installer ZIP, manifest and checksum
under `dist/`. The target is Windows 11 x64. Keep the complete extracted package
together. The builder checks dependency pins and includes the project license,
third-party notices and build identity. It neither launches the application nor
connects to hardware. The installer invokes the executable's noninteractive
`--self-check` before creating shortcuts.

## Firmware

See [firmware/README.md](../firmware/README.md). Building firmware is separate
from flashing a physical device. A new binary requires its own operator checks;
a successful compile is not physical instrument validation.

## Project layout and testing policy

- `app/src/flightmill`: acquisition, protocol, storage, desktop, API and GUI.
- `simulator`: deterministic device model.
- `firmware`: ESP32-S3 firmware and native test stubs.
- `protocol`: wire contract and schemas.
- `sample_data`: synthetic CSV contract fixtures.
- `hardware/validation`: diagnostic helper source, without private captures.
- `app/packaging` and `scripts/build_windows_package.py`: Windows distribution.

Follow [AGENTS.md](../AGENTS.md): visual and interactive Flight Mill checks belong
to the operator. Automated test passes must not be reported as operator
observations. Preserve strict hello/identity and correlated IDLE validation,
single serial ownership, cancellation, stable port selection and raw CSV format.
