"""Bounded USB serial I/O; no device is opened by port enumeration.

The worker owns the OS handle. The service alone frames/parses/records bytes.
pySerial 3.5 is pinned. Windows uses the accepted no-PurgeComm reader so that
opening does not deliberately discard startup evidence. Driver line glitches
remain possible; opening is never advertised as necessarily non-resetting.
"""
from __future__ import annotations

import os
import threading
from collections import deque
from collections.abc import Callable
from typing import Any

from flightmill.acquisition.serial_errors import SerialPortNotReadyError


def discover_ports(enumerator: Callable | None = None) -> list[dict[str, Any]]:
    if enumerator is None:
        from serial.tools.list_ports import comports
        enumerator = comports
    ports = []
    for port in enumerator():
        vid, pid = getattr(port, "vid", None), getattr(port, "pid", None)
        ports.append({"port": port.device, "description": getattr(port, "description", None),
                      "vid": vid, "pid": pid, "serial_number": getattr(port, "serial_number", None),
                      "candidate": (vid, pid) == (0x303A, 0x1001)})
    return sorted(ports, key=lambda item: item["port"])


def select_port(ports: list[dict], manual: str | None = None) -> str:
    if manual:
        # An explicit local OS port is allowed even when enumeration lacks attributes.
        if "://" in manual or not manual.strip():
            raise ValueError("Select a local serial port, not a URL")
        return manual.strip()
    candidates = [item["port"] for item in ports if item["candidate"]]
    if len(candidates) != 1:
        raise ValueError("Select a port manually: no unique USB candidate was found")
    return candidates[0]


def _serial_factory():
    if os.name == "nt":
        from flightmill.acquisition.windows_serial import WindowsResetSerial
        return WindowsResetSerial()
    from serial import Serial
    return Serial()


class SerialTransport:
    """One reader, bounded queues, finite read/write waits, no automatic retry."""

    _owners: set[str] = set()
    _owners_lock = threading.Lock()
    max_queue_bytes = 1_048_576

    def __init__(self, port: str, *, serial_factory: Callable = _serial_factory) -> None:
        self.port = port
        self._factory = serial_factory
        self.connected = False
        self.opening = False
        self.loss_reason: str | None = None
        self.port_not_ready = False
        self._lock = threading.Lock()
        self._incoming: deque[bytes] = deque()
        self._outgoing: deque[bytes] = deque()
        self._queue_bytes = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._serial = None

    def open(self) -> None:
        if self._thread and self._thread.is_alive():
            raise ValueError("Serial worker still owns this port")
        key = self.port.casefold()
        with self._owners_lock:
            if key in self._owners:
                raise ValueError("This serial port is already owned by an acquisition service")
            self._owners.add(key)
        self.loss_reason = None
        self.port_not_ready = False
        self._incoming.clear()
        self._outgoing.clear()
        self._queue_bytes = 0
        self._stop.clear()
        self.opening = True
        self._thread = threading.Thread(target=self._run, name="flightmill-serial", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        endpoint = None
        try:
            endpoint = self._factory()
            self._serial = endpoint
            endpoint.port = self.port
            endpoint.baudrate = 115200
            endpoint.timeout = 0.05
            endpoint.write_timeout = 0.25
            endpoint.rtscts = endpoint.dsrdtr = endpoint.xonxoff = False
            # Match the accepted passive-open diagnostics. Do not toggle/reset.
            endpoint.dtr = endpoint.rts = False
            try:
                endpoint.open()
            except SerialPortNotReadyError:
                # Only this pre-handle failure may be deferred by guided reconnect.
                # Errors from configuration, DTR, reads or writes are never eligible.
                self.port_not_ready = True
                raise
            endpoint.dtr = True  # Accepted Windows CDC attachment; RTS stays low.
            self.connected = True
            self.opening = False
            while not self._stop.is_set():
                with self._lock:
                    command = self._outgoing.popleft() if self._outgoing else None
                if command is not None and endpoint.write(command) != len(command):
                    raise OSError("Serial command write was partial; outcome is uncertain")
                raw = endpoint.read(min(16384, max(1, endpoint.in_waiting)))
                if raw:
                    with self._lock:
                        if self._queue_bytes + len(raw) > self.max_queue_bytes:
                            raise OSError("Serial receive queue overflow; event loss is unresolved")
                        self._incoming.append(raw)
                        self._queue_bytes += len(raw)
                # A timeout returning b'' is ordinary silence, never a disconnect.
        except Exception as exc:
            if not self._stop.is_set():
                self.loss_reason = f"Serial transport error: {exc}"
        finally:
            self.connected = self.opening = False
            if endpoint is not None:
                try:
                    endpoint.close()
                except Exception as exc:
                    self.loss_reason = self.loss_reason or f"Serial close error: {exc}"
            self._serial = None
            with self._owners_lock:
                self._owners.discard(self.port.casefold())

    def write(self, raw: bytes) -> None:
        if not self.connected:
            raise ConnectionError("Serial port is not open")
        with self._lock:
            if len(self._outgoing) >= 32 or len(raw) > 65536:
                raise OSError("Serial command queue limit exceeded")
            self._outgoing.append(bytes(raw))

    def read(self) -> bytes:
        with self._lock:
            if not self._incoming:
                return b""
            raw = self._incoming.popleft()
            self._queue_bytes -= len(raw)
            return raw

    def tick(self) -> None:
        pass

    def close(self) -> None:
        self._stop.set()
        self.connected = False
        endpoint = self._serial
        if endpoint is not None:
            try:
                endpoint.cancel_read()
            except (AttributeError, OSError):
                pass
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=0.6)
            if self._thread.is_alive():
                self.loss_reason = "Serial shutdown unconfirmed; worker retains port ownership"
        self.opening = False
