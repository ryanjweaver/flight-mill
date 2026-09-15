"""SOFTWARE ONLY: deterministic in-memory STOP boundary and byte-level audit.

Reuses Endpoint and the real SerialTransport worker. No OS port, service-state
mutation, fabricated terminal count, or wall-clock delay controls event capture.
"""
from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import threading
import time
from pathlib import Path

from flightmill.constants import RAW_CSV_HEADER
from flightmill.protocol.conformance import check_transcript
from flightmill.protocol.models import parse_line
from test_serial_acquisition import Clock, Endpoint, make_service


class TailEndpoint(Endpoint):
    def __init__(self, clock, *, times=(), delivery='separate'):
        super().__init__(clock)
        self.times = times
        self.delivery = delivery
        self.progress = threading.Condition()
        self.generation = 0
        self.collect = None
        self.before_stop = lambda: None
        self.stop_entered = threading.Event()
        self.release_stop = threading.Event()
        self.release_stop.set()

    def feed(self, *frames):
        if self.collect is not None:
            self.collect.extend(frames)
            return
        super().feed(*frames)
        with self.progress:
            self.progress.notify_all()

    def read(self, size):
        # Entering read proves the worker enqueued its preceding read result.
        with self.progress:
            self.generation += 1
            self.progress.notify_all()
            self.progress.wait_for(lambda: self.in_waiting or self.closed or self.read_error,
                                   timeout=self.timeout)
        if self.read_error:
            raise self.read_error
        with self.lock:
            if self.frames:
                raw = self.frames.popleft()
                if len(raw) > size:
                    self.frames.appendleft(raw[size:])
                return raw[:size]
        return b''

    def cancel_read(self):
        self.closed = True
        self.release_stop.set()
        with self.progress:
            self.progress.notify_all()

    def write(self, raw):
        if parse_line(raw).type != 'stop':
            return super().write(raw)
        self.stop_entered.set()
        assert self.release_stop.wait(2), 'STOP endpoint barrier was not released'
        if self.delivery != 'separate':
            self.collect = []
        # Device is still RECORDING here. Original Endpoint.write processes STOP
        # only AFTER these real model events have been accepted and emitted.
        for event_us in self.times:
            self.emit(self.device.emit_event(event_us))
        self.before_stop()
        count = super().write(raw)
        if self.collect is not None:
            batch = b''.join(self.collect)
            self.collect = None
            if self.delivery == 'fragmented':
                self.feed(batch[:17], batch[17:101], batch[101:])
            else:
                self.feed(batch)
        return count


def pump(service, endpoint, condition, timeout=2):
    deadline = time.monotonic() + timeout
    while True:
        with endpoint.progress:
            generation = endpoint.generation
        service.tick()
        state = service.snapshot()
        if condition(state):
            return state
        remaining = deadline - time.monotonic()
        assert remaining > 0, state
        with endpoint.progress:
            endpoint.progress.wait_for(lambda: endpoint.generation != generation,
                                       timeout=remaining)


def connect(directory, *, times=(), delivery='separate'):
    clock = Clock()
    endpoint = TailEndpoint(clock, times=times, delivery=delivery)
    service = make_service(directory, endpoint, clock)
    service.action('select_source', {'source': 'serial'})
    service.action('connect')
    pump(service, endpoint, lambda s: s['ready'])
    return service, endpoint, clock


def start(service, endpoint, **setup):
    service.action('arm', {'species_code': 'SOFTWARE', 'notes': 'SOFTWARE ONLY', **setup})
    pump(service, endpoint, lambda s: s['state'] == 'ARMED' and not s['pending'])
    service.action('start')
    pump(service, endpoint, lambda s: s['state'] == 'RECORDING')


def persisted_rows(directory):
    directory = Path(directory)
    journal_path, = directory.glob('*_journal.json')
    base = json.loads(journal_path.read_bytes())['base']
    raw = directory / (base + '.partial.csv')
    with raw.open(newline='') as stream:
        reader = csv.DictReader(stream)
        assert reader.fieldnames == list(RAW_CSV_HEADER)
        return list(reader)


