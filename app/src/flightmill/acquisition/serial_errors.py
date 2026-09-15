"""Narrow serial-open failure classification shared with the service worker."""


class SerialPortNotReadyError(OSError):
    """Windows CreateFile returned ERROR_FILE_NOT_FOUND without acquiring a handle."""
