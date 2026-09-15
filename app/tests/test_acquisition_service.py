"""Behavior tests exercise actual protocol bytes, timestamps, and saved bundles."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from flightmill.acquisition.service import AcquisitionService
from flightmill.acquisition.transport import SimulatedTransport
from flightmill.api.app import create_app
from flightmill.protocol.models import HeartbeatMessage, parse_line
from flightmill.storage.trial_writer import TrialWriter, discover_trials
from flightmill.storage import trial_writer


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def live(tmp_path: Path):
    clock = Clock()
    service = AcquisitionService(tmp_path, clock=clock)
    service.action("select_source", {"source": "simulation"})
    service.action("connect")
    yield service, clock
    service.close()


def start(service: AcquisitionService, profile: str = "steady", **setup) -> None:
    service.action("configure_simulation", {"profile": profile})
    service.action("arm", setup)
    service.action("start")


def test_steady_recording_uses_authoritative_raw_values(live, tmp_path: Path):
    service, clock = live
    start(service)
    clock.advance(20)
    service.tick()
    snapshot = service.action("stop")
    assert snapshot["state"] == "COMPLETE"
    assert snapshot["save_status"] == "saved"
    assert snapshot["metrics"]["event_count"] == 20
    assert snapshot["metrics"]["row_count"] == 20
    assert snapshot["metrics"]["distance_m"] == pytest.approx(4 * math.pi)
    rows = list(csv.DictReader((tmp_path / snapshot["filename"]).open()))
    assert rows[0]["speed_m_s"] == ""
    assert float(rows[1]["speed_m_s"]) == pytest.approx(0.6283185307)
    trial = discover_trials(tmp_path)[0]
    assert not trial["incomplete"]
    assert trial["metadata"]["actual_duration_s"] == 20
    assert trial["simulation"]["acquisition_mode"] == "simulation"


def test_zero_event_clock_and_auto_stop_are_independent_of_pulses(live, tmp_path: Path):
    service, clock = live
    start(service, "zero", planned_duration_s=60)
    clock.advance(60)
    service.tick()
    snapshot = service.snapshot()
    assert snapshot["state"] == "COMPLETE"
    assert snapshot["metrics"]["elapsed_s"] == 60
    assert snapshot["metrics"]["row_count"] == 0
    assert snapshot["trial"]["stop_reason"] == "planned_duration"
    assert not snapshot["trial"]["incomplete"]
    assert len((tmp_path / snapshot["filename"]).read_text().splitlines()) == 1


def test_silence_retains_last_speed_as_stale_without_fabricated_rows(live):
    service, clock = live
    start(service, "manual")
    service.action("pulse")
    clock.advance(1)
    service.action("pulse")
    clock.advance(9)
    service.tick()
    snapshot = service.action("stop")
    assert snapshot["metrics"]["elapsed_s"] == 10
    assert snapshot["metrics"]["row_count"] == 2
    assert snapshot["metrics"]["last_pulse_age_s"] == 9
    assert snapshot["metrics"]["speed_stale"]
    assert snapshot["metrics"]["speed_m_s"] == pytest.approx(2 * math.pi * 0.1)


def test_boot_uptime_does_not_reset_when_trial_starts(live):
    service, clock = live
    clock.advance(30)
    start(service, "zero")
    clock.advance(10)
    service.tick()
    heartbeat = service.transport.device.heartbeat()
    assert heartbeat.uptime_ms == 40_000
    assert service.snapshot()["metrics"]["elapsed_s"] == 10


def test_duplicate_and_wrong_session_do_not_poison_trusted_baseline(live):
    service, clock = live
    start(service, "manual")
    service.action("pulse")
    service.action("fault", {"kind": "duplicate"})
    service.action("fault", {"kind": "wrong_session"})
    clock.advance(1)
    service.action("pulse")
    snapshot = service.action("stop")
    assert snapshot["metrics"]["row_count"] == 2
    assert snapshot["metrics"]["event_count"] == 2
    assert not snapshot["trial"]["incomplete"]
    assert {item["code"] for item in snapshot["warnings"]} >= {
        "nonmonotonic_message", "session_mismatch"
    }


@pytest.mark.parametrize("fault", ["drops", "malformed", "event_gap", "mismatched_summary"])
def test_integrity_fault_never_claims_complete(live, fault):
    service, clock = live
    start(service)
    clock.advance(1)
    service.tick()
    service.action("fault", {"kind": fault})
    clock.advance(1)
    service.tick()
    snapshot = service.action("stop")
    assert snapshot["trial"]["incomplete"]
    assert snapshot["save_status"] == "incomplete"
    assert any("partial" in item["name"] for item in snapshot["trial"]["files"])


@pytest.mark.parametrize("fault", ["disconnect", "reset", "heartbeat_loss", "missing_summary"])
def test_transport_and_terminal_failure_preserve_partial_trial(live, fault):
    service, clock = live
    start(service, "manual")
    service.action("pulse")
    service.action("fault", {"kind": fault})
    if fault == "missing_summary":
        assert service.action("stop")["state"] == "STOPPING"
    if fault in {"heartbeat_loss", "missing_summary"}:
        clock.advance(4)
        service.tick()
    snapshot = service.snapshot()
    assert snapshot["trial"]["incomplete"]
    assert snapshot["save_status"] == "incomplete"
    assert not snapshot["connected"]
    assert snapshot["metrics"]["row_count"] == 1


def test_missing_ack_uses_confirmed_transition_without_retry(live):
    service, _ = live
    service.action("arm")
    service.action("fault", {"kind": "missing_ack"})
    snapshot = service.action("start")
    assert snapshot["state"] == "RECORDING"
    assert snapshot["pending"] is None
    assert any(item["code"] == "missing_ack" for item in snapshot["warnings"])
    assert not service.action("stop")["trial"]["incomplete"]


def test_missing_disarm_ack_reconciles_status_without_repeating_command(live):
    service, clock = live
    start(service, "zero")
    service.action("stop")
    service.action("fault", {"kind": "missing_ack"})
    assert service.action("disarm")["pending"]["action"] == "disarm"
    clock.advance(3)
    service.tick()
    snapshot = service.snapshot()
    assert snapshot["state"] == "IDLE"
    assert snapshot["pending"] is None
    assert any(item["code"] == "disarm_reconciled" for item in snapshot["warnings"])


def test_delayed_response_holds_pending_and_blocks_double_start(live):
    service, clock = live
    service.action("arm")
    service.action("fault", {"kind": "delayed_ack"})
    snapshot = service.action("start")
    assert snapshot["state"] == "ARMED"
    assert snapshot["pending"]["action"] == "start"
    with pytest.raises(ValueError, match="pending"):
        service.action("start")
    clock.advance(1.5)
    service.tick()
    snapshot = service.snapshot()
    assert snapshot["state"] == "RECORDING"
    assert snapshot["pending"] is None
    assert snapshot["metrics"]["elapsed_s"] == 1.5


def test_fragmented_message_is_framed_and_not_treated_as_corrupt(live):
    service, _ = live
    start(service, "zero")
    service.action("fault", {"kind": "fragmented"})
    assert not service.action("stop")["trial"]["incomplete"]


def test_physical_button_sensor_gate_and_notes(live, tmp_path: Path):
    service, clock = live
    service.action("sensor", {"blocked": True})
    with pytest.raises(ValueError, match="Clear"):
        service.action("arm")
    service.action("sensor", {"blocked": False})
    service.action("configure_simulation", {"profile": "zero"})
    service.action("arm")
    assert service.action("button", {"held_ms": 100})["state"] == "RECORDING"
    clock.advance(2)
    service.action("note", {"text": "Trial observation"})
    assert service.action("button", {"held_ms": 100})["state"] == "RECORDING"
    snapshot = service.action("button", {"held_ms": 1500})
    assert snapshot["state"] == "COMPLETE"
    assert snapshot["trial"]["stop_reason"] == "physical_button"
    simulation = json.loads(next(tmp_path.glob("*_simulation.json")).read_text())
    note = next(item for item in simulation["actions"] if item["action"] == "note")
    assert note["elapsed_s"] == 2
    assert note["details"]["text"] == "Trial observation"


def test_collision_requires_new_attempt_and_state_prevents_duplicate_recording(live):
    service, _ = live
    start(service, "zero")
    with pytest.raises(ValueError):
        service.action("arm")
    with pytest.raises(ValueError):
        service.action("validate", {"arm_radius_cm": 20})
    service.action("stop")
    service.action("disarm")
    with pytest.raises(ValueError, match="already exist"):
        service.action("arm")
    assert service.action("arm", {"attempt": 2})["state"] == "ARMED"


def test_editing_next_trial_radius_cannot_recalculate_previous_measurements(live):
    service, _ = live
    start(service, "manual")
    service.action("pulse")
    service.action("stop")
    service.action("disarm")
    snapshot = service.action("validate", {"attempt": 2, "arm_radius_cm": 20})
    assert snapshot["setup"]["arm_radius_cm"] == 20
    assert snapshot["metrics"]["arm_radius_cm"] == 10
    assert snapshot["metrics"]["distance_m"] == pytest.approx(2 * math.pi * 0.1)


def test_invalid_stop_reason_does_not_strand_recording(live):
    service, _ = live
    start(service, "zero")
    with pytest.raises(ValueError, match="Stop reason"):
        service.action("stop", {"reason": "x" * 65})
    assert service.snapshot()["state"] == "RECORDING"
    assert service.action("stop")["state"] == "COMPLETE"


def test_storage_write_failure_is_terminal_and_keeps_recorded_rows(live):
    service, clock = live
    start(service)
    clock.advance(1)
    service.tick()
    with patch.object(TrialWriter, "append_event", side_effect=OSError("simulated disk full")):
        clock.advance(1)
        service.tick()
    snapshot = service.snapshot()
    assert snapshot["save_status"] == "incomplete"
    assert not snapshot["connected"]
    assert snapshot["trial"]["stop_reason"] == "storage_failure"
    assert snapshot["metrics"]["row_count"] == 1


def test_post_append_checkpoint_failure_reports_the_row_actually_retained(live, tmp_path):
    service, clock = live
    start(service)
    clock.advance(1)
    service.tick()
    service._writer._last_sync = -100
    original = trial_writer._replace_journal_with_retry
    attempts = []

    def deny_first_checkpoint(source, destination):
        attempts.append(source)
        if len(attempts) == 1:
            raise PermissionError("SOFTWARE ONLY exhausted journal replacement")
        return original(source, destination)

    with patch.object(trial_writer, "_replace_journal_with_retry", side_effect=deny_first_checkpoint):
        clock.advance(1)
        service.tick()
    snapshot = service.snapshot()
    trial = discover_trials(tmp_path)[0]
    assert snapshot["save_status"] == "incomplete"
    assert snapshot["metrics"]["event_count"] == 2
    assert snapshot["metrics"]["row_count"] == trial["row_count"] == 2
    journal = json.loads(next(tmp_path.glob("*_journal.json")).read_bytes())
    assert journal["status"] == "incomplete"
    assert journal["row_count"] == journal["durable_row_count"] == 2


def test_finalization_failure_keeps_confirmed_terminal_observation(live, tmp_path):
    service, clock = live
    start(service)
    clock.advance(5)
    service.tick()
    with patch.object(trial_writer, "_rename_no_replace",
                      side_effect=PermissionError("SOFTWARE ONLY final promotion denied")):
        with pytest.raises(PermissionError, match="final promotion denied"):
            service.action("stop")
    snapshot = service.snapshot()
    retained = discover_trials(tmp_path)[0]
    assert snapshot["save_status"] == "incomplete"
    assert snapshot["device_stop_confirmed"] is True
    assert retained["status"] == "incomplete"
    assert retained["metadata"]["actual_duration_s"] == 5
    assert snapshot["trial"]["duration_s"] == 5
    assert snapshot["metrics"]["event_count"] == 5
    assert snapshot["metrics"]["row_count"] == retained["row_count"] == 5


def checksum_terminal_bundle(directory, snapshot, duration, count, rows):
    """Inspect the same real sidecars through discovery, restart and read-only API."""
    retained = discover_trials(directory)[0]
    assert retained["incomplete"] and retained["status"] != "complete"
    assert retained["row_count"] == rows
    assert retained["metadata"]["actual_duration_s"] == duration
    assert retained["metadata"]["accepted_event_count"] == count
    assert retained["metadata"]["stopped_at_local_iso8601"] is not None
    base = retained["base"]
    raw = (directory / f"{base}.partial.csv").read_bytes()
    assert len(list(csv.DictReader(io.StringIO(raw.decode())))) == rows
    assert retained["metadata"]["raw_csv_sha256"] == hashlib.sha256(raw).hexdigest()
    records = [json.loads(line) for line in
               (directory / f"{base}_protocol.partial.jsonl").read_text().splitlines()]
    messages = [json.loads(r["text"]) for r in records if r["kind"] == "protocol"]
    summary, = [m for m in messages if m["type"] == "trial_stopped"]
    assert summary["duration_us"] == duration * 1_000_000
    assert summary["accepted_event_count"] == count
    assert retained["metadata"]["final_dropped_events"] == summary["dropped_events"]
    assert len([m for m in messages if m["type"] == "event"]) == rows
    restarted = AcquisitionService(directory, clock=Clock())
    try:
        # No lifespan/ticker: these requests only read the closed artifact set.
        client = TestClient(create_app(directory, service=restarted), base_url="http://127.0.0.1")
        client.get("/api/session")
        archive = client.get("/api/state").json()["recent_trials"][0]
        assert archive["duration_s"] == duration
        assert archive["event_count"] == count
        assert archive["row_count"] == rows and archive["incomplete"]
        assert archive["acquisition_mode"] == "simulation"
        response = client.get(f"/api/trials/{snapshot['trial']['id']}/bundle")
        assert response.status_code == 200
        with zipfile.ZipFile(io.BytesIO(response.content)) as bundle:
            assert bundle.testzip() is None
            for file in retained["files"]:
                assert bundle.read(file["name"]) == Path(file["path"]).read_bytes()
        client.close()
    finally:
        restarted.close()


@pytest.mark.parametrize("duration,mismatch", [(5, False), (0, False), (5, True)])
def test_terminal_checksum_first_failure_retains_observation(live, tmp_path, duration, mismatch):
    service, clock = live
    start(service)
    clock.advance(duration)
    service.tick()
    assert service.snapshot()["metrics"]["row_count"] == duration
    if mismatch:
        service.action("fault", {"kind": "mismatched_summary"})
        service.transport.device.inject_dropped_events(2)
    original = TrialWriter.raw_sha256
    calls = []

    def fail_first(writer):
        calls.append(writer)
        if len(calls) == 1:
            raise PermissionError("SOFTWARE ONLY first terminal checksum denied")
        return original(writer)

    with patch.object(TrialWriter, "raw_sha256", fail_first):
        with pytest.raises(PermissionError, match="first terminal checksum denied"):
            service.action("stop")
    snapshot = service.snapshot()
    assert snapshot["device_stop_confirmed"] is True
    assert snapshot["save_status"] == "incomplete"
    assert snapshot["trial"]["duration_s"] == duration
    assert len(calls) == 2  # Original error propagated; one existing abort recovery.
    count = duration + int(mismatch)
    assert snapshot["metrics"]["accepted_received_count"] == duration
    assert snapshot["metrics"]["row_count"] == duration
    assert snapshot["metrics"]["event_count"] == count
    if mismatch:
        assert "summary_count" in {w["code"] for w in snapshot["warnings"]}
        assert service._metadata.final_dropped_events == 2
    checksum_terminal_bundle(tmp_path, snapshot, duration, count, duration)


@pytest.mark.parametrize("duration", [0, 5])
def test_terminal_checksum_persistent_failure_keeps_memory_without_complete_claim(live, tmp_path, duration):
    service, clock = live
    start(service)
    clock.advance(duration)
    service.tick()
    with patch.object(TrialWriter, "raw_sha256",
                      side_effect=PermissionError("SOFTWARE ONLY persistent checksum denial")):
        with pytest.raises(PermissionError, match="persistent checksum denial"):
            service.action("stop")
    snapshot = service.snapshot()
    assert snapshot["save_status"] == "incomplete"
    assert snapshot["device_stop_confirmed"] is True
    assert snapshot["trial"]["duration_s"] == duration
    assert service._metadata.actual_duration_s == duration
    assert service._metadata.accepted_event_count == duration
    assert service._metadata.stopped_at_local_iso8601 is not None
    assert service._metadata.raw_csv_sha256 is None
    assert service._metadata.incomplete
    retained = discover_trials(tmp_path)[0]
    assert retained["incomplete"] and retained["status"] != "complete"
    assert retained["row_count"] == duration
    # Abort could not hash/update the writer; do not claim the in-memory
    # terminal observation reached its sidecar or is recoverable on restart.
    assert retained["metadata"]["actual_duration_s"] is None
    assert retained["metadata"]["raw_csv_sha256"] is None
    assert not (tmp_path / f"{retained['base']}.csv").exists()
    assert any("Partial-file checkpoint failed" in d["message"] for d in snapshot["diagnostics"])


def test_terminal_checksum_missing_summary_never_uses_positive_host_elapsed(live, tmp_path):
    service, clock = live
    start(service)
    clock.advance(5)
    service.tick()
    service.action("fault", {"kind": "missing_summary"})
    assert service.action("stop")["state"] == "STOPPING"
    clock.advance(3.1)
    service.tick()
    snapshot = service.snapshot()
    assert snapshot["metrics"]["elapsed_s"] > 0
    assert snapshot["device_stop_confirmed"] is None
    assert snapshot["save_status"] == "incomplete"
    assert snapshot["trial"]["duration_s"] is None
    retained = discover_trials(tmp_path)[0]
    assert retained["metadata"]["actual_duration_s"] is None
    assert retained["row_count"] == 5
    restarted = AcquisitionService(tmp_path, clock=Clock())
    try:
        archive = restarted.snapshot()["recent_trials"][0]
        assert archive["duration_s"] is None and archive["event_count"] is None
        assert archive["metadata"]["accepted_event_count"] == 5  # Last observed only.
    finally:
        restarted.close()


def test_stress_profile_and_snapshot_history_are_bounded(live):
    service, clock = live
    start(service, "stress")
    for _ in range(61):
        clock.advance(1)
        service.tick()
    snapshot = service.action("stop")
    # The stress profile still supplies 20 Hz raw stimuli. With the new
    # 150 ms setting, only every third stimulus is accepted (407 in 61 s).
    assert snapshot["min_event_interval_us"] == 150_000
    assert snapshot["metrics"]["row_count"] == 407
    assert snapshot["metrics"]["event_count"] == 407
    assert not snapshot["trial"]["incomplete"]


def test_simulator_transport_yields_protocol_bytes():
    clock = Clock()
    transport = SimulatedTransport(clock=clock)
    transport.open()
    while transport.read():
        pass
    clock.advance(5)
    transport.tick()
    message = parse_line(transport.read())
    assert isinstance(message, HeartbeatMessage)
    assert message.uptime_ms == 5000
