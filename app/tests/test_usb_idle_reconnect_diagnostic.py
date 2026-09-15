import json
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import pytest

from hardware.validation import usb_idle_reconnect_diagnostic as driver
from flightmill.protocol.models import HelloMessage, StatusMessage, EventMessage


COMMON = dict(protocol="flightmill", protocol_version=1, device_id="FM-TEST",
              firmware_version="0.1.3-dev")


def transcript():
    before = StatusMessage(message_seq=12, boot_id="old", state="IDLE", session_id=None,
                           sensor_state=0, event_n=0, dropped_events=0, uptime_ms=10000, **COMMON)
    hello = HelloMessage(message_seq=0, boot_id="new", state="IDLE",
                         reset_reason="power_on", **COMMON)
    after = before.model_copy(update={"boot_id": "new", "message_seq":2, "uptime_ms":500})
    return [before, hello, after]


def matches(messages):
    return driver.reconnect_matches(messages, "old", "FM-TEST", "0.1.3-dev")


def test_real_new_boot_and_clear_idle_required():
    assert matches(transcript())
    assert not matches(transcript()[1:])
    assert not matches(transcript()[:1] + transcript()[2:])
    assert not matches(transcript()[:2])
    assert not matches(transcript() + [transcript()[1]])


@pytest.mark.parametrize(("index", "field", "value"), [
    (1, "boot_id", "old"), (1, "message_seq", 2), (1, "device_id", "different"),
    (1, "firmware_version", "other"), (2, "boot_id", "other"),
    (2, "state", "ARMED"), (2, "session_id", UUID(int=1)),
    (2, "sensor_state", 1), (2, "event_n", 1), (2, "dropped_events", 1),
])
def test_wrong_identity_missing_start_and_nonidle_data_cannot_pass(index, field, value):
    messages = transcript()
    messages[index] = messages[index].model_copy(update={field: value})
    assert not matches(messages)


def test_any_retained_event_blocks_idle_pass():
    event = EventMessage(message_seq=13, boot_id="old", session_id=UUID(int=1),
                         event_n=1, event_us=1000, dt_us=0, dropped_events=0, **COMMON)
    messages = transcript()
    messages.insert(1, event)
    assert not matches(messages)


def test_initial_usb_identity_must_be_present_unique_and_on_requested_port(monkeypatch):
    port = SimpleNamespace(device="COM_NEW", vid=0x303A, pid=0x1001, serial_number="usb")
    monkeypatch.setattr(driver.list_ports, "comports", lambda: [port])
    driver.verify_initial_port("COM_NEW", "usb")
    for port_name, identity in [("COM_OLD", "usb"), ("COM_NEW", ""), ("COM_NEW", "wrong")]:
        with pytest.raises(RuntimeError):
            driver.verify_initial_port(port_name, identity)
    monkeypatch.setattr(driver.list_ports, "comports", lambda: [port, port])
    with pytest.raises(RuntimeError):
        driver.verify_initial_port("COM_NEW", "usb")


def test_reconnect_ignores_other_devices_and_attaches_without_probe(tmp_path, monkeypatch):
    right = SimpleNamespace(device="COM_NEW", vid=0x303A, pid=0x1001, serial_number="usb")
    wrong = SimpleNamespace(device="COM_OTHER", vid=0x303A, pid=0x1001, serial_number="other")
    port_scans = iter([[wrong], [wrong, right]])
    monkeypatch.setattr(driver.list_ports, "comports", lambda: next(port_scans))
    monkeypatch.setattr(driver.time, "sleep", lambda seconds: None)
    board = Mock(evidence=SimpleNamespace(output_dir=tmp_path))
    connection = Mock()
    monkeypatch.setattr(driver, "reset_serial_connection", lambda: connection)
    attach = Mock()
    monkeypatch.setattr(driver, "attach_without_probe", attach)
    driver.wait_for_reconnect(board, "usb", 1)
    attach.assert_called_once_with(board, connection=connection)
    board.open.assert_not_called()
    board.write_payload.assert_not_called()
    assert board.port_name == "COM_NEW"
    record = json.loads((tmp_path / "usb_reconnect.json").read_text())
    assert record["connected"]
    assert not record["probe_or_reset_command"]


def test_ambiguous_reconnect_is_not_attached(tmp_path, monkeypatch):
    port = SimpleNamespace(device="COM_NEW", vid=0x303A, pid=0x1001, serial_number="usb")
    monkeypatch.setattr(driver.list_ports, "comports", lambda: [port, port])
    attach = Mock()
    monkeypatch.setattr(driver, "attach_without_probe", attach)
    board = Mock(evidence=SimpleNamespace(output_dir=tmp_path))
    with pytest.raises(RuntimeError, match="Ambiguous"):
        driver.wait_for_reconnect(board, "usb", 1)
    attach.assert_not_called()
    assert not json.loads((tmp_path / "usb_reconnect.json").read_text())["connected"]
