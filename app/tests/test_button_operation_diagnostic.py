from uuid import UUID

from hardware.validation.button_operation_diagnostic import physical_transitions_match
from flightmill.protocol.models import TrialStartedMessage, TrialStoppedMessage


SESSION = UUID("20000000-0000-4000-8000-000000000001")
COMMON = dict(
    protocol="flightmill",
    protocol_version=1,
    device_id="FM-S3-TEST",
    firmware_version="0.1.3-dev",
    boot_id="boot-1",
    session_id=SESSION,
)


def started():
    return TrialStartedMessage(message_seq=1, start_source="physical_button", **COMMON)


def stopped():
    return TrialStoppedMessage(
        message_seq=2,
        accepted_event_count=0,
        dropped_events=0,
        duration_us=2_000_000,
        stop_reason="physical_button",
        stop_source="physical_button",
        **COMMON,
    )


def test_one_physical_start_without_stop_passes_recording_stage():
    assert physical_transitions_match([started()], SESSION, "boot-1", stopped=False)
    assert not physical_transitions_match([], SESSION, "boot-1", stopped=False)


def test_early_stop_duplicate_start_or_wrong_source_fail():
    for messages in (
        [started(), stopped()],
        [started(), started()],
        [started().model_copy(update={"start_source": "ui"})],
    ):
        assert not physical_transitions_match(messages, SESSION, "boot-1", stopped=False)


def test_exactly_one_physical_stop_passes_final_stage():
    assert physical_transitions_match([started(), stopped()], SESSION, "boot-1", stopped=True)
    assert not physical_transitions_match([started()], SESSION, "boot-1", stopped=True)
    assert not physical_transitions_match(
        [started(), stopped(), stopped()], SESSION, "boot-1", stopped=True
    )


def test_serial_stop_nonzero_count_drop_or_changed_boot_fail():
    for field, value in (
        ("stop_source", "ui"),
        ("accepted_event_count", 1),
        ("dropped_events", 1),
        ("boot_id", "boot-2"),
    ):
        messages = [started(), stopped().model_copy(update={field: value})]
        assert not physical_transitions_match(messages, SESSION, "boot-1", stopped=True)


def test_different_session_does_not_satisfy_requested_trial():
    assert not physical_transitions_match(
        [started(), stopped()], UUID(int=1), "boot-1", stopped=True
    )
