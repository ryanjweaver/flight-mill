"""Generate synthetic serial replay data; never inspect or open a physical port.

Run with PYTHONPATH=app/src;. from the repository root. The output comes solely
from SimulatedDevice and the explicit commands/clock advances below.
"""
from pathlib import Path
from uuid import UUID

from flightmill.protocol.models import (
    ArmCommand, DisarmCommand, GetStatusCommand, StartCommand, StopCommand,
    serialize_line,
)
from simulator.simulated_device import SimulatedDevice


def build_frames() -> bytes:
    now = [0.0]
    device = SimulatedDevice(device_id="FM-MOCK-SYNTHETIC", clock=lambda: now[0])
    # Exercise the host's supported firmware contract with an explicit test label.
    device.firmware_version = "0.1.5-synthetic"
    session = UUID("20000000-0000-4000-8000-000000000001")
    frames = [serialize_line(device.boot())]

    def send(command_class, request_number, **values):
        command = command_class(
            protocol="flightmill", protocol_version=1,
            device_id=device.device_id, firmware_version=device.firmware_version,
            request_id=UUID(int=request_number), **values,
        )
        frames.append(serialize_line(command))
        frames.extend(serialize_line(message) for message in device.process(command))

    send(GetStatusCommand, 1)
    send(ArmCommand, 2, session_id=session)
    send(StartCommand, 3, session_id=session)
    now[0] = 0.026287
    send(StopCommand, 4, session_id=session, stop_reason="user_stop")
    send(DisarmCommand, 5, session_id=session)
    return b"".join(frames)


if __name__ == "__main__":
    Path(__file__).with_name("serial_zero_event_session.jsonl").write_bytes(build_frames())
