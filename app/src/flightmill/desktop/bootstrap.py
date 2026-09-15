"""Small standard-library launch preflight, safe even under an unsupported Python."""
from __future__ import annotations

import os
import struct
import sys


def preflight():
    if sys.version_info[:2] != (3, 12) or struct.calcsize('P') != 8:
        raise RuntimeError('Flight Mill requires 64-bit Python 3.12. Select the configured '
                           'environment with -PythonPath; see docs/CHECKPOINT_04_WINDOWS_GUIDE.md.')
    from importlib.metadata import PackageNotFoundError, version
    pins = {'pydantic': '2.13.4', 'fastapi': '0.116.1', 'jinja2': '3.1.6',
            'pyserial': '3.5', 'pywebview': '6.0', 'uvicorn': '0.35.0', 'websockets': '15.0.1'}
    for name, expected in pins.items():
        try:
            actual = version(name)
        except PackageNotFoundError:
            raise RuntimeError('Missing dependency: ' + name + '==' + expected + '. Select the '
                               'configured Python 3.12 environment or install the pinned app extras.') from None
        if actual != expected:
            raise RuntimeError('Unsupported dependency: ' + name + '==' + actual + '; expected '
                               + expected + '. Use the configured pinned environment.')


def main():
    try:
        preflight()
        from flightmill.desktop.launcher import main as launch
        launch()
    except (RuntimeError, ImportError, OSError) as exc:
        message = 'Flight Mill could not start. ' + str(exc)
        if sys.stderr is not None:
            print(message, file=sys.stderr)
        elif os.name == 'nt':
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, message, 'Flight Mill launch error', 0x10)
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
