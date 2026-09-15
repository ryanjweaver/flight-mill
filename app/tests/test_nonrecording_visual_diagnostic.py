from unittest.mock import Mock
from uuid import UUID

import pytest

from hardware.validation import nonrecording_visual_diagnostic as driver
from flightmill.protocol.models import ErrorMessage, EventMessage, SensorStateMessage, StatusMessage


COMMON = dict(protocol="flightmill", protocol_version=1, device_id="FM-TEST",
              firmware_version="0.1.3-dev", boot_id="boot")


def status(state="IDLE", session=None):
    return StatusMessage(message_seq=20, state=state, session_id=session,
                         sensor_state=0, event_n=0, dropped_events=0, uptime_ms=1000, **COMMON)


@pytest.mark.parametrize(("state", "session"), [("IDLE", None), ("ARMED", UUID(int=1))])
def test_nonrecording_sensor_cycles_are_observed_without_events(state, session):
    cycles = [SensorStateMessage(message_seq=i, state=state, sensor_state=level, **COMMON)
              for i, level in enumerate([1, 0, 1, 0, 1, 0])]
    messages = cycles + [status(state, session)]
    assert driver.nonrecording_matches(messages, state, "boot", "FM-TEST", session)
    assert driver.cycle_levels(messages) == [1, 0, 1, 0, 1, 0]
    event = EventMessage(message_seq=21, session_id=UUID(int=1), event_n=1,
                         event_us=1000, dt_us=0, dropped_events=0, **COMMON)
    assert not driver.nonrecording_matches(messages + [event], state, "boot", "FM-TEST", session)


@pytest.mark.parametrize(("field", "value"), [
    ("state", "RECORDING"), ("boot_id", "new"), ("device_id", "different"),
    ("sensor_state", 1), ("event_n", 1), ("dropped_events", 1), ("session_id", UUID(int=1)),
])
def test_nonrecording_rejects_changed_state_counts_or_identity(field, value):
    bad = status().model_copy(update={field: value})
    assert not driver.nonrecording_matches([bad], "IDLE", "boot", "FM-TEST")


def test_warning_requires_exactly_one_unsolicited_unarmed_error():
    warning = ErrorMessage(message_seq=19, state="IDLE", code="button_unarmed",
                           message="unarmed", **COMMON)
    def check(messages):
        return driver.nonrecording_matches(messages, "IDLE", "boot", "FM-TEST", warning=True)
    assert check([warning, status()])
    assert not check([status()])
    assert not check([warning, warning, status()])
    assert not check([warning.model_copy(update={"code": "other"}), status()])
    assert not check([warning.model_copy(update={"request_id": UUID(int=1)}), status()])
    assert not driver.nonrecording_matches([warning, status()], "IDLE", "boot", "FM-TEST")


@pytest.mark.parametrize(("gate", "arm_count"), [
    ("ready_before_armed_check", 0), ("ready_before_event_recording", 1),
])
def test_declined_readiness_never_starts_recording(tmp_path, monkeypatch, gate, arm_count):
    binary = tmp_path / "firmware.bin"
    binary.write_bytes(b"not firmware")
    monkeypatch.setattr("sys.argv", ["diagnostic", "--port", "COM_TEST",
                        "--expected-usb-serial", "usb", "--expected-device-id", "FM-TEST",
                        "--expected-firmware-version", "0.1.3-dev", "--firmware-bin", str(binary)])
    monkeypatch.setattr(driver, "default_output", lambda mode: tmp_path / "evidence")
    board = Mock()
    board.serial = None
    board.get_status.return_value = status()
    monkeypatch.setattr(driver, "Board", lambda port, evidence: board)
    monkeypatch.setattr(driver, "verify_initial_port", lambda *args: None)
    monkeypatch.setattr(driver, "attach_without_probe", lambda *args: None)
    monkeypatch.setattr(driver, "check_cycles", lambda *args: None)
    monkeypatch.setattr(driver, "nonrecording_matches", lambda *args, **kwargs: True)
    monkeypatch.setattr(driver, "operator_yes_no", lambda *args: True)
    monkeypatch.setattr(driver, "operator_action",
                        lambda evidence, name, prompt: name != gate)
    assert driver.main() == 1
    assert board.arm.call_count == arm_count
    board.start.assert_not_called()
    board.close.assert_called_once_with()
