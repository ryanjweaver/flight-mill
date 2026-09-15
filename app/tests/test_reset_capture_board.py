import json
import ctypes
import sys
from collections import deque
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from serial import SerialException

from hardware.validation import recording_reset_diagnostic as driver
from hardware.validation.checkpoint_02_runner import Evidence, operator_action
from flightmill.protocol.models import HelloMessage


def hello(boot):
    return HelloMessage(
        protocol="flightmill",
        protocol_version=1,
        device_id="FM-TEST",
        firmware_version="0.1.3-dev",
        message_seq=0,
        boot_id=boot,
        state="IDLE",
        reset_reason="other",
    )


class FakeSerial:
    def __init__(self, frames=(), *, disconnected=False):
        self.is_open = True
        self.frames = deque(frames)
        self.disconnected = disconnected

    def readline(self):
        if self.disconnected:
            raise SerialException("RESET invalidated old USB handle")
        return self.frames.popleft() if self.frames else b""

    def open(self):
        self.is_open = True

    def close(self):
        self.is_open = False


def test_reset_wait_recovers_hello_before_operator_finishes(tmp_path):
    evidence = Evidence(tmp_path / "reset", "recording_reset_diagnostic", "COM_TEST", None)
    board = driver.Board("COM_TEST", evidence)
    board.serial = FakeSerial(disconnected=True)
    board.hello = hello("old")
    board.device_id = "FM-TEST"
    board.firmware_version = "0.1.3-dev"
    new_frame = hello("new").model_dump_json().encode() + b"\n"
    replacement = FakeSerial([new_frame])
    port = SimpleNamespace(device="COM_TEST", vid=0x303A, pid=0x1001, serial_number="test-usb")
    captured = Event()
    capture = evidence.capture_protocol
    observed_before_done = []

    def record(frame):
        capture(frame)
        if frame == new_frame:
            captured.set()

    def operator_answer(prompt):
        observed_before_done.append(captured.wait(1.0))
        return "DONE"

    evidence.capture_protocol = record
    try:
        with (
            patch("serial.tools.list_ports.comports", return_value=[port]),
            patch("hardware.validation.reset_capture_board.reset_serial_connection", return_value=replacement),
            patch("subprocess.run", side_effect=AssertionError("A recovery must not run a probe")),
            patch("builtins.input", side_effect=operator_answer),
        ):
            board.enable_reset_recovery()
            assert operator_action(evidence, "reset", "Press RESET once", allow_disconnect=True)
            assert observed_before_done == [True]
            assert board.read_message(0.1).boot_id == "new"
            assert evidence.protocol_frames == [new_frame]
    finally:
        board.close()
        evidence.finalize()


@pytest.fixture
def reset_board(tmp_path):
    evidence = Evidence(tmp_path / "recovery", "test", "COM_OLD", None)
    board = driver.Board("COM_OLD", evidence)
    board.serial = FakeSerial(disconnected=True)
    board.hello = hello("old")
    board.device_id = "FM-TEST"
    board.firmware_version = "0.1.3-dev"
    port = SimpleNamespace(device="COM_OLD", vid=0x303A, pid=0x1001, serial_number="test-usb")
    with patch("serial.tools.list_ports.comports", return_value=[port]):
        board.enable_reset_recovery()
    try:
        yield board, evidence, port
    finally:
        board.close()
        evidence.finalize()


def test_recovery_uses_exact_usb_identity_and_preserves_queue(reset_board):
    board, evidence, port = reset_board
    old = hello("old")
    old_frame = old.model_dump_json().encode() + b"\n"
    evidence.capture_protocol(old_frame)
    evidence.observe_message(old)
    board._pending_messages.append(old)
    right = SimpleNamespace(**vars(port))
    right.device = "COM_NEW"
    wrong = SimpleNamespace(**vars(port))
    wrong.serial_number = "different-usb"
    new_frame = hello("new").model_dump_json().encode() + b"\n"
    with (
        patch("serial.tools.list_ports.comports", return_value=[wrong, right]),
        patch("hardware.validation.reset_capture_board.reset_serial_connection", return_value=FakeSerial([new_frame])) as factory,
        patch("subprocess.run", side_effect=AssertionError("No reset/probe permitted")),
    ):
        assert board.read_live_message(0.1).boot_id == "new"
    factory.assert_called_once_with()
    assert board.port_name == "COM_NEW"
    assert list(board._pending_messages) == [old]
    assert evidence.protocol_frames == [old_frame, new_frame]
    assert not evidence.sequence_issues


