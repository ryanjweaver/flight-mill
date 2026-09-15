"""Injected endpoint tests use the real SerialTransport worker, never an OS port."""
from __future__ import annotations

import base64
import json
import threading
import time
from collections import deque
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from flightmill.acquisition.serial_transport import SerialTransport, discover_ports, select_port
from flightmill.acquisition.service import AcquisitionService
from flightmill.api.app import create_app
from flightmill.protocol.models import AckMessage, TrialArmedMessage, parse_line, serialize_line
from flightmill.storage.trial_writer import discover_trials
from simulator.simulated_device import SimulatedDevice


class Clock:
    def __init__(self):
        self.now = 100.0
    def __call__(self):
        return self.now
    def advance(self, seconds):
        self.now += seconds


class Endpoint:
    """In-memory pySerial-shaped endpoint, with explicit firmware-model identity."""
    def __init__(self, clock, *, fresh=True):
        self.device = SimulatedDevice(device_id="FM-MOCK-SOFTWARE-ONLY", clock=clock)
        self.device.firmware_version = "0.1.3-dev"
        self.fresh = fresh
        self.frames = deque()
        self.lock = threading.Lock()
        self.writes = []
        self.response_filter = lambda messages: messages
        self.open_error = None
        self.read_error = None
        self.closed = False
        self.opened = threading.Event()
        self.hello_transform = lambda raw: raw
        self.timeout_count = 0

    def feed(self, *frames):
        with self.lock:
            self.frames.extend(frames)

    def emit(self, *messages):
        self.feed(*(serialize_line(m) for m in messages))

    def open(self):
        if self.open_error:
            raise self.open_error
        self.closed = False
        hello = self.device.boot()
        if self.fresh:
            self.feed(self.hello_transform(serialize_line(hello)))
        self.opened.set()

    @property
    def in_waiting(self):
        with self.lock:
            return sum(map(len, self.frames))

    def read(self, size):
        if self.read_error:
            raise self.read_error
        with self.lock:
            if self.frames:
                raw = self.frames.popleft()
                if len(raw) > size:
                    self.frames.appendleft(raw[size:])
                return raw[:size]
        time.sleep(0.001)
        self.timeout_count += 1
        return b""

    def write(self, raw):
        command = parse_line(raw)
        self.writes.append(command)
        self.emit(*self.response_filter(self.device.process(command)))
        return len(raw)

    def cancel_read(self):
        pass

    def close(self):
        self.closed = True


