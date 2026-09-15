"""Read-only enumeration gate for one operator-requested USB unplug/replug.

This helper never opens a port or sends a reset command. A matched return only
allows the acquisition service to begin its existing strict boot handshake.
"""
from __future__ import annotations

import time
from collections.abc import Callable


class USBReconnectWaiter:
    timeout_s = 60.0
    port_ready_timeout_s = 2.0
    max_open_attempts = 20

    def __init__(self, ports: list[dict], selected_port: str,
                 *, clock: Callable[[], float] = time.monotonic) -> None:
        selected = [port for port in ports if port["port"].casefold() == selected_port.casefold()]
        if len(selected) != 1:
            raise ValueError("Plug in the prototype and select its current port before preparing USB reconnect")
        port = selected[0]
        if ((port.get("vid"), port.get("pid")) != (0x303A, 0x1001)
                or not port.get("serial_number")):
            raise ValueError("USB reconnect requires an Espressif USB device with a unique serial number")
        self.identity = {key: port[key] for key in ("vid", "pid", "serial_number")}
        if len(self._matches(ports)) != 1:
            raise ValueError("USB device identity is ambiguous; select one uniquely identified prototype")
        self.phase = "waiting_for_unplug"
        self._clock = clock
        self._deadline = clock() + self.timeout_s
        self._port_deadline: float | None = None
        self.open_attempts = 0

    def _matches(self, ports: list[dict]) -> list[dict]:
        return [port for port in ports if all(port.get(key) == value for key, value in self.identity.items())]

    def poll(self, ports: list[dict]) -> str | None:
        if self._clock() >= self._deadline:
            raise TimeoutError("USB reconnect timed out after 60 seconds; no connection was opened")
        if (self._port_deadline is not None
                and (self._clock() >= self._port_deadline or self.open_attempts >= self.max_open_attempts)):
            raise TimeoutError("USB returned, but its port was not available within two seconds; prepare USB reconnect again")
        matches = self._matches(ports)
        if len(matches) > 1:
            raise ValueError("USB device identity became ambiguous; no connection was opened")
        if self.phase == "waiting_for_unplug":
            if not matches:
                self.phase = "waiting_for_replug"
            return None
        if not matches:
            return None
        if self._port_deadline is None:
            self._port_deadline = self._clock() + self.port_ready_timeout_s
        self.phase = "waiting_for_port"
        self.open_attempts += 1
        return matches[0]["port"]
