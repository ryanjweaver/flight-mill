"""SOFTWARE ONLY regressions for confirmed recording receipt during host STOPPING."""
import json
from uuid import uuid4

import pytest

from flightmill.protocol.models import EventMessage, serialize_line
from flightmill.storage.trial_writer import TrialWriter

from stop_tail_support import assert_clean, audit, connect, pump, run_case, start


@pytest.mark.parametrize('mode', ['default_150ms', 'legacy_50ms'])
@pytest.mark.parametrize('path', ['ui', 'planned', 'close', 'control'])
def test_stop_tail_matrix(tmp_path, mode, path):
    result = run_case(tmp_path / 'bundle', mode, path)
    assert result['conformance_passed'], result['conformance_issues']
    assert_clean(result, 2)
    assert result['stop_commands'] == 1
    assert not {'event_outside_recording', 'summary_count'} & set(result['warnings'])


@pytest.fixture
def boundary(tmp_path):
    service, endpoint, clock = connect(tmp_path)
    yield service, endpoint, clock, tmp_path
    endpoint.release_stop.set()
    service.close()


@pytest.mark.parametrize('delivery', ['separate', 'batch', 'fragmented'])
def test_multiple_tail_events_and_terminal_frames_preserve_wire_order(boundary, delivery):
    service, endpoint, clock, directory = boundary
    start(service, endpoint)
    endpoint.emit(endpoint.device.emit_event(0))
    pump(service, endpoint, lambda s: s['metrics']['row_count'] == 1)
    endpoint.times = (150000, 300000, 450000)
    endpoint.delivery = delivery
    clock.advance(1)
    service.action('stop')
    pump(service, endpoint, lambda s: s['save_status'] == 'saved')
    result = audit(directory)
    assert_clean(result, 4)
    assert [r['event_n'] for r in result['rows']] == ['1', '2', '3', '4']
    assert [r['dt_s'] for r in result['rows']] == ['0.000000'] + ['0.150000'] * 3
    types = [m.type for m in result['messages']]
    stop = types.index('stop')
    assert types[stop:] == ['stop', 'event', 'event', 'event', 'ack', 'trial_stopped']
    chunks = [entry['text'] for entry in result['entries'] if entry.get('direction') == 'in']
    if delivery == 'batch':
        assert any(all(word in chunk for word in ['"event_n":2', '"event_n":4',
                                                  '"command":"stop"', '"trial_stopped"'])
                   for chunk in chunks)
    if delivery == 'fragmented':
        assert any(not chunk.endswith('\n') for chunk in chunks)


@pytest.mark.parametrize('mode,interval', [('default_150ms', 150000), ('legacy_50ms', 50000)])
def test_first_only_event_arrives_during_stopping(boundary, mode, interval):
    service, endpoint, clock, directory = boundary
    start(service, endpoint, interval_mode=mode)
    endpoint.times = (interval,)
    clock.advance(1)
    service.action('stop')
    pump(service, endpoint, lambda s: s['save_status'] in {'saved', 'incomplete'})
    result = audit(directory)
    assert_clean(result, 1)
    assert float(result['rows'][0]['time_s']) == interval / 1_000_000


def test_repeated_stop_and_start_blocked_then_fresh_trial_rejects_old_tail(boundary):
    service, endpoint, clock, directory = boundary
    start(service, endpoint)
    old = endpoint.device.emit_event(0)
    endpoint.emit(old)
    pump(service, endpoint, lambda s: s['metrics']['row_count'] == 1)
    endpoint.times = (150000,)
    endpoint.release_stop.clear()
    clock.advance(1)
    service.action('stop')
    assert endpoint.stop_entered.wait(2)
    assert service.snapshot()['state'] == 'STOPPING'
    for action in ['stop', 'stop', 'start', 'arm']:
        with pytest.raises(ValueError, match='pending'):
            service.action(action)
    endpoint.release_stop.set()
    pump(service, endpoint, lambda s: s['save_status'] == 'saved')
    assert_clean(audit(directory), 2)
    assert len([c for c in endpoint.writes if c.type == 'stop']) == 1
    before = {p.name: p.read_bytes() for p in directory.iterdir()}
    service.action('disarm')
    pump(service, endpoint, lambda s: s['state'] == 'IDLE' and not s['pending'])
    next_directory = directory / 'next'
    service.action('validate', {'output_dir': str(next_directory), 'attempt': 2})
    start(service, endpoint, attempt=2)
    assert service.snapshot()['metrics']['row_count'] == 0
    assert service.snapshot()['metrics']['event_count'] == 0
    # A fresh sequence does not confer ownership on a prior trial's delayed data.
    seq = endpoint.device.status().message_seq
    endpoint.emit(old.model_copy(update={'message_seq': seq}))
    fresh = endpoint.device.emit_event(0)
    endpoint.emit(fresh)
    pump(service, endpoint, lambda s: s['metrics']['row_count'] == 1)
    endpoint.times = ()
    clock.advance(1)
    service.action('stop')
    pump(service, endpoint, lambda s: s['save_status'] == 'saved')
    result = audit(next_directory)
    assert len(result['rows']) == 1 and result['rows'][0]['speed_m_s'] == ''
    assert result['metadata']['incomplete'] == 'false'
    assert result['journal']['trial_uuid'] != str(old.session_id)
    assert 'session_mismatch' in {w['code'] for w in service.snapshot()['warnings']}
    assert before == {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()}


