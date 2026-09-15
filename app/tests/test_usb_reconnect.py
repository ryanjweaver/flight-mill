"""Operator USB-reconnect logic using enumerator records and injected endpoints.

No OS serial port, real USB operation, or application UI is exercised.
"""
import pytest
from fastapi.testclient import TestClient

from flightmill.acquisition.serial_errors import SerialPortNotReadyError
from flightmill.acquisition.serial_transport import SerialTransport
from flightmill.acquisition.service import AcquisitionService
from flightmill.acquisition.usb_reconnect import USBReconnectWaiter
from flightmill.api.app import create_app
from test_serial_acquisition import Clock, Endpoint, pump, start

PORT = {"port":"MOCK3", "vid":0x303A, "pid":0x1001,
        "serial_number":"SOFTWARE_ONLY_USB", "candidate":True}


def make_replug_service(tmp_path, *, fresh=True, open_failures=()):
    clock = Clock()
    ports = [dict(PORT)]

    class CountedEndpoint(Endpoint):
        opens = 0

        def open(self):
            self.opens += 1
            if self.opens <= len(open_failures):
                raise open_failures[self.opens - 1]
            super().open()

    endpoint = CountedEndpoint(clock, fresh=fresh)
    service = AcquisitionService(tmp_path, clock=clock,
        port_enumerator=lambda:list(ports),
        serial_factory=lambda port:SerialTransport(port, serial_factory=lambda:endpoint))
    service.action('select_source', {'source':'serial'})
    return service, endpoint, clock, ports


def advance_ports(service, clock, ports, values):
    ports[:] = values
    clock.advance(.2)
    service.tick()
    return service.snapshot()


def test_default_serial_can_prepare_reconnect_without_manual_port(tmp_path):
    clock = Clock()
    endpoint = Endpoint(clock)
    service = AcquisitionService(tmp_path, clock=clock,
        port_enumerator=lambda: [dict(PORT)],
        serial_factory=lambda port: SerialTransport(port, serial_factory=lambda: endpoint))
    try:
        result = service.action('connect_after_replug')
        assert result['selected_port'] == 'MOCK3'
        assert result['connection_state'] == 'waiting_for_unplug'
        assert not endpoint.opened.is_set() and endpoint.writes == []
        assert service.action('disconnect')['connection_state'] == 'disconnected'
    finally:
        service.close()


def test_replug_waits_for_removal_and_same_identity_then_handshakes_once(tmp_path):
    service, endpoint, clock, ports = make_replug_service(tmp_path)
    try:
        result = service.action('connect_after_replug')
        assert result['connection_state'] == 'waiting_for_unplug'
        clock.advance(10)  # Operator wait must not consume the five-second handshake window.
        service.tick()
        assert endpoint.opens == 0 and endpoint.writes == []
        result = advance_ports(service, clock, ports, [])
        assert result['connection_state'] == 'waiting_for_replug'
        result = advance_ports(service, clock, ports, [{**PORT, 'serial_number':'DIFFERENT_USB'}])
        assert result['connection_state'] == 'waiting_for_replug'
        assert endpoint.opens == 0
        advance_ports(service, clock, ports, [{**PORT, 'port':'MOCK7'}])
        result = pump(service, lambda s:s['ready'])
        assert result['selected_port'] == 'MOCK7'
        assert endpoint.port == 'MOCK7'
        assert endpoint.opens == 1
        assert result['state'] == 'IDLE' and result['device']['firmware_version'] == '0.1.3-dev'
        assert [command.type for command in endpoint.writes] == ['get_status']
        assert not list(tmp_path.glob('*_journal.json'))
        for _ in range(10):
            clock.advance(.1)
            service.tick()
        assert endpoint.opens == 1
    finally:
        service.close()


@pytest.mark.parametrize('cancel', ['disconnect', 'close'])
def test_cancel_never_opens_when_device_returns_later(tmp_path, cancel):
    service, endpoint, clock, ports = make_replug_service(tmp_path)
    try:
        service.action('connect_after_replug')
        advance_ports(service, clock, ports, [])
        if cancel == 'close':
            service.close()
        else:
            service.action('disconnect')
        result = advance_ports(service, clock, ports, [PORT])
        assert result['connection_state'] == 'disconnected'
        assert endpoint.opens == 0 and endpoint.writes == []
    finally:
        service.close()


@pytest.mark.parametrize('action', ['connect', 'connect_after_replug', 'select_source', 'arm', 'discover'])
def test_other_actions_are_locked_while_usb_reconnect_is_pending(tmp_path, action):
    service, endpoint, _, _ = make_replug_service(tmp_path)
    try:
        service.action('connect_after_replug')
        with pytest.raises(ValueError, match='USB reconnect is pending'):
            service.action(action, {'source':'simulation'})
        assert endpoint.opens == 0
    finally:
        service.close()


