# Windows installation

Download **FlightMill-Windows11-x64-20260915-public1.zip** from
[the preview release](https://github.com/ryanjweaver/flight-mill/releases/tag/v0.2.0-preview.1).
Use the installer asset; GitHub's automatically generated source ZIP does not
contain the application executable.

## Where to extract

Use **Extract All** into a local folder outside OneDrive or another cloud-sync
folder, for example **C:\FlightMillInstall**. If that location is not writable,
enter **%LOCALAPPDATA%** in File Explorer's address bar, create **FlightMillSetup**
there, and extract into it. Keep all files together.

Avoid a OneDrive-backed Desktop or Documents folder for extraction. The ZIP may
be stored anywhere; the extracted folder is what the installer checks.
If setup reports **Package paths must not contain links or junctions**, extract
a fresh copy into the local folder above. OneDrive placeholders can carry the
reparse-point metadata that this check rejects. The installed Desktop shortcut
may stay on Desktop, including a OneDrive-backed Desktop.

## Install

1. Use Windows 11 on Intel/AMD x64.
2. Run **Install-FlightMill.cmd** from the extracted directory.
3. Wait for **Flight Mill installed successfully** and the dependency check.
4. Open the newly created Flight Mill shortcut.

Python, application libraries, native bridge DLLs and GUI assets are bundled.
The installer checks .NET Framework 4.8 or later and WebView2 registration. If
WebView2 is missing it downloads Microsoft's installer, verifies its signature,
installs it and rechecks registration. Internet is needed for that download.
A damaged/missing .NET Windows component produces repair guidance.

Setup verifies package file paths, sizes and SHA-256 hashes. It installs per-user
under `%LOCALAPPDATA%\Programs\FlightMill\<package-id>` and runs a noninteractive
self-check before creating shortcuts. Existing builds and recordings remain in
place. Re-running an identical verified package can reuse its installation.
The application does not open automatically during installation.

The Flight Mill executable and wrapper are unsigned. If your organization blocks
them, retain the message and consult IT. Installation logs are under
`%LOCALAPPDATA%\FlightMill\InstallLogs`.

## Connect

Plug in the supported device and open the new shortcut. Select USB serial device
and click **Connect selected source** once. Expect ready/IDLE with your device ID,
firmware **0.1.5-dev** and **Protocol 1**, without handshake errors. Windows may
assign a different COM port; use Find serial ports if necessary.

Firmware 0.1.5 supports ordinary connection even when the device was plugged in
before the app opened. **Connect after USB reconnect** remains a guided recovery
path. Moving an already working 0.1.5 device does not require reflashing.

Recordings normally use your actual `Documents\FlightMill\Trials` folder,
including Windows folder redirection. Instrument settings can choose another
folder. Keep one application in control of a device and recording directory.

## Verification and scope

The operator approved the GUI and three connection workflows on the development
computer. On another Windows 11 x64 computer, the operator reported installation,
Desktop shortcut, USB connection and a saved manual-spin trial after moving the
installer outside OneDrive. Exact counts and both timed reconnect scenarios
were not independently reported for that second computer.

The public package adds licensing and installation documentation to that tested
application. The executable, dependencies, GUI and installer behavior remain
unchanged. A working installation does not need an update for this revision.
Automated package integrity, bundled import/native bridge and mocked prerequisite
checks are distinct from the operator's observations.

See [the quick-start text](WINDOWS_PACKAGE_QUICK_START.txt) for grouped checks
on additional computers, and [the developer guide](DEVELOPER_GUIDE.md) to build.

## License

Copyright 2026 Ryan Weaver. [PolyForm Noncommercial License 1.0.0](../LICENSE),
with scope in [NOTICE](../NOTICE). Third-party components retain their own terms.
The release contains project, Python and dependency notices.
