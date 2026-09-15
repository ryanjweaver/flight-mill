from uuid import UUID
from unittest.mock import Mock

import pytest

from hardware.validation.recording_reset_diagnostic import reset_matches
from hardware.validation import recording_reset_diagnostic as driver
from flightmill.protocol.models import (
    EventMessage,
    HelloMessage,
    StatusMessage,
    TrialStartedMessage,
    TrialStoppedMessage,
)


SESSION = UUID(int=1)
COMMON = dict(
    protocol="flightmill", protocol_version=1, device_id="FM-TEST", firmware_version="0.1.3-dev"
)


def transcript():
    return [
        HelloMessage(message_seq=0, boot_id="old", state="IDLE", reset_reason="other", **COMMON),
        TrialStartedMessage(
            message_seq=1, boot_id="old", session_id=SESSION, start_source="ui", **COMMON
        ),
        EventMessage(
            message_seq=2,
            boot_id="old",
            session_id=SESSION,
            event_n=1,
            event_us=1_000_000,
            dt_us=0,
            dropped_events=0,
            **COMMON,
        ),
        HelloMessage(message_seq=0, boot_id="new", state="IDLE", reset_reason="other", **COMMON),
        StatusMessage(
            message_seq=1,
            boot_id="new",
            state="IDLE",
            session_id=None,
            sensor_state=0,
            event_n=0,
            dropped_events=0,
            uptime_ms=300,
            **COMMON,
        ),
    ]


def matches(messages):
    return reset_matches(messages, SESSION, "old", "new", "FM-TEST")


def test_new_boot_without_clean_stop_passes():
    assert matches(transcript())


@pytest.mark.parametrize(
    ("index", "field", "value"),
    [
        (2, "event_n", 2),
        (2, "dt_us", 1),
        (2, "dropped_events", 1),
        (3, "boot_id", "old"),
        (3, "device_id", "different"),
        (3, "message_seq", 2),
        (4, "state", "RECORDING"),
        (4, "session_id", SESSION),
        (4, "event_n", 1),
        (4, "dropped_events", 1),
        (4, "sensor_state", 1),
    ],
)
def test_incorrect_reset_event_identity_or_post_reset_state_fails(index, field, value):
    messages = transcript()
    messages[index] = messages[index].model_copy(update={field: value})
    assert not matches(messages)


def test_missing_or_duplicate_required_frame_fails():
    for index in range(5):
        messages = transcript()
        assert not matches(messages[:index] + messages[index + 1 :])
    assert not matches(transcript()[:3] + [transcript()[2]] + transcript()[3:])


def test_clean_stop_cannot_be_credited_as_reset():
    messages = transcript()
    messages.insert(
        3,
        TrialStoppedMessage(
            message_seq=3,
            boot_id="old",
            session_id=SESSION,
            accepted_event_count=1,
            dropped_events=0,
            duration_us=2_000_000,
            stop_source="ui",
            stop_reason="test",
            **COMMON,
        ),
    )
    assert not matches(messages)


def test_unconfirmed_readiness_never_arms_or_starts(tmp_path, monkeypatch):
    firmware = tmp_path / "firmware.bin"
    firmware.write_bytes(b"test artifact, not firmware")
    monkeypatch.setattr(
        "sys.argv",
        [
            "recording_reset_diagnostic",
            "--port",
            "COM_TEST",
            "--expected-device-id",
            "FM-TEST",
            "--firmware-bin",
            str(firmware),
        ],
    )
    monkeypatch.setattr(driver, "default_output", lambda mode: tmp_path / "readiness")
    board = Mock()
    board.serial = None
    board.wait_for_hello.return_value = transcript()[0]
    board.get_status.return_value = transcript()[-1].model_copy(update={"boot_id": "old"})
    monkeypatch.setattr(driver, "Board", lambda port, evidence: board)
    monkeypatch.setattr(driver, "operator_action", lambda *args, **kwargs: False)
    assert driver.main() == 1
    board.arm.assert_not_called()
    board.start.assert_not_called()
