"""Checkpoint 03 configuration and scenario coverage through public service/CLI paths."""
import csv
import json

import pytest
from fastapi.testclient import TestClient

from flightmill.acquisition.service import AcquisitionService
from flightmill.api.app import create_app
from flightmill.cli.main import main, record
from flightmill.storage.trial_writer import discover_trials
from test_serial_acquisition import Clock
from simulator.simulated_device import SCENARIO_NAMES


@pytest.mark.parametrize('scenario', SCENARIO_NAMES)
def test_all_frozen_scenario_behaviors_through_shared_acquisition(tmp_path, scenario):
    clock = Clock()
    service = AcquisitionService(tmp_path, clock=clock)
    service.action('select_source', {'source': 'simulation'})
    try:
        service.action('connect')
        service.action('configure_simulation', {'profile':'manual'})
        service.action('arm', {'interval_mode':'legacy_50ms' if scenario=='chatter_below_50ms' else 'default_150ms'})
        if scenario in {'physical_button_start','physical_button_stop'}:
            service.action('button', {'held_ms':100})
        else:
            service.action('start')
        if scenario != 'zero_event':
            service.action('pulse')
        if scenario in {'normal','known_timing','physical_button_start','physical_button_stop'}:
            clock.advance(1)
            service.action('pulse')
        elif scenario == 'chatter_below_50ms':
            service.transport.pulse(event_us=49999)
            service.transport.pulse(event_us=50000)
            clock.advance(.05)
            service.tick()
        elif scenario == 'zero_event':
            clock.advance(60)
            service.tick()
        else:
            fault={'reported_drops':'drops','malformed_line':'malformed','duplicate_event':'duplicate',
                   'skipped_message_sequence':'sequence_gap','device_reset_during_trial':'reset',
                   'disconnect_reconnect':'disconnect'}[scenario]
            service.action('fault', {'kind':fault})
        if service.snapshot()['state'] == 'RECORDING':
            service.action('button', {'held_ms':1500}) if scenario=='physical_button_stop' else service.action('stop')
        trial = discover_trials(tmp_path)[0]
        assert trial['incomplete'] == (scenario in {'reported_drops','malformed_line','device_reset_during_trial','disconnect_reconnect'})
        if scenario=='disconnect_reconnect':
            assert service.action('connect')['state']=='IDLE'
            assert service._writer is None
    finally:
        service.close()


@pytest.mark.parametrize('mode,interval', [('default_150ms',150000),('legacy_50ms',50000)])
@pytest.mark.parametrize('delta', [-1,0,1])
def test_exact_interval_rule_through_service(tmp_path, mode, interval, delta):
    clock = Clock()
    service = AcquisitionService(tmp_path, clock=clock)
    service.action('select_source', {'source': 'simulation'})
    try:
        service.action('connect')
        service.action('configure_simulation', {'profile':'manual'})
        service.action('arm', {'interval_mode':mode})
        service.action('start')
        service.transport.pulse(event_us=0)
        service.transport.pulse(event_us=interval+delta)
        service.tick()
        assert service.snapshot()['metrics']['row_count'] == (1 if delta < 0 else 2)
        if delta < 0:
            # The rejected edge must not extend the accepted-event baseline.
            service.transport.pulse(event_us=interval)
        clock.advance(1)
        result = service.action('stop')
        assert result['save_status'] == 'saved'
        rows = list(csv.DictReader((tmp_path/result['filename']).open()))
        assert rows[0]['dt_s'] == '0.000000'
        assert rows[0]['speed_m_s'] == ''
        assert len(rows) == 2
    finally:
        service.close()