def test_unexpected_disconnect_does_not_reconnect(reset_board):
    board, _, _ = reset_board
    board._reset_recovery_enabled = False
    with patch("hardware.validation.reset_capture_board.reset_serial_connection") as factory:
        with pytest.raises(SerialException):
            board.read_live_message(0.1)
    factory.assert_not_called()


def test_missing_usb_identity_times_out_without_attaching(reset_board):
    board, _, port = reset_board
    port.serial_number = "different-usb"
    board.RECOVERY_TIMEOUT_S = 0.01
    board.RETRY_INTERVAL_S = 0
    with (
        patch("serial.tools.list_ports.comports", return_value=[port]),
        patch("hardware.validation.reset_capture_board.reset_serial_connection") as factory,
    ):
        with pytest.raises(TimeoutError):
            board.read_live_message(0.1)
    factory.assert_not_called()


def test_ambiguous_matching_usb_identity_refuses_to_attach(reset_board):
    board, _, port = reset_board
    with (
        patch("serial.tools.list_ports.comports", return_value=[port, port]),
        patch("hardware.validation.reset_capture_board.reset_serial_connection") as factory,
    ):
        with pytest.raises(RuntimeError, match="Ambiguous USB identity"):
            board.read_live_message(0.1)
    factory.assert_not_called()


def test_transient_open_failure_retries_without_probe(reset_board):
    board, evidence, port = reset_board
    frame = hello("new").model_dump_json().encode() + b"\n"
    with (
        patch("serial.tools.list_ports.comports", return_value=[port]),
        patch("hardware.validation.reset_capture_board.reset_serial_connection", side_effect=[SerialException("not ready"), FakeSerial([frame])]),
        patch("subprocess.run", side_effect=AssertionError("No probe permitted")),
    ):
        assert board.read_live_message(0.1).boot_id == "new"
    recovery = json.loads((evidence.output_dir / "usb_reset_recovery.json").read_text())
    assert recovery["open_attempts"] == 2
    assert recovery["reconnected"]
    assert recovery["open_errors"] == ["not ready"]


def test_second_disconnect_does_not_start_another_recovery(reset_board):
    board, _, port = reset_board
    replacement = FakeSerial([hello("new").model_dump_json().encode() + b"\n"])
    with (
        patch("serial.tools.list_ports.comports", return_value=[port]),
        patch("hardware.validation.reset_capture_board.reset_serial_connection", return_value=replacement) as factory,
    ):
        assert board.read_live_message(0.1).boot_id == "new"
        replacement.disconnected = True
        with pytest.raises(SerialException):
            board.read_live_message(0.1)
    factory.assert_called_once_with()


def test_partial_old_frame_is_preserved_and_prevents_pass(reset_board):
    board, evidence, port = reset_board
    partial = b'{"incomplete":'
    board._serial_partial.extend(partial)
    evidence.capture("device_to_host", partial, board.port_name)
    frame = hello("new").model_dump_json().encode() + b"\n"
    with (
        patch("serial.tools.list_ports.comports", return_value=[port]),
        patch("hardware.validation.reset_capture_board.reset_serial_connection", return_value=FakeSerial([frame])),
    ):
        assert board.read_live_message(0.1).boot_id == "new"
    recovery = json.loads((evidence.output_dir / "usb_reset_recovery.json").read_text())
    assert recovery["partial_frame_hex"] == partial.hex()
    assert not evidence.passed
    assert any(
        r.name == "partial_frame_at_reset" and r.status == "AMBIGUOUS" for r in evidence.results
    )


def test_readiness_requires_usb_serial_identity(reset_board):
    board, _, port = reset_board
    port.serial_number = None
    with patch("serial.tools.list_ports.comports", return_value=[port]):
        with pytest.raises(RuntimeError, match="verified USB serial identity"):
            board.enable_reset_recovery()


