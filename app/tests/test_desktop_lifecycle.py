"""SOFTWARE ONLY callback tests; native dialogs require separate acceptance."""
import sys
import logging
import threading
import time
from types import SimpleNamespace

import pytest

from flightmill.desktop import launcher
from flightmill.desktop.lifecycle import DesktopLifecycle
from flightmill.storage.trial_writer import TrialWriter
from stop_tail_support import assert_clean, audit, connect, pump, start


@pytest.fixture(autouse=True)
def release_test_logging_handlers():
    root = logging.getLogger()
    before = list(root.handlers)
    yield
    for handler in list(root.handlers):
        if handler not in before:
            root.removeHandler(handler)
            handler.close()


class Event:
    def __init__(self):
        self.callbacks = []

    def __iadd__(self, callback):
        self.callbacks.append(callback)
        return self

    def fire(self):
        return all(callback() is not False for callback in self.callbacks)


class Window:
    def __init__(self, answers=(True,)):
        self.events = SimpleNamespace(closing=Event())
        self.answers = iter(answers)
        self.dialogs = []
        self.destroyed = threading.Event()
        self.titles = []

    def create_confirmation_dialog(self, title, message):
        self.dialogs.append((title, message))
        return next(self.answers, False)

    def destroy(self):
        if self.events.closing.fire():
            self.destroyed.set()

    def set_title(self, title):
        self.titles.append(title)


class Service:
    def __init__(self, directory):
        self.directory = directory
        self.state = 'RECORDING'
        self.saved = False
        self.actions = []

    def snapshot(self):
        return {'state': self.state, 'pending': {'action': 'stop'} if self.state == 'STOPPING'
                else None, 'save_status': 'saved' if self.saved else 'recording',
                'trial': {'id': 'SOFTWARE', 'incomplete': not self.saved},
                'connected': True, 'output_dir': str(self.directory),
                'device_stop_confirmed': True if self.saved else None}

    def action(self, name, payload=None):
        self.actions.append((name, payload))
        self.state = 'STOPPING'


def test_launcher_async_stop_closes_automatically_and_enables_native_downloads(tmp_path, monkeypatch):
    service = Service(tmp_path)
    window = Window()
    observation = {}

    class Server:
        started = False
        should_exit = False

        def __init__(self, config):
            pass

        def run(self, **kwargs):
            self.started = True
            while not self.should_exit:
                time.sleep(.005)

    native = SimpleNamespace(settings={'ALLOW_DOWNLOADS': False}, FileDialog=SimpleNamespace(FOLDER=20))

    def create_window(title, url, **kwargs):
        observation['title'] = title
        observation['bridge'] = kwargs['js_api']
        return window

    def start(**kwargs):
        observation['first_close'] = window.events.closing.fire()
        deadline = time.monotonic() + 1
        while not service.actions and time.monotonic() < deadline:
            time.sleep(.005)
        observation['repeat_close'] = window.events.closing.fire()
        service.state, service.saved = 'COMPLETE', True
        observation['automatically_closed'] = window.destroyed.wait(1)

    native.create_window, native.start = create_window, start
    monkeypatch.setitem(sys.modules, 'webview', native)
    monkeypatch.setattr(launcher.uvicorn, 'Server', Server)
    monkeypatch.setattr(launcher, 'create_app', lambda directory:
                        SimpleNamespace(state=SimpleNamespace(acquisition=service)))
    monkeypatch.setattr(sys, 'argv', ['flightmill', '--output-dir', str(tmp_path), '--port', '18997'])
    launcher.main()
    assert observation['first_close'] is False
    assert observation['repeat_close'] is False
    assert service.actions == [('stop', {'stop_reason': 'application_close'})]
    assert observation['automatically_closed'], 'A single accepted close must close after saved STOP'
    assert native.settings['ALLOW_DOWNLOADS'] is True
    assert 'Simulation' not in observation['title']
    bridge = observation['bridge']
    assert {name for name in dir(bridge) if not name.startswith('_')} == {
        'choose_output_dir', 'open_output_folder'}
    selected = tmp_path / 'folder with spaces'
    window.create_file_dialog = lambda kind: (str(selected),)
    assert bridge.choose_output_dir() == str(selected)
    window.create_file_dialog = lambda kind: None
    assert bridge.choose_output_dir() is None
    assert service.directory == tmp_path  # Picking is a draft, never a validation bypass.
    opened = []
    monkeypatch.setattr(launcher.os, 'startfile', lambda path: opened.append(path), raising=False)
    assert bridge.open_output_folder() is True
    assert opened == [tmp_path]
    service.directory = selected  # A missing folder must produce a useful error.
    with pytest.raises(ValueError, match='unavailable'):
        bridge.open_output_folder()


