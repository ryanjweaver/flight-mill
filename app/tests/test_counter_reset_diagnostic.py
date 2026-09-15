from uuid import UUID

import pytest

from hardware.validation.counter_reset_diagnostic import counter_reset_matches
from flightmill.protocol.models import EventMessage, TrialStartedMessage, TrialStoppedMessage


SESSIONS = [UUID(int=1), UUID(int=2)]
COMMON = dict(
    protocol="flightmill",
    protocol_version=1,
    device_id="FM-S3-TEST",
    firmware_version="0.1.3-dev",
    boot_id="boot-1",
)


def transcript():
    messages = []
    for offset, session in zip((0, 3), SESSIONS):
        common = dict(COMMON, session_id=session)
        messages.extend(
            [
                TrialStartedMessage(message_seq=offset + 1, start_source="ui", **common),
                EventMessage(
                    message_seq=offset + 2,
                    event_n=1,
                    event_us=1_000_000,
                    dt_us=0,
                    dropped_events=0,
                    **common,
                ),
                TrialStoppedMessage(
                    message_seq=offset + 3,
                    accepted_event_count=1,
                    dropped_events=0,
                    duration_us=2_000_000,
                    stop_source="ui",
                    stop_reason="counter_reset_test",
                    **common,
                ),
            ]
        )
    return messages


def matches(messages, sessions=SESSIONS):
    return counter_reset_matches(messages, sessions, "boot-1", "FM-S3-TEST")


def test_two_distinct_nonzero_trials_on_same_boot_pass():
    assert matches(transcript())


@pytest.mark.parametrize(
    ("index", "field", "value"),
    [
        (4, "event_n", 2),
        (4, "dt_us", 10),
        (4, "dropped_events", 1),
        (5, "accepted_event_count", 0),
        (5, "accepted_event_count", 2),
        (5, "dropped_events", 1),
        (5, "duration_us", 1),
        (3, "start_source", "physical_button"),
        (5, "stop_source", "physical_button"),
        (4, "session_id", UUID(int=3)),
        (4, "device_id", "FM-S3-DIFFERENT"),
        (4, "boot_id", "boot-2"),
        (3, "message_seq", 1),
    ],
)
def test_invalid_counter_timing_identity_or_transition_fails(index, field, value):
    messages = transcript()
    messages[index] = messages[index].model_copy(update={field: value})
    assert not matches(messages)


def test_missing_or_duplicate_frames_are_not_hidden():
    for index in range(6):
        messages = transcript()
        assert not matches(messages[:index] + messages[index + 1 :])
        assert not matches(messages[:index] + [messages[index]] + messages[index:])


def test_reset_between_whole_trials_fails():
    messages = transcript()
    messages[3:] = [m.model_copy(update={"boot_id": "boot-2"}) for m in messages[3:]]
    assert not matches(messages)


def test_reused_session_or_reordered_trials_fail():
    messages = transcript()
    assert not matches(messages, [SESSIONS[0], SESSIONS[0]])
    assert not matches(messages, SESSIONS[:1])
    assert not matches(messages[3:] + messages[:3])
    messages[3:] = [m.model_copy(update={"session_id": SESSIONS[0]}) for m in messages[3:]]
    assert not matches(messages)
