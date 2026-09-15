from copy import deepcopy
from uuid import UUID

from hardware.validation.sensor_single_cycle_diagnostic import samples_match


SESSION = UUID("20000000-0000-4000-8000-000000000001")


def samples() -> list[dict]:
    return [
        dict(
            type="heartbeat",
            boot_id="boot-1",
            state="RECORDING",
            session_id=str(SESSION),
            sensor_state=1,
            event_n=1,
            dropped_events=0,
        )
        for _ in range(10)
    ]


def test_stable_blocked_samples_pass() -> None:
    assert samples_match(samples(), 1, 1, "boot-1", SESSION)


def test_even_one_extra_count_or_clear_sample_fails() -> None:
    for field, value in [
        ("event_n", 2),
        ("sensor_state", 0),
        ("dropped_events", 1),
        ("boot_id", "boot-2"),
        ("state", "COMPLETE"),
        ("session_id", "another-session"),
        ("type", "error"),
    ]:
        changed = deepcopy(samples())
        changed[4][field] = value
        assert not samples_match(changed, 1, 1, "boot-1", SESSION)


def test_insufficient_or_absent_telemetry_fails() -> None:
    assert not samples_match([], 1, 1, "boot-1", SESSION)
    assert not samples_match(samples()[:7], 1, 1, "boot-1", SESSION)


def test_clear_state_requires_zero_at_baseline_and_one_after_withdrawal() -> None:
    cleared = samples()
    for sample in cleared:
        sample["sensor_state"] = 0
    assert samples_match(cleared, 0, 1, "boot-1", SESSION)
    assert not samples_match(cleared, 0, 0, "boot-1", SESSION)
    for sample in cleared:
        sample["event_n"] = 0
    assert samples_match(cleared, 0, 0, "boot-1", SESSION)