def test_stale_preferences_require_explicit_legacy_and_api_echo(tmp_path):
    clock = Clock()
    service = AcquisitionService(tmp_path, clock=clock)
    service.action('select_source', {'source': 'simulation'})
    with TestClient(create_app(tmp_path, service=service), base_url='http://127.0.0.1') as client:
        token = client.get('/api/session').json()['token']
        def action(name, **payload):
            return client.post('/api/action', json={'action':name, **payload}, headers={'X-Flightmill-Token':token})
        action('connect')
        assert action('arm', min_event_interval_us=50000).status_code == 409
        assert not list(tmp_path.glob('*_journal.json'))
        for attempt, mode, interval in [(1,'default_150ms',150000),(2,'legacy_50ms',50000)]:
            result = action('arm', attempt=attempt, interval_mode=mode).json()
            assert result['configuration']['confirmed_interval_us'] == interval
            assert action('validate', interval_mode='default_150ms').status_code == 409
            action('start')
            action('stop')
            action('disarm')
        action('disconnect')
        result = action('connect').json()
        assert result['configuration']['confirmed_interval_us'] is None
        assert result['configuration']['requested_interval_us'] == 50000
        assert action('arm', attempt=3, interval_mode='default_150ms').json()['configuration']['confirmed_interval_us'] == 150000


@pytest.mark.parametrize('profile', ['steady','ramp','flight_rest','zero','stress','manual'])
def test_cli_record_profiles(tmp_path, profile):
    clock = Clock()
    service = AcquisitionService(tmp_path, clock=clock)
    service.action('select_source', {'source': 'simulation'})
    try:
        service.action('connect')
        service.action('configure_simulation', {'profile':profile})
        result = record(service, setup={'planned_duration_s':3}, clock=clock, sleep=clock.advance)
        assert result['save_status'] == 'saved'
    finally:
        service.close()


@pytest.mark.parametrize('fault,incomplete', [
    ('duplicate',False), ('sequence_gap',False), ('fragmented',False),
    ('malformed',True), ('drops',True), ('disconnect',True), ('reset',True),
    ('heartbeat_loss',True), ('missing_ack',False), ('delayed_ack',False),
    ('missing_summary',True), ('mismatched_summary',True), ('wrong_session',False), ('event_gap',True),
])
def test_cli_record_faults(tmp_path, fault, incomplete):
    clock = Clock()
    service = AcquisitionService(tmp_path, clock=clock)
    service.action('select_source', {'source': 'simulation'})
    try:
        service.action('connect')
        result = record(service, setup={'planned_duration_s':6}, fault=fault, clock=clock, sleep=clock.advance)
        assert result['trial']['incomplete'] is incomplete
        assert discover_trials(tmp_path)[0]['incomplete'] is incomplete
    finally:
        service.close()


def test_explicit_legacy_stress_preserves_twenty_accepted_per_second(tmp_path):
    clock = Clock()
    service = AcquisitionService(tmp_path, clock=clock)
    service.action('select_source', {'source': 'simulation'})
    try:
        service.action('connect')
        service.action('configure_simulation', {'profile':'stress'})
        result = record(service, setup={'planned_duration_s':61, 'interval_mode':'legacy_50ms'},
                        clock=clock, sleep=lambda _:clock.advance(1))
        assert result['metrics']['row_count'] == 1220
        assert result['save_status'] == 'saved'
    finally:
        service.close()


def test_cli_entry_point_zero_and_no_overwrite(tmp_path, capsys):
    args = ['--output-dir',str(tmp_path),'--duration','0.05','--profile','zero']
    assert main(args) == 0
    before = {p.name:p.read_bytes() for p in tmp_path.iterdir()}
    assert main(args) == 1
    assert before == {p.name:p.read_bytes() for p in tmp_path.iterdir()}
    assert 'already exist' in capsys.readouterr().out


def test_missing_simulation_manifest_never_becomes_hardware(tmp_path):
    service = AcquisitionService(tmp_path)
    service.action('select_source', {'source': 'simulation'})
    try:
        service.action('connect')
        service.action('arm')
        service.action('start')
        service.action('stop')
    finally:
        service.close()
    next(tmp_path.glob('*_simulation.json')).unlink()
    trial = discover_trials(tmp_path)[0]
    assert trial['incomplete'] and trial['acquisition_mode'] == 'simulation'
    journal = next(tmp_path.glob('*_journal.json'))
    value = json.loads(journal.read_text())
    value.pop('acquisition_mode')
    journal.write_text(json.dumps(value))
    trial = discover_trials(tmp_path)[0]
    assert trial['incomplete'] and trial['acquisition_mode'] == 'unknown'