def test_physical_button_drains_events_before_terminal_summary(boundary):
    service, endpoint, clock, directory = boundary
    service.action('arm', {'species_code': 'SOFTWARE', 'notes': 'SOFTWARE ONLY'})
    pump(service, endpoint, lambda s: s['state'] == 'ARMED')
    endpoint.emit(*endpoint.device.physical_button(100))
    pump(service, endpoint, lambda s: s['state'] == 'RECORDING')
    clock.advance(1)
    tail = [endpoint.device.emit_event(t) for t in [0, 150000]]
    stopped = endpoint.device.physical_button(1500)
    endpoint.feed(b''.join(serialize_line(m) for m in [*tail, *stopped]))
    pump(service, endpoint, lambda s: s['save_status'] == 'saved')
    result = audit(directory)
    assert_clean(result, 2)
    assert result['metadata']['stop_reason'] == 'physical_button'
    assert not any(c.type in {'start', 'stop'} for c in endpoint.writes)


def test_observed_complete_status_after_lost_summary_has_bounded_failure(boundary):
    service, endpoint, clock, directory = boundary
    start(service, endpoint)
    clock.advance(1)
    endpoint.emit(endpoint.device.emit_event(0), endpoint.device.emit_event(150000))
    endpoint.device.physical_button(1500)  # Deliberately lose the terminal summary.
    endpoint.emit(endpoint.device.status())  # Actual firmware order: after drain/summary.
    pump(service, endpoint, lambda s: s['state'] == 'STOPPING')
    assert service.snapshot()['metrics']['row_count'] == 2
    assert service.snapshot()['pending']['action'] == 'stop'
    clock.advance(3.1)
    service.tick()
    result = audit(directory)
    assert result['metadata']['incomplete'] == 'true'
    assert result['metadata']['actual_duration_s'] == ''
    assert result['metadata']['stop_reason'] == 'stop_timeout'
    assert len(result['rows']) == 2
    assert not any(c.type == 'stop' for c in endpoint.writes)


@pytest.mark.parametrize('fault,warning', [
    ('session', 'session_mismatch'), ('duplicate_message', 'nonmonotonic_message'),
    ('duplicate_event', 'duplicate_event'), ('nonmonotonic_event', 'nonmonotonic_event'),
    ('nonmonotonic_time', 'nonmonotonic_event_time'),
])
def test_rejected_tail_messages_keep_existing_policy_and_trusted_baseline(boundary, fault, warning):
    service, endpoint, clock, directory = boundary
    start(service, endpoint)
    first = endpoint.device.emit_event(0)
    second = endpoint.device.emit_event(150000)
    endpoint.emit(first, second)
    pump(service, endpoint, lambda s: s['metrics']['row_count'] == 2)
    def inject():
        seq = endpoint.device.status().message_seq
        updates = {'message_seq': seq}
        if fault == 'session':
            updates.update(session_id=uuid4(), event_n=3, event_us=300000)
        elif fault == 'nonmonotonic_event':
            updates.update(event_n=1, event_us=300000, dt_us=0)
        elif fault == 'nonmonotonic_time':
            updates.update(event_n=3, event_us=149999, dt_us=100000)
        rejected = second.model_copy(update=updates) if fault != 'duplicate_message' else first
        endpoint.emit(rejected, endpoint.device.emit_event(300000))
    endpoint.before_stop = inject
    clock.advance(1)
    service.action('stop')
    pump(service, endpoint, lambda s: s['save_status'] in {'saved', 'incomplete'})
    result = audit(directory)
    assert [r['event_n'] for r in result['rows']] == ['1', '2', '3']
    assert result['metadata']['incomplete'] == 'false'
    assert warning in {w['code'] for w in service.snapshot()['warnings']}


@pytest.mark.parametrize('fault', ['interval_mismatch', 'interval_below_minimum',
                                  'summary_count', 'summary_duration', 'missing_summary',
                                  'invalid_summary', 'drops', 'missing_row', 'boot', 'disconnect'])
