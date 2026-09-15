from types import SimpleNamespace
from unittest.mock import Mock, patch

from hardware.validation.post_reset_status_diagnostic import attach_without_probe


def test_attach_has_normal_line_settings_and_never_runs_probe():
    connection = Mock()
    board = SimpleNamespace(port_name="COM_TEST", serial=None, evidence=Mock())

    def open_serial():
        assert connection.port == "COM_TEST"
        assert connection.baudrate == 115200
        assert connection.dtr is False
        assert connection.rts is False
        assert connection.dsrdtr is False
        assert connection.rtscts is False

    connection.open.side_effect = open_serial
    with (
        patch(
            "hardware.validation.post_reset_status_diagnostic.serial.Serial",
            return_value=connection,
        ),
        patch("subprocess.run", side_effect=AssertionError("No probe/reset permitted")),
    ):
        attach_without_probe(board)
    assert board.serial is connection
    connection.open.assert_called_once_with()
    assert connection.dtr is True
    assert connection.rts is False
    connection.write.assert_not_called()
    board.evidence.use_port.assert_called_once_with("COM_TEST")
