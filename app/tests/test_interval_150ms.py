"""Non-physical regression evidence for the owner-requested interval change."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from flightmill.constants import DEFAULT_MIN_EVENT_INTERVAL_US, SUPPORTED_MIN_EVENT_INTERVAL_US
from flightmill.protocol.models import AcquisitionConfig, ArmCommand, StartCommand
from simulator.simulated_device import SESSION_ID, SimulatedDevice, command_fields


def recording_device(interval: int) -> SimulatedDevice:
    device = SimulatedDevice()
    device.boot()
    device.process(
        ArmCommand(
            session_id=SESSION_ID,
            config=AcquisitionConfig(min_event_interval_us=interval),
            **command_fields(1),
        )
    )
    device.process(StartCommand(session_id=SESSION_ID, **command_fields(2)))
    return device


def test_new_default_and_explicit_legacy_support() -> None:
    assert DEFAULT_MIN_EVENT_INTERVAL_US == 150_000
    assert AcquisitionConfig().min_event_interval_us == 150_000
    assert SUPPORTED_MIN_EVENT_INTERVAL_US == {50_000, 150_000}
    assert AcquisitionConfig(min_event_interval_us=50_000).min_event_interval_us == 50_000


@pytest.mark.parametrize("interval", [50_000, 150_000])
def test_below_exact_and_above_boundary_without_extending_rejection(interval: int) -> None:
    device = recording_device(interval)
    assert device.emit_event(0).dt_us == 0
    assert device.emit_event(interval - 1) is None
    boundary = device.emit_event(interval)
    assert (boundary.event_n, boundary.dt_us) == (2, interval)
    above = device.emit_event(interval * 2 + 1)
    assert (above.event_n, above.dt_us) == (3, interval + 1)


@pytest.mark.parametrize("interval", [0, 49_999, 50_001, 100_000, 149_999, 150_001])
def test_unapproved_intervals_remain_invalid(interval: int) -> None:
    with pytest.raises(ValidationError):
        AcquisitionConfig(min_event_interval_us=interval)


def test_replay_of_fourteen_previously_accepted_timestamps_yields_ten() -> None:
    # Recorded 0.1.2-dev timestamps: this replay cannot recover rejected raw
    # edges, and does not substitute for a physical rerun on 0.1.3-dev.
    timestamps = [
        95850766,
        95905499,
        97737181,
        98804126,
        100223896,
        100274925,
        101946157,
        103055587,
        104537301,
        104590500,
        106365659,
        107700360,
        109638446,
        109716329,
    ]
    device = recording_device(150_000)
    accepted = [
        message for timestamp in timestamps if (message := device.emit_event(timestamp)) is not None
    ]
    assert [message.event_n for message in accepted] == list(range(1, 11))
    assert accepted[0].dt_us == 0


def test_schemas_support_both_intervals_with_new_default() -> None:
    root = Path(__file__).parents[2]
    serial = json.loads((root / "protocol/serial_protocol_v1.schema.json").read_text())
    metadata = json.loads((root / "protocol/trial_metadata_v1.schema.json").read_text())
    for schema in (
        serial["$defs"]["config"]["properties"]["min_event_interval_us"],
        metadata["properties"]["min_event_interval_us"],
    ):
        assert schema["enum"] == [50_000, 150_000]
        assert schema["default"] == 150_000
