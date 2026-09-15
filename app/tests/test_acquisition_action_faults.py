"""Regression coverage for interactive command failures and missing physical stops."""

from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest

from flightmill.acquisition.service import AcquisitionService
from flightmill.protocol.models import serialize_line
from flightmill.storage.trial_writer import TrialWriter, discover_trials


class Clock:
    now = 100.0

    def __call__(self):
        return self.now


@pytest.fixture
def recording(tmp_path: Path):
    clock = Clock()
    service = AcquisitionService(tmp_path, clock=clock)
    service.action("select_source", {"source": "simulation"})
    service.action("connect")
    service.action("arm")
    service.action("start")
    yield service, clock, tmp_path
    service.close()


@pytest.mark.parametrize("action,payload", [
    ("configure_simulation", {"profile": "zero"}),
    ("stop", {}),
    ("note", {"text": "This note must not appear as successfully saved"}),
])
def test_action_audit_failure_stops_and_preserves_incomplete(recording, action, payload):
    service, _, directory = recording
    original = TrialWriter.log_action

    def fail_action(writer, name, details, elapsed_s=0):
        if name == action:
            raise OSError("One-shot audit write failure")
        return original(writer, name, details, elapsed_s)

    with patch.object(TrialWriter, "log_action", fail_action):
        with pytest.raises(OSError, match="audit write failure"):
            service.action(action, payload)
    snapshot = service.snapshot()
    assert not snapshot["connected"]
    assert snapshot["save_status"] == "incomplete"
    assert snapshot["trial"]["stop_reason"] == "storage_failure"
    assert discover_trials(directory)[0]["incomplete"]
    assert snapshot["simulation"]["profile"] == "steady"
    assert not snapshot["trial"]["notes"]


def test_manual_pulse_write_failure_uses_same_fatal_policy(recording):
    service, _, _ = recording
    with patch.object(TrialWriter, "append_event", side_effect=OSError("Disk full")):
        with pytest.raises(OSError, match="Disk full"):
            service.action("pulse")
    snapshot = service.snapshot()
    assert snapshot["save_status"] == "incomplete"
    assert not snapshot["connected"]


def test_physical_stop_missing_summary_has_terminal_deadline(recording):
    service, clock, directory = recording
    service.action("fault", {"kind": "missing_summary"})
    snapshot = service.action("button", {"held_ms": 1500})
    assert snapshot["state"] == "STOPPING"
    assert snapshot["pending"]["action"] == "stop"
    clock.now += 4
    service.tick()
    snapshot = service.snapshot()
    assert snapshot["save_status"] == "incomplete"
    assert snapshot["trial"]["stop_reason"] == "stop_timeout"
    assert not snapshot["connected"]
    assert discover_trials(directory)[0]["incomplete"]


def test_heartbeat_terminal_state_without_summary_does_not_record_forever(recording):
    service, clock, _ = recording
    # Device physically stopped outside host controls; terminal frame was lost.
    service.transport.device.physical_button(1500)
    clock.now += 1
    service.tick()
    assert service.snapshot()["state"] == "STOPPING"
    clock.now += 4
    service.tick()
    assert service.snapshot()["save_status"] == "incomplete"


def test_heartbeat_session_change_is_a_terminal_fault(recording):
    service, _, _ = recording
    heartbeat = service.transport.device.heartbeat().model_copy(update={"session_id": uuid4()})
    service.transport._enqueue(serialize_line(heartbeat))
    service.tick()
    assert service.snapshot()["save_status"] == "incomplete"
    assert service.snapshot()["trial"]["stop_reason"] == "session_mismatch"


def test_invalid_configuration_does_not_abort_a_valid_recording(recording):
    service, _, _ = recording
    with pytest.raises(ValueError, match="Unknown simulation profile"):
        service.action("configure_simulation", {"profile": "invalid"})
    assert service.snapshot()["state"] == "RECORDING"
    assert not service.action("stop")["trial"]["incomplete"]


def test_failed_create_exposes_recoverable_partial_without_restart(tmp_path: Path):
    service = AcquisitionService(tmp_path, clock=Clock())
    service.action("select_source", {"source": "simulation"})
    service.action("connect")
    try:
        # Initial reservation and partial streams exist before the first checkpoint.
        with patch.object(TrialWriter, "checkpoint", side_effect=OSError("Initial sync failed")):
            with pytest.raises(OSError, match="Initial sync failed"):
                service.action("arm")
        snapshot = service.snapshot()
        assert snapshot["state"] == "IDLE"
        assert snapshot["trial"] is None
        assert len(snapshot["recent_trials"]) == 1
        assert snapshot["recent_trials"][0]["incomplete"]
        assert any("partial" in file["name"] for file in snapshot["recent_trials"][0]["files"])
    finally:
        service.close()


def test_shutdown_audit_failure_preserves_incomplete_and_closes_transport(recording):
    service, _, directory = recording
    original = TrialWriter.log_action

    def fail_stop(writer, name, details, elapsed_s=0):
        if name == "stop":
            raise OSError("Shutdown audit write failed")
        return original(writer, name, details, elapsed_s)

    with patch.object(TrialWriter, "log_action", fail_stop):
        service.close()
    snapshot = service.snapshot()
    assert not snapshot["connected"]
    assert snapshot["save_status"] == "incomplete"
    assert snapshot["trial"]["stop_reason"] == "storage_failure"
    assert discover_trials(directory)[0]["incomplete"]
    assert service._writer is None


def test_shutdown_finalization_failure_is_incomplete_and_recoverable(recording):
    service, _, directory = recording
    with patch.object(TrialWriter, "finalize", side_effect=OSError("Final sync failed")):
        service.close()
    snapshot = service.snapshot()
    assert not snapshot["connected"]
    assert snapshot["save_status"] == "incomplete"
    assert discover_trials(directory)[0]["incomplete"]


def test_shutdown_cleanly_saves_confirmed_stop(recording):
    service, clock, directory = recording
    clock.now += 1
    service.tick()
    service.close()
    snapshot = service.snapshot()
    assert not snapshot["connected"]
    assert snapshot["save_status"] == "saved"
    trial = discover_trials(directory)[0]
    assert not trial["incomplete"]
    assert trial["metadata"]["stop_reason"] == "application_exit"
    assert trial["metadata"]["actual_duration_s"] == 1


@pytest.mark.parametrize("controlled_abort", [False, True])
def test_recovery_distinguishes_placeholder_device_count_from_observed_rows(recording, controlled_abort):
    service, clock, directory = recording
    clock.now += 45
    service.tick()
    assert service.snapshot()["metrics"]["row_count"] == 45
    if controlled_abort:
        service.action("disconnect")
    else:
        # End file ownership without a device STOP or a final metadata update,
        # reproducing the persisted state left by interrupted acquisition.
        assert service._writer is not None
        service._writer.close()
    recovered = AcquisitionService(directory, clock=Clock())
    try:
        trial = recovered.snapshot()["recent_trials"][0]
        assert trial["incomplete"]
        assert trial["row_count"] == 45
        if controlled_abort:
            # Host observation remains in metadata, but no terminal summary exists.
            assert trial["event_count"] is None
            assert trial["metadata"]["accepted_event_count"] == 45
            assert trial["duration_s"] is None
        else:
            assert trial["metadata"]["accepted_event_count"] == 0
            assert trial["event_count"] is None
            assert trial["duration_s"] is None
    finally:
        recovered.close()
