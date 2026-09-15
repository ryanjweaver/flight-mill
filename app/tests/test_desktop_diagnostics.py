"""SOFTWARE ONLY: lifecycle diagnostics are persistent; user notes stay in bundles."""
import logging

from flightmill.acquisition.service import AcquisitionService
from flightmill.desktop.launcher import SessionTokenFilter


def test_lifecycle_diagnostics_reach_logging_without_user_notes(tmp_path, caplog):
    service = AcquisitionService(tmp_path)
    service.action('select_source', {'source': 'simulation'})
    with caplog.at_level(logging.INFO):
        service.action('connect')
        service.action('arm', {'species_code': 'SOFTWARE', 'notes': 'PRIVATE SETUP NOTE'})
        service.action('start')
        service.action('note', {'text': 'PRIVATE TIMESTAMPED NOTE'})
        service.action('stop')
        service.close()
    assert 'Trial saved' in caplog.text
    assert 'Note' in caplog.text
    assert 'PRIVATE' not in caplog.text


def test_diagnostics_filter_removes_websocket_token_from_formatted_record():
    record = logging.LogRecord('test', logging.INFO, '', 1,
                               'socket %s', ('/ws?token=EPHEMERAL&next=ok',), None)
    assert SessionTokenFilter().filter(record)
    assert 'EPHEMERAL' not in record.getMessage()
    assert 'token=[redacted]&next=ok' in record.getMessage()
