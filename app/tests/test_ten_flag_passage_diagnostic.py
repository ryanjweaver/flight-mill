from hardware.validation.ten_flag_passage_diagnostic import assess_events
from flightmill.protocol.models import EventMessage


def events(count: int) -> list[EventMessage]:
    return [
        EventMessage(
            protocol="flightmill",
            protocol_version=1,
            device_id="FM-S3-001122334455",
            firmware_version="0.1.2-dev",
            message_seq=n,
            boot_id="boot-1",
            session_id="20000000-0000-4000-8000-000000000001",
            event_n=n,
            event_us=n * 1_000_000,
            dt_us=0 if n == 1 else 1_000_000,
            dropped_events=0,
        )
        for n in range(1, count + 1)
    ]


def test_ten_events_pass() -> None:
    result = assess_events(events(10), 10, 0)
    assert result["passed"]
    assert result["count_classification"] == "10 events"


def test_twenty_events_remain_twenty_and_fail_without_truncation() -> None:
    result = assess_events(events(20), 20, 0)
    assert not result["passed"]
    assert result["captured_event_messages"] == 20
    assert len(result["events"]) == 20
    assert result["count_classification"] == "20 events"


def test_other_counts_are_not_forced_into_ten_or_twenty() -> None:
    for count in (0, 9, 11, 19, 21):
        result = assess_events(events(count), count, 0)
        assert not result["passed"]
        assert result["count_classification"] == f"other: {count} events"


def test_missing_frames_or_summary_disagreement_fail() -> None:
    assert not assess_events(events(10), 20, 0)["passed"]
    assert not assess_events(events(20), 10, 0)["passed"]
    assert not assess_events(events(9), 10, 0)["passed"]


def test_drops_and_wrong_first_delta_fail() -> None:
    assert not assess_events(events(10), 10, 1)["passed"]
    captured = events(10)
    captured[0] = captured[0].model_copy(update={"dt_us": 1})
    assert not assess_events(captured, 10, 0)["passed"]
    captured = events(10)
    captured[5] = captured[5].model_copy(update={"dropped_events": 1})
    assert not assess_events(captured, 10, 0)["passed"]


def test_inconsistent_timing_or_numbering_fail() -> None:
    for field, value in (("dt_us", 50_000), ("event_n", 99), ("event_us", 1)):
        captured = events(10)
        captured[5] = captured[5].model_copy(update={field: value})
        assert not assess_events(captured, 10, 0)["passed"]


def test_twenty_passage_trial_requires_exactly_twenty() -> None:
    result = assess_events(events(20), 20, 0, expected_passages=20)
    assert result["passed"]
    assert result["expected_physical_passages"] == 20
    for count in (10, 19, 21, 40):
        result = assess_events(events(count), count, 0, expected_passages=20)
        assert not result["passed"]
        assert result["captured_event_messages"] == count
        assert len(result["events"]) == count


def test_twenty_passage_trial_rejects_summary_mismatch_or_drops() -> None:
    assert not assess_events(events(20), 19, 0, expected_passages=20)["passed"]
    assert not assess_events(events(20), 20, 1, expected_passages=20)["passed"]