@pytest.fixture
def windows_serial_api(monkeypatch):
    """Model the Windows receive queue while exercising real pySerial open/read."""
    if sys.platform != "win32":
        pytest.skip("Windows serial backend regression")
    from serial import win32

    queue = bytearray()
    purges = []
    closed = []
    handles = iter(range(200, 220))

    def purge(handle, flags):
        purges.append(flags)
        if flags & win32.PURGE_RXCLEAR:
            queue.clear()
        return True

    def pending(handle, flags, status):
        status._obj.cbInQue = len(queue)
        return True

    def read(handle, target, count, received, overlapped):
        data = bytes(queue[:count])
        del queue[:count]
        ctypes.memmove(target, data, len(data))
        received._obj.value = len(data)
        return True

    monkeypatch.setattr(win32, "CreateFile", lambda *args: 101)
    monkeypatch.setattr(win32, "CreateEvent", lambda *args: next(handles))
    for name in (
        "SetupComm", "GetCommTimeouts", "SetCommTimeouts", "SetCommMask",
        "GetCommState", "SetCommState", "EscapeCommFunction", "ResetEvent",
        "GetOverlappedResult", "CancelIoEx",
    ):
        monkeypatch.setattr(win32, name, lambda *args: True)
    monkeypatch.setattr(win32, "CloseHandle", lambda handle: closed.append(handle) or True)
    monkeypatch.setattr(win32, "PurgeComm", purge)
    monkeypatch.setattr(win32, "ClearCommError", pending)
    monkeypatch.setattr(win32, "ReadFile", read)
    return SimpleNamespace(queue=queue, purges=purges, closed=closed)


def test_windows_reset_open_retains_already_queued_hello(reset_board, windows_serial_api):
    board, evidence, port = reset_board
    frame = hello("new").model_dump_json().encode() + b"\n"
    windows_serial_api.queue.extend(frame)
    try:
        with (
            patch("serial.tools.list_ports.comports", return_value=[port]),
            patch("subprocess.run", side_effect=AssertionError("No reset/probe permitted")),
        ):
            message = board.read_live_message(0.05)
        assert isinstance(message, HelloMessage), "Windows open discarded the waiting startup hello"
        assert message.boot_id == "new"
        assert evidence.protocol_frames == [frame]
        assert not windows_serial_api.purges
    finally:
        # Close fake handles before the patched Win32 functions are restored.
        board.close()


def test_windows_reset_backend_does_not_change_normal_serial_open(windows_serial_api):
    import serial

    windows_serial_api.queue.extend(b"normal open still clears this")
    connection = serial.Serial()
    connection.port = "COM_TEST"
    try:
        connection.open()
        assert not windows_serial_api.queue
        assert windows_serial_api.purges == [15]
    finally:
        connection.close()


@pytest.mark.parametrize("failure", ["CreateEvent", "SetupComm", "GetCommTimeouts", "SetCommState"])
def test_windows_reset_open_failure_closes_all_acquired_handles(
    windows_serial_api, monkeypatch, failure
):
    from serial import win32
    from hardware.validation.windows_reset_serial import WindowsResetSerial

    created_events = []
    create = win32.CreateEvent

    def create_event(*args):
        if failure == "CreateEvent" and created_events:
            return 0
        event = create(*args)
        created_events.append(event)
        return event

    monkeypatch.setattr(win32, "CreateEvent", create_event)
    if failure != "CreateEvent":
        monkeypatch.setattr(win32, failure, lambda *args: False)
    connection = WindowsResetSerial()
    connection.port = "COM_TEST"
    with pytest.raises(SerialException):
        connection.open()
    assert not connection.is_open
    assert connection._port_handle is None
    assert connection._overlapped_read is connection._overlapped_write is None
    assert sorted(windows_serial_api.closed) == sorted([101, *created_events])
    assert not windows_serial_api.purges


def test_windows_reset_backend_rejects_unreviewed_pyserial(windows_serial_api, monkeypatch):
    import serial
    from hardware.validation.windows_reset_serial import WindowsResetSerial

    monkeypatch.setattr(serial, "VERSION", "unreviewed")
    connection = WindowsResetSerial()
    connection.port = "COM_TEST"
    with pytest.raises(SerialException, match="pySerial 3.5"):
        connection.open()
    assert not connection.is_open
    assert connection._port_handle is None
