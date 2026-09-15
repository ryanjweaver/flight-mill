"""Software-only pulse replay; no physical device or UI is operated."""

import csv
import math

import pytest

from flightmill.acquisition.service import AcquisitionService


class Clock:
    now = 100.0

    def __call__(self):
        return self.now


@pytest.fixture
def manual(tmp_path):
    clock = Clock()
    service = AcquisitionService(tmp_path, clock=clock)
    service.action("select_source", {"source": "simulation"})
    service.action("connect")
    service.action("configure_simulation", {"profile": "manual"})
    service.action("arm")
    service.action("start")
    yield service, clock
    service.close()


# Device event_us from BENCH_soak1_A1_Attempt4, independently reconciled in
# docs/prototype/soak_attempt4_20260915_audit.json. No runtime evidence dependency.
SOAK_TIMES = [
    4349214, 5466417, 6899731, 8674530, 10239634,
    331490500, 333157586, 334530747, 336188185, 338235324,
    654679686, 656336647, 657884567, 659285402, 661335608,
    957777862, 959806575, 961228841, 962956491, 964247768,
    1202287410, 1204270848, 1205855875, 1207462801, 1208871641,
    1442453035, 1443917842, 1445240260, 1446636279, 1448381231,
]


@pytest.mark.parametrize("batched", [False, True])
def test_soak_replay_withholds_only_restart_speeds_and_preserves_all_raw_rows(manual, tmp_path, batched):
    service, clock = manual
    for timestamp in SOAK_TIMES:
        clock.now = 100 + timestamp / 1_000_000
        service.transport.pulse(event_us=timestamp)
        if not batched:
            service.tick()
    service.tick()
    snapshot = service.action("stop")
    assert snapshot["save_status"] == "saved"
    assert not snapshot["warnings"]
    points = snapshot["chart"]
    assert [p["event_n"] for p in points if p["speed_after_gap"]] == [6, 11, 16, 21, 26]
    assert [p["event_n"] for p in points if p["speed_m_s"] is None] == [1, 6, 11, 16, 21, 26]
    assert snapshot["metrics"]["row_count"] == snapshot["metrics"]["event_count"] == 30
    assert snapshot["metrics"]["speed_m_s"] == pytest.approx(0.3600778306)
    rows = list(csv.DictReader((tmp_path / snapshot["filename"]).open()))
    assert len(rows) == 30
    assert rows[0]["speed_m_s"] == ""
    assert rows[5]["dt_s"] == "321.250866"
    assert rows[5]["speed_m_s"] == "0.0019558501"
    assert rows[6]["speed_m_s"] == "0.3768962913"
    assert float(rows[-1]["cumulative_distance_m"]) == pytest.approx(30 * 0.2 * math.pi)
    for point, row, timestamp in zip(points, rows, SOAK_TIMES):
        assert float(row["time_s"]) == timestamp / 1_000_000
        assert row["dropped_events"] == "0"
        if point["event_n"] > 1:
            assert point["interval_speed_m_s"] == pytest.approx(float(row["speed_m_s"]), abs=5.1e-11)


def pulse_at(service, clock, timestamp):
    clock.now = 100 + timestamp / 1_000_000
    service.transport.pulse(event_us=timestamp)
    service.tick()
    return service.snapshot()


def test_gap_withholds_speed_until_next_interval_and_persists_when_stopped(manual):
    service, clock = manual
    pulse_at(service, clock, 1_000_000)
    pulse_at(service, clock, 2_000_000)
    snapshot = pulse_at(service, clock, 12_000_000)
    assert snapshot["metrics"]["speed_after_gap"]
    assert snapshot["metrics"]["speed_stale"]
    assert snapshot["metrics"]["speed_m_s"] is None
    assert snapshot["metrics"]["last_interval_s"] == 10
    assert snapshot["metrics"]["interval_speed_m_s"] == pytest.approx(0.0628318531)
    clock.now += 1
    service.tick()
    assert service.snapshot()["metrics"]["speed_m_s"] is None
    snapshot = pulse_at(service, clock, 14_000_000)
    assert not snapshot["metrics"]["speed_after_gap"]
    assert not snapshot["metrics"]["speed_stale"]
    assert snapshot["metrics"]["speed_m_s"] == pytest.approx(math.pi / 10)
    snapshot = pulse_at(service, clock, 24_000_000)
    assert snapshot["metrics"]["speed_after_gap"]
    assert service.action("stop")["metrics"]["speed_m_s"] is None
    service.action("disarm")
    service.action("arm", {"attempt": 2})
    metrics = service.snapshot()["metrics"]
    assert not metrics["speed_after_gap"]
    assert metrics["last_interval_s"] is None
    assert metrics["interval_speed_m_s"] is None


@pytest.mark.parametrize("intervals, expected", [
    ([1_000_000, 2_000_000], [False, False]),
    ([1_000_000, 2_000_001], [False, True]),
    ([3_000_000, 6_000_000, 6_000_000], [True, False, False]),
    ([3_000_000, 6_000_001], [True, True]),
    ([10_000_000, 10_000_000], [True, False]),
])
def test_gap_boundary_uses_preceding_device_interval_and_allows_steady_slow_turns(manual, intervals, expected):
    service, clock = manual
    timestamp = 1_000_000
    first = pulse_at(service, clock, timestamp)
    assert first["metrics"]["speed_m_s"] is None
    assert not first["metrics"]["speed_after_gap"]
    for interval, after_gap in zip(intervals, expected):
        timestamp += interval
        snapshot = pulse_at(service, clock, timestamp)
        assert snapshot["metrics"]["speed_after_gap"] is after_gap
        assert (snapshot["metrics"]["speed_m_s"] is None) is after_gap
