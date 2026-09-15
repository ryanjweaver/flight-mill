"""Preserve bytes already queued when Windows reopens the reset-test device.

Only the physical reset recorder selects this backend. Normal serial opens keep
pySerial's behavior. No global patch or installed-package edit is performed.
The open/handle setup is adapted from pySerial 3.5 serialwin32.py; see
licenses/pyserial-BSD-3-Clause.txt. Reads, writes, settings, and normal close
remain pySerial's implementation. This module is imported only on Windows.
"""

# Copyright (c) 2001-2020 Chris Liechti <cliechti@gmx.net>
# SPDX-License-Identifier: BSD-3-Clause

import ctypes

import serial
from serial import SerialException, win32
from serial.serialwin32 import Serial


class WindowsResetSerial(Serial):
    """Open without PurgeComm, which can erase the real startup hello."""

    def open(self):
        if serial.VERSION != "3.5":
            raise SerialException("Reset capture backend requires reviewed pySerial 3.5")
        if self._port is None or self.is_open:
            raise SerialException("Reset serial port must be configured and closed before open")
        port = self.name
        if port.upper().startswith("COM") and port[3:].isdigit():
            port = "\\\\.\\" + port
        handle = win32.CreateFile(
            port, win32.GENERIC_READ | win32.GENERIC_WRITE, 0, None,
            win32.OPEN_EXISTING, win32.FILE_ATTRIBUTE_NORMAL | win32.FILE_FLAG_OVERLAPPED, 0,
        )
        if handle == win32.INVALID_HANDLE_VALUE:
            raise SerialException(f"Cannot open reset capture port {port}: {ctypes.WinError()}")
        self._port_handle = handle
        events = []
        saved_timeouts = False
        try:
            for attribute, manual_reset in (("_overlapped_read", 1), ("_overlapped_write", 0)):
                overlapped = win32.OVERLAPPED()
                event = win32.CreateEvent(None, manual_reset, 0, None)
                if not event:
                    raise SerialException(f"Cannot create reset capture event: {ctypes.WinError()}")
                events.append(event)
                overlapped.hEvent = event
                setattr(self, attribute, overlapped)
            if not win32.SetupComm(handle, 4096, 4096):
                raise SerialException(f"Cannot size reset capture queues: {ctypes.WinError()}")
            self._orgTimeouts = win32.COMMTIMEOUTS()
            if not win32.GetCommTimeouts(handle, ctypes.byref(self._orgTimeouts)):
                raise SerialException(f"Cannot read reset capture timeouts: {ctypes.WinError()}")
            saved_timeouts = True
            self._reconfigure_port()
            # Do not purge input/output or reset either buffer. Startup bytes may
            # already be queued before this handle is ready for its first read.
        except BaseException:
            if saved_timeouts:
                win32.SetCommTimeouts(handle, self._orgTimeouts)
            for event in events:
                win32.CloseHandle(event)
            win32.CloseHandle(handle)
            self._port_handle = None
            self._overlapped_read = self._overlapped_write = None
            raise
        self.is_open = True