@pytest.mark.parametrize('case', ['absent', 'no_serial', 'unsupported', 'ambiguous'])
def test_prepare_rejects_unavailable_or_nonunique_usb_identity(case):
    ports = [dict(PORT)]
    if case == 'absent':
        ports = []
    elif case == 'no_serial':
        ports[0]['serial_number'] = None
    elif case == 'unsupported':
        ports[0]['vid'] = 1234
    else:
        ports.append({**PORT, 'port':'MOCK7'})
    with pytest.raises(ValueError):
        USBReconnectWaiter(ports, PORT['port'])


@pytest.mark.parametrize('case', ['timeout', 'ambiguous', 'enumeration_error'])
def test_wait_failures_do_not_open_or_get_reported_as_storage_failures(tmp_path, case):
    service, endpoint, clock, ports = make_replug_service(tmp_path)
    try:
        service.action('connect_after_replug')
        advance_ports(service, clock, ports, [])
        if case == 'timeout':
            clock.advance(61)
        elif case == 'ambiguous':
            ports[:] = [PORT, {**PORT, 'port':'MOCK7'}]
        else:
            def broken_enumerator():
                raise OSError('Enumeration unavailable')
            service._port_enumerator = broken_enumerator
        clock.advance(.2)
        service.tick()
        result = service.snapshot()
        assert not result['connected'] and result['connection_state'] == 'disconnected'
        assert endpoint.opens == 0
        assert [warning['code'] for warning in result['warnings']] == ['connection_failed']
        assert not list(tmp_path.glob('*_journal.json'))
    finally:
        service.close()


def test_replug_without_hello_still_cannot_arm(tmp_path):
    service, endpoint, clock, ports = make_replug_service(tmp_path, fresh=False)
    try:
        service.action('connect_after_replug')
        advance_ports(service, clock, ports, [])
        advance_ports(service, clock, ports, [PORT])
        pump(service, lambda s:any(d['message'].startswith('Discovery awaiting') for d in s['diagnostics']))
        clock.advance(6)
        service.tick()
        assert not service.snapshot()['ready']
        with pytest.raises(ValueError):
            service.action('arm')
        assert [command.type for command in endpoint.writes] == ['get_status']
    finally:
        service.close()


def test_replug_is_rejected_during_an_active_recording(tmp_path):
    service, endpoint, _, _ = make_replug_service(tmp_path)
    try:
        service.action('connect')
        pump(service, lambda s:s['ready'])
        start(service)
        count = len(endpoint.writes)
        with pytest.raises(ValueError, match='no active trial'):
            service.action('connect_after_replug')
        assert service.snapshot()['state'] == 'RECORDING'
        assert endpoint.opens == 1 and len(endpoint.writes) == count
    finally:
        service.close()


def test_api_exposes_guided_reconnect_and_cancel_without_opening_a_port(tmp_path):
    service, endpoint, _, _ = make_replug_service(tmp_path)
    with TestClient(create_app(tmp_path, service=service), base_url='http://127.0.0.1') as client:
        token = client.get('/api/session').json()['token']
        headers = {'X-Flightmill-Token':token}
        assert 'id="connect-after-replug"' in client.get('/').text
        result = client.post('/api/action', headers=headers, json={'action':'connect_after_replug'})
        assert result.status_code == 200
        assert result.json()['connection_state'] == 'waiting_for_unplug'
        assert not result.json()['ready']
        cancelled = client.post('/api/action', headers=headers, json={'action':'disconnect'})
        assert cancelled.status_code == 200
        assert cancelled.json()['connection_state'] == 'disconnected'
        assert endpoint.opens == 0 and endpoint.writes == []


def replug(service, clock, ports):
    service.action('connect_after_replug')
    advance_ports(service, clock, ports, [])
    advance_ports(service, clock, ports, [PORT])


def test_brief_prehandle_unavailability_then_one_strict_handshake(tmp_path):
    service, endpoint, clock, ports = make_replug_service(tmp_path,
        open_failures=[SerialPortNotReadyError('WinError 2')] * 2)
    try:
        replug(service, clock, ports)
        for attempt in (1, 2):
            result = pump(service, lambda s:s['connection_state'] == 'waiting_for_port')
            assert endpoint.opens == attempt and endpoint.writes == []
            assert not result['ready'] and not result['device']
            for _ in range(30):
                service.tick()
            assert endpoint.opens == attempt, 'Polling cannot cause a tight retry loop'
            advance_ports(service, clock, ports, [{**PORT, 'port':'MOCK7'}])
        result = pump(service, lambda s:s['ready'])
        assert result['selected_port'] == 'MOCK7'
        assert result['state'] == 'IDLE' and endpoint.opens == 3
        assert [command.type for command in endpoint.writes] == ['get_status']
        assert not result['warnings'] and not list(tmp_path.glob('*_journal.json'))
        # A failure after connection must not reopen, even if its exception has
        # the same type as the allowed pre-handle failure.
        endpoint.read_error = SerialPortNotReadyError('later read failure')
        pump(service, lambda s:not s['connected'])
        clock.advance(3)
        service.tick()
        assert endpoint.opens == 3
    finally:
        service.close()