def test_native_downloads_are_enabled_without_replacing_registered_routes(tmp_path, monkeypatch):
    # Exercise the actual launcher setup, including the pinned settings object.
    service = Service(tmp_path)
    service.state, service.saved = 'COMPLETE', True
    window = Window()
    native = SimpleNamespace(settings={'ALLOW_DOWNLOADS': False})

    class Server:
        started = False
        should_exit = False

        def __init__(self, config):
            pass

        def run(self, **kwargs):
            self.started = True
            while not self.should_exit:
                time.sleep(.005)

    native.create_window = lambda *a, **kw: window
    native.start = lambda **kw: None
    monkeypatch.setitem(sys.modules, 'webview', native)
    monkeypatch.setattr(launcher.uvicorn, 'Server', Server)
    monkeypatch.setattr(launcher, 'create_app', lambda directory:
                        SimpleNamespace(state=SimpleNamespace(acquisition=service)))
    monkeypatch.setattr(sys, 'argv', ['flightmill', '--output-dir', str(tmp_path), '--port', '18998'])
    launcher.main()
    assert native.settings['ALLOW_DOWNLOADS'] is True


def wait_worker(controller):
    controller.worker.join(timeout=3)
    assert not controller.worker.is_alive()


def test_cancel_keeps_recording_and_repeated_close_does_not_duplicate_dialog(tmp_path):
    service = Service(tmp_path)
    entered, release = threading.Event(), threading.Event()
    window = Window()

    def confirm(*args):
        entered.set()
        assert release.wait(2)
        return False

    window.create_confirmation_dialog = confirm
    controller = DesktopLifecycle(service, window)
    assert controller.closing() is False
    assert entered.wait(1)
    assert controller.closing() is False
    assert service.state == 'RECORDING' and not service.actions
    release.set()
    wait_worker(controller)
    assert not window.destroyed.is_set() and not service.actions


@pytest.mark.parametrize('mode', ['default_150ms', 'legacy_50ms'])
def test_close_drains_real_serial_worker_tail_with_one_stop(tmp_path, mode):
    interval = 150000 if mode == 'default_150ms' else 50000
    service, endpoint, clock = connect(tmp_path, times=(interval,))
    window = Window()
    controller = DesktopLifecycle(service, window)
    window.events.closing += controller.closing
    try:
        start(service, endpoint, interval_mode=mode)
        endpoint.emit(endpoint.device.emit_event(0))
        pump(service, endpoint, lambda s: s['metrics']['row_count'] == 1)
        clock.advance(1)
        assert controller.closing() is False
        pump(service, endpoint, lambda s: s['save_status'] == 'saved')
        wait_worker(controller)
        assert window.destroyed.is_set()
        result = audit(tmp_path)
        assert_clean(result, 2)
        assert result['metadata']['stop_reason'] == 'application_close'
        assert sum(c.type == 'stop' for c in endpoint.writes) == 1
    finally:
        service.close()


def test_close_during_stopping_waits_and_sends_no_second_stop(tmp_path):
    service, endpoint, clock = connect(tmp_path, times=(150000,))
    window = Window()
    controller = DesktopLifecycle(service, window)
    window.events.closing += controller.closing
    try:
        start(service, endpoint)
        endpoint.release_stop.clear()
        clock.advance(1)
        service.action('stop')
        assert endpoint.stop_entered.wait(1)
        controller.closing()
        controller.closing()
        assert not window.dialogs
        endpoint.release_stop.set()
        pump(service, endpoint, lambda s: s['save_status'] == 'saved')
        wait_worker(controller)
        assert window.destroyed.is_set()
        assert sum(c.type == 'stop' for c in endpoint.writes) == 1
    finally:
        endpoint.release_stop.set()
        service.close()


