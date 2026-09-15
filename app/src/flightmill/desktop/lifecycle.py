"""Native close coordination; the existing app ticker remains the acquisition owner."""
from __future__ import annotations

import logging
import threading
import time

LOGGER = logging.getLogger(__name__)


class DesktopLifecycle:
    """Cancel the immediate native close and finish once, off the window thread.

    pywebview 6.0 closing handlers run synchronously on WinForms. A worker keeps
    that callback short, including during confirmation and terminal persistence.
    The eight-second UI watchdog does not alter any acquisition deadline.
    """

    def __init__(self, service, window, *, clock=time.monotonic, wait=time.sleep):
        self.service, self.window = service, window
        self.clock, self.wait = clock, wait
        self._lock = threading.Lock()
        self._closing = False
        self._allow_close = False
        self.worker = None

    def closing(self):
        with self._lock:
            if self._allow_close:
                return True
            if self._closing:
                return False
            self._closing = True
        self.worker = threading.Thread(target=self._finish_close, name='flightmill-close', daemon=True)
        self.worker.start()
        return False

    def _finish_close(self):
        consent = False
        deadline = self.clock() + 8
        try:
            while True:
                state = self.service.snapshot()
                phase = state['state']
                if phase == 'RECORDING' and not consent:
                    if not self.window.create_confirmation_dialog(
                        'Recording in progress',
                        'Stop and save this trial, then close? Choose Cancel to keep recording.',
                    ):
                        return
                    consent = True
                    deadline = self.clock() + 8
                    continue  # State may have changed while the dialog was open.
                if not state.get('pending'):
                    try:
                        if phase == 'RECORDING':
                            self.service.action('stop', {'stop_reason': 'application_close'})
                            continue
                        if phase == 'ARMED':
                            self.service.action('disarm', {})
                            continue
                    except ValueError:
                        # An auto/button stop may have won the service lock. Only
                        # accept that race when the authoritative phase changed.
                        if self.service.snapshot()['state'] == phase:
                            raise
                        continue
                waiting = (phase in {'ARMED', 'RECORDING', 'STOPPING'} or state.get('pending')
                           or state.get('save_status') == 'saving'
                           or (state.get('connected') and state.get('device_stop_confirmed') is False))
                if not waiting:
                    trial = state.get('trial') or {}
                    if state.get('save_status') == 'incomplete' or trial.get('incomplete'):
                        confirmed = state.get('device_stop_confirmed') is True
                        if not self.window.create_confirmation_dialog(
                            'Incomplete trial — review required',
                            'The trial was not successfully saved as complete. Retained files remain '
                            'available in Trial archive. Device STOP is '
                            + ('confirmed.' if confirmed else 'unconfirmed.')
                            + ' Choose OK to close or Cancel to review the evidence.',
                        ):
                            return
                    with self._lock:
                        self._allow_close = True
                    self.window.destroy()
                    return
                if self.clock() >= deadline:
                    self.window.create_confirmation_dialog(
                        'Close could not finish',
                        'Stop or save is still unconfirmed. The application will stay open. '
                        'Review the live state and Diagnostics; retained files have not been discarded.',
                    )
                    return
                self.wait(.05)
        except Exception:
            LOGGER.exception('Native close could not finish; keeping the service alive')
            self.window.create_confirmation_dialog(
                'Close needs attention',
                'The application will stay open. Review Diagnostics and the retained trial files. '
                'A complete save has not been confirmed.',
            )
        finally:
            with self._lock:
                self._closing = False