@pytest.mark.parametrize('case', ['wrong_identity', 'ambiguous', 'deadline'])
def test_prehandle_retry_rechecks_identity_and_has_a_fixed_deadline(tmp_path, case):
    service, endpoint, clock, ports = make_replug_service(tmp_path,
        open_failures=[SerialPortNotReadyError('not present')] * 20)
    try:
        replug(service, clock, ports)
        pump(service, lambda s:s['connection_state'] == 'waiting_for_port')
        if case == 'wrong_identity':
            advance_ports(service, clock, ports, [{**PORT, 'serial_number':'OTHER'}])
            assert endpoint.opens == 1
            clock.advance(2)
        elif case == 'ambiguous':
            ports[:] = [PORT, {**PORT, 'port':'MOCK7'}]
            clock.advance(.2)
        else:
            clock.advance(2)
        service.tick()
        result = service.snapshot()
        assert endpoint.opens == 1 and endpoint.writes == []
        assert result['connection_state'] == 'disconnected'
        assert [warning['code'] for warning in result['warnings']] == ['connection_failed']
        assert not list(tmp_path.glob('*_journal.json'))
    finally:
        service.close()


def test_port_wait_has_an_attempt_limit_even_if_clock_does_not_advance():
    clock = Clock()
    waiter = USBReconnectWaiter([PORT], PORT['port'], clock=clock)
    assert waiter.poll([]) is None
    for _ in range(20):
        assert waiter.poll([PORT]) == PORT['port']
    with pytest.raises(TimeoutError):
        waiter.poll([PORT])


@pytest.mark.parametrize('cancel', ['disconnect', 'close'])
@pytest.mark.parametrize('phase', ['waiting_for_replug', 'waiting_for_port'])
def test_cancel_takes_precedence_when_next_open_is_due(tmp_path, cancel, phase):
    service, endpoint, clock, ports = make_replug_service(tmp_path,
        open_failures=[SerialPortNotReadyError('not present')])
    try:
        service.action('connect_after_replug')
        advance_ports(service, clock, ports, [])
        if phase == 'waiting_for_port':
            advance_ports(service, clock, ports, [PORT])
            pump(service, lambda s:s['connection_state'] == 'waiting_for_port')
        opens = endpoint.opens
        ports[:] = [PORT]
        clock.advance(.2)
        if cancel == 'disconnect':
            service.action('disconnect')
        else:
            service.close()
        service.tick()
        assert endpoint.opens == opens and endpoint.writes == []
        assert service.snapshot()['connection_state'] == 'disconnected'
    finally:
        service.close()


@pytest.mark.parametrize('error', [PermissionError('busy port'),
    FileNotFoundError('unclassified missing file'), OSError('configuration failed')])
def test_guided_reconnect_does_not_retry_other_errors(tmp_path, error):
    service, endpoint, clock, ports = make_replug_service(tmp_path, open_failures=[error])
    try:
        replug(service, clock, ports)
        pump(service, lambda s:bool(s['warnings']))
        clock.advance(.2)
        service.tick()
        assert endpoint.opens == 1 and endpoint.writes == []
        assert service.snapshot()['connection_state'] == 'disconnected'
    finally:
        service.close()


def test_ordinary_connect_does_not_retry_prehandle_failure(tmp_path):
    service, endpoint, clock, _ = make_replug_service(tmp_path,
        open_failures=[SerialPortNotReadyError('not present')])
    try:
        service.action('connect')
        pump(service, lambda s:bool(s['warnings']))
        clock.advance(.2)
        service.tick()
        assert endpoint.opens == 1 and endpoint.writes == []
    finally:
        service.close()


def test_prehandle_retry_does_not_weaken_missing_hello_rejection(tmp_path):
    service, endpoint, clock, ports = make_replug_service(tmp_path, fresh=False,
        open_failures=[SerialPortNotReadyError('not present')])
    try:
        replug(service, clock, ports)
        pump(service, lambda s:s['connection_state'] == 'waiting_for_port')
        advance_ports(service, clock, ports, [PORT])
        pump(service, lambda s:any(d['message'].startswith('Discovery awaiting') for d in s['diagnostics']))
        clock.advance(6)
        service.tick()
        assert not service.snapshot()['ready'] and endpoint.opens == 2
        with pytest.raises(ValueError):
            service.action('arm')
        assert [command.type for command in endpoint.writes] == ['get_status']
        assert not list(tmp_path.glob('*_journal.json'))
    finally:
        service.close()
