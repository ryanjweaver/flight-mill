"""Headless diagnostic/fallback controls using the GUI's acquisition owner."""
from __future__ import annotations

import argparse
import json
import threading
import time
from pathlib import Path

from flightmill.acquisition.serial_transport import discover_ports
from flightmill.acquisition.service import AcquisitionService
from flightmill.acquisition.transport import FAULTS, PROFILES


def wait_for(service, predicate, timeout=6, *, clock=time.monotonic, sleep=time.sleep):
    deadline = clock() + timeout
    while True:
        service.tick()
        state = service.snapshot()
        if predicate(state):
            return state
        if clock() >= deadline or (not state["connected"] and state["connection_state"] != "opening"):
            raise RuntimeError("Device confirmation unavailable: " + json.dumps(state["warnings"]))
        sleep(0.02)


def record(service, *, setup, physical_start=False, start_timeout=120,
           fault=None, clock=time.monotonic, sleep=time.sleep):
    """CLI execution path with injectable clocks for deterministic verification."""
    wait_for(service, lambda s: s["ready"], clock=clock, sleep=sleep)
    service.action("arm", setup)
    state = wait_for(service, lambda s: s["state"] in {"ARMED", "RECORDING"} and not s["pending"], clock=clock, sleep=sleep)
    if not physical_start and state["state"] == "ARMED":
        service.action("start")
    wait_for(service, lambda s: s["state"] == "RECORDING", start_timeout, clock=clock, sleep=sleep)
    injected = False
    while True:
        service.tick()
        result = service.snapshot()
        if result["save_status"] in {"saved", "incomplete"}:
            return result
        if fault and not injected and result["metrics"]["elapsed_s"] >= 1:
            service.action("fault", {"kind": fault})
            injected = True
        sleep(0.02)


def interactive(service):
    stop = threading.Event()
    def ticker():
        while not stop.wait(0.02):
            service.tick()
    worker = threading.Thread(target=ticker, name="flightmill-cli-ticker", daemon=True)
    worker.start()
    print('Commands: status, self_test, arm [setup JSON], start, stop, disarm, disconnect, connect, quit')
    print('The acquisition worker continues while this prompt waits for input.')
    try:
        while True:
            try:
                line = input('flightmill> ').strip()
            except EOFError:
                break
            if line == 'quit':
                break
            if not line:
                continue
            name, _, body = line.partition(' ')
            try:
                result = service.snapshot() if name == 'status' else service.action(name, json.loads(body) if body else {})
                print(json.dumps(result, indent=2))
            except (ValueError, RuntimeError, OSError) as exc:
                print(f'Action rejected: {exc}')
    finally:
        stop.set()
        worker.join(timeout=1)


def main(argv=None, *, service_factory=AcquisitionService) -> int:
    parser = argparse.ArgumentParser(description="FlightMill shared acquisition CLI")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--source", choices=["simulation", "serial"], default="simulation")
    parser.add_argument("--port", help="Explicit local OS port; otherwise one USB candidate is required")
    parser.add_argument("--list-ports", action="store_true", help="Enumerate only; opens no ports")
    parser.add_argument("--interactive", action="store_true")
    parser.add_argument("--connect-only", action="store_true", help="Handshake and sensor status, no ARM")
    parser.add_argument("--physical-start", action="store_true")
    parser.add_argument("--start-timeout", type=float, default=120)
    parser.add_argument("--profile", choices=PROFILES, default="steady")
    parser.add_argument("--fault", choices=FAULTS, help="Inject a simulation fault after one second")
    parser.add_argument("--duration", type=float, default=10)
    parser.add_argument("--rate", type=float, default=1)
    parser.add_argument("--attempt", type=int, default=1)
    parser.add_argument("--radius-cm", type=float, default=10)
    parser.add_argument("--interval-mode", choices=["default_150ms", "legacy_50ms"], default="default_150ms")
    args = parser.parse_args(argv)
    if args.list_ports:
        print(json.dumps(discover_ports(), indent=2))
        return 0
    if args.output_dir is None:
        parser.error("--output-dir is required for acquisition")
    if args.source == "serial" and args.fault:
        parser.error("--fault is simulation-only")
    service = service_factory(args.output_dir)
    try:
        service.action("select_source", {"source": args.source, "port": args.port})
        service.action("connect")
        wait_for(service, lambda s: s["ready"])
        setup = {"species_code": "SIM" if args.source == "simulation" else "TEST",
                 "trial_type": "diagnostic", "trial_number": 1, "well_id": "A1",
                 "attempt": args.attempt, "arm_radius_cm": args.radius_cm,
                 "interval_mode": args.interval_mode, "planned_duration_s": args.duration}
        service.action("validate", setup)
        if args.source == "simulation":
            service.action("configure_simulation", {"profile": args.profile, "seed": 42, "rate_hz": args.rate})
        if args.interactive:
            interactive(service)
            return 0
        if args.connect_only:
            print(json.dumps(service.snapshot(), indent=2))
            return 0
        result = record(service, setup=setup, physical_start=args.physical_start,
                        start_timeout=args.start_timeout, fault=args.fault)
        print(json.dumps({key: result[key] for key in
              ("state", "source", "configuration", "metrics", "trial", "warnings")}, indent=2))
        if result["state"] == "COMPLETE":
            service.action("disarm")
            wait_for(service, lambda s: s["state"] == "IDLE" and not s["pending"])
        return int(result["trial"]["incomplete"])
    except KeyboardInterrupt:
        print("Interrupted; requesting stop. Unconfirmed data remain incomplete.")
        return 130
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"Acquisition could not complete: {exc}")
        return 1
    finally:
        service.close()
