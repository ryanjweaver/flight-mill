# Initial public source

This repository starts from the approved September 15, 2026 working tree,
including uncommitted GUI and serial changes. The previous base commit alone
does not contain those repairs.

The application, GUI, simulator and firmware runtime files are unchanged from
that accepted source. The public test suite uses synthetic identities and a
simulator-generated protocol session in place of a private hardware capture.
It retains hardware contract assertions but omits a private photograph hash.
Private recordings, diagnostic history, machine-specific flashing helpers,
caches and local environments are excluded. This is a fresh public history;
the owner's original repository and its evidence remain intact.

LICENSE is the unmodified official PolyForm Noncommercial 1.0.0 text from
https://github.com/polyformproject/polyform-licenses/blob/1.0.0/PolyForm-Noncommercial-1.0.0.md
(SHA-256 c0ea4a896d2c8c394b29f9427589996db826cd501c512279ff0ed3ef48fabbe5).
NOTICE identifies Ryan Weaver as copyright owner and preserves third-party terms.

The Windows public1 package is a license/documentation revision of the
operator-tested package. Executable, libraries, GUI assets and installer scripts
are unchanged. Its manifest and checksum identify the new distribution.

## Noninteractive release checks

- Public source: 520 pytest tests passed, including 22 native firmware tests;
  zero failures, errors or skips. Native tests used ArduinoJson 7.4.3 headers
  and a C++ compiler, without flashing a device.
- JavaScript controls: 33 Node tests passed.
- Windows installer: 64 mocked PowerShell checks passed; 28 package/preflight
  Python tests passed, and the frozen executable self-check passed.
- Source wheel built with project/pySerial licenses, NOTICE, simulator and GUI
  assets included. The current dependency pins are recorded in the Windows lock.
- Release-content scan and documentation link checks passed. All 44 runtime,
  GUI, simulator and firmware production files match the approved working tree.

These checks operate on files, mocks and command-line code. They do not replace
operator UI or physical instrument acceptance.