def test_real_tail_integrity_and_terminal_failures_stay_incomplete(boundary, fault):
    service, endpoint, clock, directory = boundary
    start(service, endpoint)
    endpoint.emit(endpoint.device.emit_event(0))
    pump(service, endpoint, lambda s: s['metrics']['row_count'] == 1)
    def inject():
        if fault == 'boot':
            endpoint.emit(endpoint.device.boot())
            return
        if fault == 'drops':
            endpoint.device.inject_dropped_events(1)
        tail = endpoint.device.emit_event(150000)
        if fault == 'interval_mismatch':
            tail = tail.model_copy(update={'event_us': 150001})
        elif fault == 'interval_below_minimum':
            tail = tail.model_copy(update={'event_us': 149999, 'dt_us': 149999})
        if fault != 'missing_row':
            endpoint.emit(tail)
    endpoint.before_stop = inject
    def responses(messages):
        result = []
        for message in messages:
            if message.type == 'trial_stopped':
                if fault in {'missing_summary', 'disconnect'}:
                    continue
                if fault == 'summary_count':
                    message = message.model_copy(update={'accepted_event_count': 3})
                elif fault == 'summary_duration':
                    message = message.model_copy(update={'duration_us': 1})
                elif fault == 'invalid_summary':
                    endpoint.feed(b'{"type":"trial_stopped"}\n')
                    continue
            result.append(message)
        return result
    endpoint.response_filter = responses
    clock.advance(1)
    service.action('stop')
    if fault in {'missing_summary', 'invalid_summary', 'disconnect'}:
        pump(service, endpoint, lambda s: s['metrics']['row_count'] == 2)
        if fault == 'disconnect':
            service.action('disconnect')
        else:
            clock.advance(3.1)
            service.tick()
    pump(service, endpoint, lambda s: s['save_status'] == 'incomplete')
    result = audit(directory)
    assert result['journal']['status'] == 'incomplete'
    assert result['metadata']['incomplete'] == 'true'
    expected = 1 if fault in {'interval_mismatch', 'interval_below_minimum', 'missing_row', 'boot'} else 2
    assert len(result['rows']) == expected
    if fault in {'boot', 'disconnect', 'missing_summary', 'invalid_summary'}:
        assert result['metadata']['actual_duration_s'] == ''
    assert len([c for c in endpoint.writes if c.type == 'stop']) == 1


@pytest.mark.parametrize('pending_start', [False, True])
def test_no_rows_before_confirmed_start(boundary, pending_start):
    service, endpoint, _, directory = boundary
    service.action('arm')
    pump(service, endpoint, lambda s: s['state'] == 'ARMED')
    if pending_start:
        endpoint.response_filter = lambda messages: [m for m in messages if m.type != 'trial_started']
        service.action('start')
        pump(service, endpoint, lambda s: any(c.type == 'start' for c in endpoint.writes))
    fields = endpoint.device.status().model_dump()
    fields = {k: v for k, v in fields.items() if k in EventMessage.model_fields}
    fields.update(type='event', state='RECORDING', event_n=1, event_us=0, dt_us=0)
    endpoint.emit(EventMessage.model_validate(fields))
    pump(service, endpoint, lambda s: any(w['code'] == 'event_outside_recording' for w in s['warnings']))
    assert service.snapshot()['metrics']['row_count'] == 0
    service.action('disconnect')
    result = audit(directory)
    assert result['rows'] == [] and result['metadata']['incomplete'] == 'true'


def test_post_terminal_events_cannot_append_or_reopen_bundle(boundary):
    service, endpoint, clock, directory = boundary
    start(service, endpoint)
    event = endpoint.device.emit_event(0)
    endpoint.emit(event)
    pump(service, endpoint, lambda s: s['metrics']['row_count'] == 1)
    clock.advance(1)
    service.action('stop')
    pump(service, endpoint, lambda s: s['save_status'] == 'saved')
    before = {p.name: p.read_bytes() for p in directory.iterdir()}
    sequence = endpoint.device.status().message_seq
    endpoint.emit(event.model_copy(update={'message_seq': sequence, 'event_n': 2,
                                          'event_us': 150000, 'dt_us': 150000}))
    pump(service, endpoint, lambda s: any(w['code'] == 'event_outside_recording' for w in s['warnings']))
    assert service.snapshot()['metrics']['row_count'] == 1
    assert before == {p.name: p.read_bytes() for p in directory.iterdir()}
    assert_clean(audit(directory), 1)


def test_storage_failure_during_tail_keeps_recovery_and_does_not_retry_stop(boundary, monkeypatch):
    service, endpoint, clock, directory = boundary
    start(service, endpoint)
    endpoint.emit(endpoint.device.emit_event(0))
    pump(service, endpoint, lambda s: s['metrics']['row_count'] == 1)
    original = TrialWriter.append_event
    def fail_tail(writer, event):
        if event.event_n == 2:
            raise OSError('SOFTWARE ONLY tail persistence failure')
        original(writer, event)
    monkeypatch.setattr(TrialWriter, 'append_event', fail_tail)
    endpoint.times = (150000, 300000)
    endpoint.delivery = 'batch'
    clock.advance(1)
    service.action('stop')
    pump(service, endpoint, lambda s: s['save_status'] == 'incomplete')
    service.close()
    result = audit(directory)
    assert len(result['rows']) == 1
    assert result['metadata']['incomplete'] == 'true'
    assert result['metadata']['actual_duration_s'] == ''
    assert result['metadata']['stop_reason'] == 'storage_failure'
    assert len([c for c in endpoint.writes if c.type == 'stop']) == 1
    assert [m.event_n for m in result['messages'] if m.type == 'event'] == [1, 2, 3]
    assert json.loads(next(directory.glob('*_journal.json')).read_bytes())['status'] == 'incomplete'