@pytest.mark.parametrize('fault', ['missing_summary', 'storage_failure'])
def test_close_reports_incomplete_and_allows_review(tmp_path, monkeypatch, fault):
    service, endpoint, clock = connect(tmp_path)
    window = Window((True, False))
    controller = DesktopLifecycle(service, window)
    window.events.closing += controller.closing
    try:
        start(service, endpoint)
        endpoint.emit(endpoint.device.emit_event(0))
        pump(service, endpoint, lambda s: s['metrics']['row_count'] == 1)
        if fault == 'missing_summary':
            endpoint.response_filter = lambda messages: [m for m in messages if m.type != 'trial_stopped']
        else:
            monkeypatch.setattr(TrialWriter, 'finalize', lambda *args:
                                (_ for _ in ()).throw(OSError('SOFTWARE ONLY storage failure')))
        clock.advance(1)
        controller.closing()
        pump(service, endpoint, lambda s: s['pending'] is not None or s['save_status'] == 'incomplete')
        if fault == 'missing_summary':
            pump(service, endpoint, lambda s: bool(endpoint.writes[-1].type == 'stop'))
            clock.advance(3.1)
        pump(service, endpoint, lambda s: s['save_status'] == 'incomplete')
        wait_worker(controller)
        assert not window.destroyed.is_set()
        assert 'Incomplete trial' in window.dialogs[-1][0]
        assert 'not successfully saved' in window.dialogs[-1][1]
        assert 'unconfirmed' in window.dialogs[-1][1] if fault == 'missing_summary' else True
        assert sum(c.type == 'stop' for c in endpoint.writes) == 1
        assert audit(tmp_path)['metadata']['incomplete'] == 'true'
    finally:
        service.close()


def test_armed_close_disarms_and_preserves_unstarted_trial(tmp_path):
    service, endpoint, _ = connect(tmp_path)
    window = Window((True,))
    controller = DesktopLifecycle(service, window)
    window.events.closing += controller.closing
    try:
        service.action('arm', {'species_code': 'SOFTWARE', 'notes': 'SOFTWARE ONLY'})
        pump(service, endpoint, lambda s: s['state'] == 'ARMED')
        controller.closing()
        pump(service, endpoint, lambda s: s['state'] == 'IDLE' and not s['pending'])
        wait_worker(controller)
        assert window.destroyed.is_set()
        assert sum(c.type == 'disarm' for c in endpoint.writes) == 1
        assert not any(c.type in {'start', 'stop'} for c in endpoint.writes)
        assert audit(tmp_path)['metadata']['stop_reason'] == 'disarmed_before_start'
    finally:
        service.close()


@pytest.mark.parametrize('phase', ['IDLE', 'COMPLETE', 'DISCONNECTED'])
def test_idle_complete_close_needs_no_acquisition_action(tmp_path, phase):
    service = Service(tmp_path)
    service.state, service.saved = phase, True
    window = Window()
    controller = DesktopLifecycle(service, window)
    window.events.closing += controller.closing
    controller.closing()
    wait_worker(controller)
    assert window.destroyed.is_set() and not service.actions and not window.dialogs


def test_close_watchdog_stays_open_without_retry_or_forced_finalization(tmp_path):
    service = Service(tmp_path)
    service.state = 'STOPPING'
    window = Window((False,))
    now = [0.0]
    def advance(seconds):
        now[0] += seconds
    controller = DesktopLifecycle(service, window, clock=lambda: now[0], wait=advance)
    window.events.closing += controller.closing
    controller.closing()
    wait_worker(controller)
    assert not window.destroyed.is_set() and not service.actions
    assert window.dialogs[0][0] == 'Close could not finish'