def pump(service, condition, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        service.tick()
        result = service.snapshot()
        if condition(result):
            return result
        time.sleep(0.002)
    raise AssertionError(service.snapshot())


def wait_wire(service):
    """Deliver injected bytes before evaluating a jumped deterministic deadline."""
    deadline = time.monotonic() + 2
    while not service.transport._queue_bytes and time.monotonic() < deadline:
        time.sleep(.001)
    assert service.transport._queue_bytes


def make_service(tmp_path, endpoint, clock):
    return AcquisitionService(tmp_path, clock=clock,
        serial_factory=lambda port: SerialTransport(port, serial_factory=lambda: endpoint),
        port_enumerator=lambda: [{"port": "MOCK9", "candidate": True}])


@pytest.mark.parametrize("original_source", ["simulation", "serial"])
@pytest.mark.parametrize("failure", ["enumeration", "missing", "ambiguous", "factory"])
def test_failed_source_selection_preserves_previous_transport(tmp_path, original_source, failure):
    clock = Clock()
    endpoint = Endpoint(clock)
    service = make_service(tmp_path, endpoint, clock)
    try:
        service.action("select_source", {"source": original_source})
        original_transport = service.transport
        original_ports = service._ports

        def fail(*args):
            raise OSError("Injected selection failure")

        if failure == "enumeration":
            service._port_enumerator = fail
        elif failure == "missing":
            service._port_enumerator = lambda: []
        elif failure == "ambiguous":
            service._port_enumerator = lambda: [
                {"port": "MOCK10", "candidate": True},
                {"port": "MOCK11", "candidate": True},
            ]
        else:
            service._serial_factory = fail

        with pytest.raises((OSError, ValueError)):
            service.action("select_source", {"source": "serial"})
        assert service.source == original_source
        assert service.transport is original_transport
        assert service._ports is original_ports
        assert not endpoint.opened.is_set()

        # A rejected selection leaves the previous source usable, without a
        # source/transport mismatch or an implicit switch to another port.
        service.action("connect")
        state = pump(service, lambda value: value["ready"])
        assert state["source"] == original_source
        assert state["state"] == "IDLE"
        assert state["selected_port"] == ("MOCK9" if original_source == "serial" else None)
    finally:
        service.close()


def test_default_serial_is_idle_until_connect_resolves_unique_candidate(tmp_path):
    clock = Clock()
    endpoint = Endpoint(clock)
    enumerations = []

    def enumerate_ports():
        enumerations.append(True)
        return [{"port": "MOCK9", "candidate": True}]

    service = AcquisitionService(tmp_path, clock=clock,
        serial_factory=lambda port: SerialTransport(port, serial_factory=lambda: endpoint),
        port_enumerator=enumerate_ports)
    try:
        service.tick()
        initial = service.snapshot()
        assert initial["source"] == "serial"
        assert initial["selected_port"] == ""
        assert initial["state"] == "DISCONNECTED" and not initial["ready"]
        assert initial["simulation"]["mode"] == "serial"
        assert not enumerations and not endpoint.opened.is_set()
        service.action("connect")
        connected = pump(service, lambda s: s["ready"])
        assert connected["selected_port"] == "MOCK9"
        assert connected["device"]["device_id"] == "FM-MOCK-SOFTWARE-ONLY"
        assert len(enumerations) == 1
    finally:
        service.close()


@pytest.mark.parametrize("ports", [[], [
    {"port": "MOCK1", "candidate": True}, {"port": "MOCK2", "candidate": True},
]])
def test_default_serial_requires_manual_port_without_unique_candidate(tmp_path, ports):
    clock = Clock()
    endpoint = Endpoint(clock)
    service = AcquisitionService(tmp_path, clock=clock,
        serial_factory=lambda port: SerialTransport(port, serial_factory=lambda: endpoint),
        port_enumerator=lambda: ports)
    try:
        with pytest.raises(ValueError, match="no unique USB candidate"):
            service.action("connect")
        assert not endpoint.opened.is_set()
        assert service.snapshot()["source"] == "serial"
        assert service.snapshot()["connection_state"] == "disconnected"
    finally:
        service.close()


@pytest.fixture
def serial_live(tmp_path):
    clock = Clock()
    endpoint = Endpoint(clock)
    service = make_service(tmp_path, endpoint, clock)
    service.action("select_source", {"source": "serial"})
    service.action("connect")
    pump(service, lambda s: s["ready"])
    yield service, endpoint, clock
    service.close()


def start(service, **setup):
    service.action("arm", setup)
    pump(service, lambda s: s["state"] == "ARMED" and not s["pending"])
    service.action("start")
    pump(service, lambda s: s["state"] == "RECORDING")


def test_enumeration_does_not_open_and_requires_manual_for_ambiguous_unknown():
    def port(name, vid=None, pid=None):
        return SimpleNamespace(device=name, vid=vid, pid=pid)
    ports = discover_ports(lambda: [port("COM7", 0x303A, 0x1001), port("COM8")])
    assert select_port(ports) == "COM7"
    assert ports[1]["serial_number"] is None
    assert select_port(ports, "COM8") == "COM8"
    for candidates in ([], [ports[0], {**ports[0], "port": "COM9"}]):
        with pytest.raises(ValueError, match="manually"):
            select_port(candidates)
    assert select_port([{**ports[0], "port": "COM17"}]) == "COM17"
    with pytest.raises(ValueError):
        select_port([], "socket://remote")


@pytest.mark.parametrize("mode,interval", [("default_150ms",150000), ("legacy_50ms",50000)])
def test_serial_default_legacy_and_physical_button_zero_trial(serial_live, tmp_path, mode, interval):
    service, endpoint, clock = serial_live
    assert service.snapshot()["configuration"]["confirmed_interval_us"] is None
    service.action("arm", {"interval_mode":mode, "arm_radius_cm":12})
    # The reservation is real and precedes delivery of ARM to the endpoint.
    assert list(tmp_path.glob('*.partial.csv'))
    pump(service, lambda s: s["state"] == "ARMED")
    assert service.snapshot()["configuration"]["confirmed_interval_us"] == interval
    assert next(c for c in endpoint.writes if c.type == "arm").config.min_event_interval_us == interval
    endpoint.emit(*endpoint.device.physical_button(100))
    pump(service, lambda s: s["state"] == "RECORDING")
    clock.advance(60)
    endpoint.emit(endpoint.device.heartbeat(), *endpoint.device.physical_button(1500))
    wait_wire(service)
    result = pump(service, lambda s: s["save_status"] == "saved")
    assert result["metrics"]["row_count"] == 0
    bundle = discover_trials(tmp_path)[0]
    assert bundle["acquisition_mode"] == "serial"
    assert bundle["metadata"]["actual_duration_s"] == 60
    assert bundle["metadata"]["arm_radius_m"] == .12
    assert not list(tmp_path.glob('*_simulation*'))
    assert not bundle['incomplete']


def test_batched_fragmented_crlf_invalid_bytes_and_retained_audit(serial_live, tmp_path):
    service, endpoint, clock = serial_live
    start(service)
    frames = [serialize_line(endpoint.device.emit_event(t)).replace(b'\n', b'\r\n') for t in (0,150000,300000)]
    endpoint.feed(frames[0][:25])
    time.sleep(.01)
    service.tick()
    assert service.snapshot()["metrics"]["row_count"] == 0
    endpoint.feed(frames[0][25:] + frames[1] + frames[2], b'\xff\n')
    pump(service, lambda s: s["metrics"]["row_count"] == 3)
    clock.advance(1)
    service.action("stop")
    pump(service, lambda s: s["save_status"] == "incomplete")
    evidence = next(tmp_path.glob('*_protocol.partial.jsonl')).read_text()
    assert any(b'\xff' in base64.b64decode(r['raw_base64']) for r in map(json.loads,evidence.splitlines()) if r.get('kind') == 'protocol')
    assert any(w['code'] == 'malformed_protocol' for w in service.snapshot()['warnings'])


def test_timeout_is_not_loss_but_heartbeat_deadline_and_read_error_are(serial_live):
    service, endpoint, clock = serial_live
    start(service)
    time.sleep(.03)
    assert endpoint.timeout_count > 0
    assert service.snapshot()['connected']
    clock.advance(3)
    endpoint.emit(endpoint.device.heartbeat())
    pump(service, lambda s: not endpoint.in_waiting)
    service.tick()
    assert service.snapshot()['state'] == 'RECORDING'
    endpoint.read_error = OSError('unplugged endpoint')
    pump(service, lambda s: s['save_status'] == 'incomplete')
    assert service._metadata.actual_duration_s is None


@pytest.mark.parametrize('case', ['late', 'wrong_identity', 'wrong_protocol', 'old_firmware', 'occupied', 'missing'])
def test_handshake_rejects_unverified_device(tmp_path, case):
    clock = Clock()
    endpoint = Endpoint(clock, fresh=case != 'late')
    if case in {'occupied','missing'}:
        endpoint.open_error = OSError(case)
    if case == 'wrong_identity':
        endpoint.device.device_id = 'OTHER-INSTRUMENT'
    if case == 'old_firmware':
        endpoint.device.firmware_version = '0.1.2-dev'
    if case == 'wrong_protocol':
        endpoint.hello_transform = lambda raw: raw.replace(b'"protocol_version":1', b'"protocol_version":2')
    service = make_service(tmp_path, endpoint, clock)
    try:
        service.action('select_source', {'source':'serial'})
        service.action('connect')
        time.sleep(.025)
        service.tick()
        clock.advance(6)
        service.tick()
        assert not service.snapshot()['ready']
        with pytest.raises(ValueError):
            service.action('arm')
        assert not any(c.type == 'arm' for c in endpoint.writes)
        assert not list(tmp_path.glob('*_journal.json'))
    finally:
        service.close()


@pytest.mark.parametrize('mode', ['mismatch', 'missing', 'delayed', 'missing_ack'])
def test_arm_confirmation_and_delays(serial_live, mode):
    service, endpoint, clock = serial_live
    held = []
    def filter_responses(messages):
        result = []
        for msg in messages:
            if isinstance(msg, TrialArmedMessage):
                if mode == 'mismatch':
                    msg = msg.model_copy(update={'config':msg.config.model_copy(update={'min_event_interval_us':50000})})
                elif mode == 'missing':
                    continue
            if mode == 'missing_ack' and isinstance(msg,AckMessage):
                continue
            result.append(msg)
        if mode == 'delayed':
            held.extend(result)
            return []
        return result
    endpoint.response_filter = filter_responses
    service.action('arm')
    pump(service, lambda s: any(c.type == 'arm' for c in endpoint.writes))
    if mode == 'delayed':
        assert service.snapshot()['pending']
        with pytest.raises(ValueError):
            service.action('start')
        endpoint.emit(*held)
    if mode == 'missing':
        clock.advance(3.1)
        service.tick()
    result = pump(service, lambda s: s['state'] == 'ARMED' or s['save_status'] == 'incomplete')
    assert (result['state'] == 'ARMED') == (mode in {'delayed','missing_ack'})
    assert len([c for c in endpoint.writes if c.type == 'arm']) == 1


def test_source_lock_and_consecutive_serial_trials(serial_live):
    service, endpoint, clock = serial_live
    for attempt, mode in [(1,'legacy_50ms'), (2,'default_150ms')]:
        start(service, attempt=attempt, interval_mode=mode)
        with pytest.raises(ValueError):
            service.action('select_source', {'source':'simulation'})
        with pytest.raises(ValueError):
            service.action('validate', {'interval_mode':'legacy_50ms'})
        clock.advance(1)
        endpoint.emit(endpoint.device.emit_event(1_000_000))
        pump(service, lambda s: s['metrics']['row_count'] == 1)
        service.action('stop')
        pump(service, lambda s: s['save_status'] == 'saved')
        service.action('disarm')
        pump(service, lambda s: s['state'] == 'IDLE' and not s['pending'])
    assert len({c.session_id for c in endpoint.writes if c.type == 'arm'}) == 2


def test_boot_change_never_resumes_and_reconnect_requires_new_identity(serial_live):
    service, endpoint, clock = serial_live
    start(service)
    endpoint.emit(endpoint.device.boot())
    pump(service, lambda s: s['save_status'] == 'incomplete')
    assert not service.snapshot()['connected']
    service.action('connect')
    pump(service, lambda s: s['ready'])
    assert service.snapshot()['configuration']['confirmed_interval_us'] is None
    assert service.snapshot()['save_status'] == 'incomplete'


def test_one_worker_cannot_steal_owned_port(serial_live):
    service, endpoint, _ = serial_live
    competing = SerialTransport('mock9', serial_factory=lambda: endpoint)
    with pytest.raises(ValueError, match='owned'):
        competing.open()
    assert service.snapshot()['ready']


def test_api_serial_selection_and_export_provenance(tmp_path):
    clock, endpoint = Clock(), None
    endpoint = Endpoint(clock)
    service = make_service(tmp_path, endpoint, clock)
    with TestClient(create_app(tmp_path, service=service), base_url='http://127.0.0.1') as client:
        token = client.get('/api/session').json()['token']
        def action(name, **payload):
            result = client.post('/api/action', json={'action':name, **payload}, headers={'X-Flightmill-Token':token})
            assert result.status_code == 200, result.text
            return result.json()
        action('select_source', source='serial')
        action('connect')
        pump(service, lambda s:s['ready'])
        action('arm', interval_mode='legacy_50ms')
        pump(service, lambda s:s['state']=='ARMED')
        action('start')
        pump(service, lambda s:s['state']=='RECORDING')
        action('stop')
        result = pump(service, lambda s:s['save_status']=='saved')
        import io
        import zipfile
        archive = zipfile.ZipFile(io.BytesIO(client.get(f"/api/trials/{result['trial']['id']}/bundle").content))
        assert 'ACQUISITION_NOTICE.txt' in archive.namelist()
        assert not any('simulation' in name.lower() or name == 'SIMULATED_DATA.txt' for name in archive.namelist())


def test_storage_failure_requests_one_stop_and_never_cleans_bundle(serial_live, monkeypatch):
    from flightmill.storage.trial_writer import TrialWriter
    service, endpoint, _ = serial_live
    start(service)
    monkeypatch.setattr(TrialWriter, 'append_event', lambda *args: (_ for _ in ()).throw(OSError('disk full')))
    endpoint.emit(endpoint.device.emit_event(0))
    result = pump(service, lambda s:s['device_stop_confirmed'] is True)
    assert result['save_status'] == 'incomplete'
    assert result['trial']['duration_s'] is None
    assert len([c for c in endpoint.writes if c.type == 'stop']) == 1


def test_oversized_line_discards_until_newline_and_keeps_following_frames(serial_live):
    service, endpoint, _ = serial_live
    start(service)
    endpoint.feed(b'x' * 70000)
    pump(service, lambda s:any(w['code']=='oversize_line' for w in s['warnings']))
    endpoint.feed(b'ignored tail\n' + serialize_line(endpoint.device.emit_event(0)))
    pump(service, lambda s:s['metrics']['row_count']==1)
    service.action('stop')
    pump(service, lambda s:s['save_status']=='incomplete')


def test_queue_overflow_preserves_incomplete_evidence(serial_live):
    service, endpoint, _ = serial_live
    start(service)
    service.transport.max_queue_bytes = 16
    endpoint.emit(endpoint.device.emit_event(0))
    result = pump(service, lambda s:s['save_status']=='incomplete')
    assert any('overflow' in w['message'] for w in result['warnings'])


def test_unowned_active_trial_blocks_new_reservation(serial_live, tmp_path):
    from flightmill.protocol.models import ArmCommand, StartCommand
    from simulator.simulated_device import SESSION_ID, command_fields
    service, endpoint, _ = serial_live
    fields = {**command_fields(50), 'device_id':endpoint.device.device_id,
              'firmware_version':endpoint.device.firmware_version}
    endpoint.device.process(ArmCommand(session_id=SESSION_ID, **fields))
    endpoint.device.process(StartCommand(session_id=SESSION_ID, **fields))
    endpoint.emit(endpoint.device.status())
    result = pump(service, lambda s:not s['connected'])
    assert any(w['code']=='unowned_trial' for w in result['warnings'])
    assert not list(tmp_path.glob('*_journal.json'))


def test_synthetic_zero_event_session_replay_through_serial_worker(tmp_path):
    """Replay simulator-generated frames with fresh host request/session UUIDs.

    The fixture models a 26,287 us trial with no accepted events. It contains
    no device capture and supplies no physical-hardware acceptance evidence.
    """
    from pathlib import Path
    from flightmill.protocol.models import HostCommand
    fixture = Path(__file__).with_name('fixtures') / 'serial_zero_event_session.jsonl'
    messages = [parse_line(line) for line in fixture.read_bytes().splitlines()]
    initial = []
    groups = {}
    response = initial
    for message in messages:
        if isinstance(message, HostCommand):
            response = groups[message.type] = []
        else:
            response.append(message)
    class ReplayEndpoint(Endpoint):
        def open(self):
            self.emit(*initial)
        def write(self, raw):
            command = parse_line(raw)
            self.writes.append(command)
            if command.type == 'arm':
                self.session = command.session_id
            for message in groups[command.type]:
                updates = {}
                if getattr(message,'command',None) == command.type:
                    updates['request_id'] = command.request_id
                if getattr(message,'session_id',None):
                    updates['session_id'] = self.session
                self.emit(message.model_copy(update=updates))
            return len(raw)
    clock = Clock()
    endpoint = ReplayEndpoint(clock)
    service = make_service(tmp_path, endpoint, clock)
    try:
        service.action('select_source', {'source':'serial'})
        service.action('connect')
        pump(service, lambda s:s['ready'])
        start(service)
        service.action('stop')
        result = pump(service, lambda s:s['save_status']=='saved')
        assert result['trial']['duration_s'] == .026287
        assert result['metrics']['row_count'] == 0
        assert result['configuration']['confirmed_interval_us'] == 150000
    finally:
        service.close()


def test_failed_handshake_reports_startup_bytes_without_accepting_identity(tmp_path):
    clock = Clock()
    endpoint = Endpoint(clock, fresh=False)
    endpoint.feed(b'partial boot text\r\n')
    service = make_service(tmp_path, endpoint, clock)
    try:
        service.action('select_source', {'source':'serial'})
        service.action('connect')
        pump(service, lambda s:any(d['message'].startswith('Discovery awaiting') for d in s['diagnostics']))
        clock.advance(6)
        service.tick()
        result = service.snapshot()
        trace = [entry['message'] for entry in result['diagnostics']
                 if entry['message'].startswith('Serial handshake ')]
        assert any('partial boot text\\r\\n' in entry for entry in trace)
        assert any('get_status' in entry and ' out ' in entry for entry in trace)
        assert any('ack' in entry and ' in ' in entry for entry in trace)
        assert result['device'] == {} and not result['ready']
        assert not result['connected']
        assert not list(tmp_path.glob('*_journal.json'))
        assert [command.type for command in endpoint.writes] == ['get_status']
    finally:
        service.close()


def test_handshake_trace_limits_bytes_and_chunks_and_resets_for_next_connection(tmp_path):
    clock = Clock()
    endpoint = Endpoint(clock, fresh=False)
    service = make_service(tmp_path, endpoint, clock)
    try:
        service.action('select_source', {'source':'serial'})
        service._connection = 'handshaking'
        raw = b'x' * 2000 + b'UNLOGGED_SUFFIX'
        for _ in range(40):
            service._log_protocol('in', raw)
        trace = [entry['message'] for entry in service.snapshot()['diagnostics']
                 if entry['message'].startswith('Serial handshake ')]
        assert len(trace) == 16
        assert all('2015 bytes' in entry and 'truncated' in entry for entry in trace)
        assert all('UNLOGGED_SUFFIX' not in entry and len(entry) < 1200 for entry in trace)
        # The existing pretrial protocol audit still retains original chunks.
        assert service._prelog[-1] == ('in', raw)
        service.action('connect')
        pump(service, lambda s:sum(entry['message'].startswith('Serial handshake ')
                                  for entry in s['diagnostics']) > len(trace))
    finally:
        service.close()


def test_each_connection_reports_missing_hello_or_strict_ready(tmp_path):
    clock = Clock()
    endpoint = Endpoint(clock, fresh=False)
    service = make_service(tmp_path, endpoint, clock)
    try:
        # Missing hello remains a failure even with an ACK/status response.
        for _ in range(2):
            service.action("connect")
            pump(service, lambda value: value["connection_state"] == "handshaking")
            clock.advance(6)
            service.tick()
            state = service.snapshot()
            timeout = next(w for w in state["warnings"] if w["code"] == "handshake_timeout")
            assert "Device hello was not received" in timeout["message"]
            assert "Connect after USB reconnect" in timeout["message"]
            assert not state["ready"] and not state["connected"]

        assert sum(entry["message"].startswith("Handshake timed out for serial")
                   for entry in state["diagnostics"]) == 2
        assert not any("USB handshake ready" in entry["message"] for entry in state["diagnostics"])
        endpoint.fresh = True  # Inject a genuine new model boot; do not synthesize host identity.
        service.action("connect")
        state = pump(service, lambda value: value["ready"])
        outcomes = [entry["message"] for entry in state["diagnostics"]
                    if entry["message"].startswith("USB handshake ready")]
        assert len(outcomes) == 1
        assert state["device"]["boot_id"] in outcomes[0]
        assert "genuine hello and correlated get_status confirmed IDLE" in outcomes[0]
        assert all(command.type == "get_status" for command in endpoint.writes)
        assert not list(tmp_path.glob("*_journal.json"))
    finally:
        service.close()


def test_hello_without_correlated_status_has_distinct_timeout(tmp_path):
    clock = Clock()
    endpoint = Endpoint(clock)
    endpoint.response_filter = lambda messages: [m for m in messages if not isinstance(m, AckMessage)]
    service = make_service(tmp_path, endpoint, clock)
    try:
        service.action("connect")
        pump(service, lambda value: value["device"] and value["connection_state"] == "handshaking")
        clock.advance(6)
        service.tick()
        state = service.snapshot()
        timeout = next(w for w in state["warnings"] if w["code"] == "handshake_timeout")
        assert "Device hello received, but a fresh correlated get_status" in timeout["message"]
        assert not state["ready"] and not state["connected"]
        assert not any("USB handshake ready" in entry["message"] for entry in state["diagnostics"])
        with pytest.raises(ValueError):
            service.action("arm")
        assert not any(command.type == "arm" for command in endpoint.writes)
    finally:
        service.close()


def test_handshake_trace_stops_when_ready_and_does_not_log_trial_commands(serial_live):
    service, endpoint, clock = serial_live
    trace = [entry for entry in service.snapshot()['diagnostics']
             if entry['message'].startswith('Serial handshake ')]
    assert trace
    start(service, notes='private trial note')
    clock.advance(1)
    endpoint.emit(endpoint.device.emit_event(0))
    pump(service, lambda s:s['metrics']['row_count'] == 1)
    service.action('stop')
    result = pump(service, lambda s:s['save_status'] == 'saved')
    after = [entry for entry in result['diagnostics']
             if entry['message'].startswith('Serial handshake ')]
    assert after == trace
    assert all('private trial note' not in entry['message'] for entry in result['diagnostics'])
