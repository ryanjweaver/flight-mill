"""Application copy of the accepted Win32 correction, with mocked OS handles."""
import ctypes
import sys

import pytest

from test_reset_capture_board import windows_serial_api  # noqa: F401


@pytest.mark.parametrize('failure', [None, 'SetupComm', 'GetCommTimeouts', 'SetCommState'])
def test_application_win32_reader_preserves_startup_or_closes_handles(windows_serial_api, monkeypatch, failure):  # noqa: F811
    if sys.platform != 'win32':
        pytest.skip('Windows serial backend regression')
    from serial import SerialException, win32
    from flightmill.acquisition.windows_serial import WindowsResetSerial
    connection = WindowsResetSerial()
    connection.port = 'COM_SOFTWARE_TEST'
    connection.timeout = 0
    frame = b'queued startup evidence\n'
    windows_serial_api.queue.extend(frame)
    if failure:
        monkeypatch.setattr(win32, failure, lambda *args:False)
        with pytest.raises(SerialException):
            connection.open()
        assert 101 in windows_serial_api.closed
        assert not connection.is_open
    else:
        try:
            connection.open()
            assert connection.read(len(frame)) == frame
        finally:
            connection.close()
    assert not windows_serial_api.purges


@pytest.mark.parametrize('error_code', [2, 5, 32, 31])
def test_only_prehandle_file_not_found_is_classified_for_retry(windows_serial_api, monkeypatch, error_code):  # noqa: F811
    from serial import SerialException, win32
    from flightmill.acquisition.serial_errors import SerialPortNotReadyError
    from flightmill.acquisition.windows_serial import WindowsResetSerial
    connection = WindowsResetSerial()
    connection.port = 'COM_SOFTWARE_TEST'
    error = ctypes.WinError(error_code)
    monkeypatch.setattr(ctypes, 'WinError', lambda:error)
    monkeypatch.setattr(win32, 'CreateFile', lambda *args:win32.INVALID_HANDLE_VALUE)
    expected = SerialPortNotReadyError if error_code == 2 else SerialException
    with pytest.raises(expected) as raised:
        connection.open()
    assert raised.value.__cause__ is error
    assert not connection.is_open and not windows_serial_api.closed
    assert not windows_serial_api.purges


def test_file_not_found_after_handle_acquisition_is_not_retryable(windows_serial_api, monkeypatch):  # noqa: F811
    from serial import SerialException, win32
    from flightmill.acquisition.serial_errors import SerialPortNotReadyError
    from flightmill.acquisition.windows_serial import WindowsResetSerial
    connection = WindowsResetSerial()
    connection.port = 'COM_SOFTWARE_TEST'
    error = ctypes.WinError(2)
    monkeypatch.setattr(ctypes, 'WinError', lambda:error)
    monkeypatch.setattr(win32, 'SetupComm', lambda *args:False)
    with pytest.raises(SerialException) as raised:
        connection.open()
    assert not isinstance(raised.value, SerialPortNotReadyError)
    assert not connection.is_open and 101 in windows_serial_api.closed
    assert not windows_serial_api.purges