def audit(directory):
    """Independently parse saved bytes (not service/discovery counters)."""
    directory = Path(directory)
    journal_path = next(directory.glob('*_journal.json'))
    journal = json.loads(journal_path.read_bytes())
    base = journal['base']
    def artifact(final, partial):
        path = directory / (base + partial)
        return path if path.exists() else directory / (base + final)
    raw_path = artifact('.csv', '.partial.csv')
    raw = raw_path.read_bytes()
    reader = csv.DictReader(io.StringIO(raw.decode()))
    rows = list(reader)
    assert reader.fieldnames == list(RAW_CSV_HEADER)
    with artifact('_metadata.csv', '_metadata.partial.csv').open(newline='') as stream:
        fields = list(csv.reader(stream))
    assert fields.pop(0) == ['field', 'value']
    metadata = dict(fields)
    digest = hashlib.sha256(raw).hexdigest()
    assert metadata['raw_csv_sha256'] == journal['raw_sha256'] == digest
    assert journal['row_count'] == journal['durable_row_count'] == len(rows)
    assert journal['metadata']['raw_csv_sha256'] == digest
    entries = [json.loads(line) for line in
               artifact('_protocol.jsonl', '_protocol.partial.jsonl').read_bytes().splitlines()]
    buffers = {'in': b'', 'out': b''}
    frames = []
    for entry in entries:
        if entry['kind'] != 'protocol':
            continue
        direction = entry['direction']
        buffers[direction] += base64.b64decode(entry['raw_base64'])
        while b'\n' in buffers[direction]:
            frame, buffers[direction] = buffers[direction].split(b'\n', 1)
            frames.append(frame + b'\n')
    report = check_transcript(frames)
    # Negative controls deliberately retain malformed bytes; the conformance
    # report separates those issues from successfully parsed device evidence.
    messages = report.parsed_messages
    summaries = [m for m in messages if m.type == 'trial_stopped']
    return {'scope': 'SOFTWARE ONLY', 'rows': rows, 'metadata': metadata,
            'journal': journal, 'sha256': digest, 'frames': frames,
            'messages': messages, 'summaries': summaries, 'entries': entries,
            'conformance_passed': report.passed,
            'conformance_issues': [issue.code for issue in report.issues]}


def assert_clean(result, count):
    assert len(result['rows']) == count
    assert result['journal']['status'] == 'complete'
    assert result['metadata']['incomplete'] == 'false'
    assert int(result['metadata']['accepted_event_count']) == count
    assert result['summaries'][-1].accepted_event_count == count
    assert result['conformance_passed'], result['conformance_issues']
    if count:
        first = result['rows'][0]
        assert first['event_n'] == '1' and first['speed_m_s'] == ''
        assert first['dt_ms'] == '0.000' and first['dt_s'] == '0.000000'


def run_case(directory, mode, path):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    (directory / 'SOFTWARE_ONLY.txt').write_text('SOFTWARE ONLY: in-memory endpoint; no physical trial.\n')
    interval = 50000 if mode == 'legacy_50ms' else 150000
    service, endpoint, clock = connect(directory, times=() if path == 'control' else (interval,))
    try:
        start(service, endpoint, interval_mode=mode,
              planned_duration_s=1 if path == 'planned' else None)
        endpoint.emit(endpoint.device.emit_event(0))
        pump(service, endpoint, lambda s: len(persisted_rows(directory)) == 1)
        if path == 'control':
            endpoint.emit(endpoint.device.emit_event(interval))
            pump(service, endpoint, lambda s: len(persisted_rows(directory)) == 2)
        clock.advance(1)
        if path == 'close':
            service.close()
        elif path == 'planned':
            service.tick()
        else:
            service.action('stop')
        state = pump(service, endpoint, lambda s: s['save_status'] in {'saved', 'incomplete'})
        result = audit(directory)
        result['warnings'] = [w['code'] for w in state['warnings']]
        result['stop_commands'] = len([c for c in endpoint.writes if c.type == 'stop'])
        return result
    finally:
        service.close()
