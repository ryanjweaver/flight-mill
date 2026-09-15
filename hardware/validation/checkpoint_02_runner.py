"""Real-device conformance and operator-assisted Checkpoint 02 acceptance.

This is validation tooling, not the Flight Mill host acquisition application.
It preserves every serial frame as base64 plus decoded text, writes the exact
bidirectional protocol transcript, and never treats an unanswered operator
observation as a pass.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from queue import Empty, Queue
from typing import Any, Callable
from uuid import UUID, uuid4

import serial
from serial import SerialException
from serial.tools import list_ports


ROOT = Path(__file__).parents[2]
APP_SOURCE = ROOT / "app" / "src"
if str(APP_SOURCE) not in sys.path:
    sys.path.insert(0, str(APP_SOURCE))

from flightmill.protocol.conformance import check_transcript  # noqa: E402
from flightmill.constants import DEFAULT_MIN_EVENT_INTERVAL_US  # noqa: E402
from flightmill.protocol.models import (  # noqa: E402
    AckMessage,
    DeviceMessage,
    ErrorMessage,
    EventMessage,
    HeartbeatMessage,
    HelloMessage,
    Message,
    ProtocolError,
    StatusMessage,
    TrialArmedMessage,
    TrialStartedMessage,
    TrialStoppedMessage,
    parse_line,
)


BAUD_RATE = 115_200
MIN_EVENT_INTERVAL_US = DEFAULT_MIN_EVENT_INTERVAL_US
EXPECTED_VID = 0x303A
EXPECTED_PID = 0x1001
ESPTOOL = ROOT / ".pio-core" / "packages" / "tool-esptoolpy" / "esptool.py"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def repository_commit() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def esptool_probe_command(port: str) -> list[str]:
    return [
        sys.executable,
        str(ESPTOOL),
        "--chip",
        "esp32s3",
        "--port",
        port,
        "--baud",
        str(BAUD_RATE),
        "flash_id",
    ]


@dataclass(slots=True)
class CheckResult:
    name: str
    status: str
    expected: str
    observed: str
    timestamp_utc: str = field(default_factory=utc_now)


class Evidence:
    def __init__(
        self, output_dir: Path, mode: str, requested_port: str, firmware_bin: Path | None
    ) -> None:
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=False)
        self.mode = mode
        self.requested_port = requested_port
        self.started_at_utc = utc_now()
        self.repository_commit = repository_commit()
        self.validator_source_sha256 = sha256_file(Path(__file__))
        self.ended_at_utc: str | None = None
        self.results: list[CheckResult] = []
        self.operator_observations: dict[str, str] = {}
        self.protocol_frames: list[bytes] = []
        self.malformed_after_handshake: list[str] = []
        self.sequence_issues: list[str] = []
        self._last_sequence: dict[tuple[str, str], int] = {}
        self.board: Board | None = None
        self.ports_used: list[str] = []
        self.device_id: str | None = None
        self.firmware_version: str | None = None
        self.boot_ids: list[str] = []
        self.session_ids: list[str] = []
        self.firmware_bin_name = firmware_bin.name if firmware_bin else None
        self.firmware_bin_sha256 = sha256_file(firmware_bin) if firmware_bin else None
        self._raw_stream = (self.output_dir / "raw_serial_capture.jsonl").open(
            "w", encoding="utf-8", newline="\n"
        )
        self._protocol_stream = (self.output_dir / "protocol_transcript.jsonl").open("wb")

    def use_port(self, port: str) -> None:
        if port not in self.ports_used:
            self.ports_used.append(port)

    def capture(self, direction: str, raw: bytes, port: str) -> None:
        record = {
            "timestamp_utc": utc_now(),
            "monotonic_ns": time.monotonic_ns(),
            "direction": direction,
            "port": port,
            "raw_base64": base64.b64encode(raw).decode("ascii"),
            "text_utf8": raw.decode("utf-8", errors="replace").rstrip("\r\n"),
        }
        self._raw_stream.write(json.dumps(record, separators=(",", ":")) + "\n")
        self._raw_stream.flush()

    def capture_protocol(self, raw: bytes) -> None:
        frame = raw if raw.endswith(b"\n") else raw + b"\n"
        self.protocol_frames.append(frame)
        self._protocol_stream.write(frame)
        self._protocol_stream.flush()

    def observe_message(self, message: Message) -> None:
        if isinstance(message, DeviceMessage):
            key = (message.device_id, message.boot_id)
            previous = self._last_sequence.get(key)
            if previous is not None and message.message_seq != previous + 1:
                self.sequence_issues.append(
                    f"{utc_now()}: boot_id={message.boot_id}; "
                    f"expected message_seq={previous + 1}; received={message.message_seq}"
                )
            self._last_sequence[key] = message.message_seq
            if self.device_id is None:
                self.device_id = message.device_id
                self.firmware_version = message.firmware_version
            if message.boot_id not in self.boot_ids:
                self.boot_ids.append(message.boot_id)
        session = getattr(message, "session_id", None)
        if session is not None and str(session) not in self.session_ids:
            self.session_ids.append(str(session))

    def add(self, name: str, passed: bool, expected: str, observed: str) -> None:
        self.results.append(CheckResult(name, "PASS" if passed else "FAIL", expected, observed))

    def ambiguous(self, name: str, expected: str, observed: str) -> None:
        self.results.append(CheckResult(name, "AMBIGUOUS", expected, observed))

    def record_operator(self, name: str, value: str) -> None:
        self.operator_observations[name] = value

    def record_probe(self, text: str) -> None:
        with (self.output_dir / "esptool_probe.log").open(
            "a", encoding="utf-8", newline="\n"
        ) as stream:
            stream.write(f"[{utc_now()}]\n{text.rstrip()}\n")

    @property
    def passed(self) -> bool:
        return (
            bool(self.results)
            and not self.malformed_after_handshake
            and not self.sequence_issues
            and all(result.status == "PASS" for result in self.results)
        )

    def finalize(self) -> None:
        self.add(
            "serial_capture_integrity",
            not self.malformed_after_handshake and not self.sequence_issues,
            "no malformed frames after handshake and contiguous per-boot message sequence",
            f"malformed={len(self.malformed_after_handshake)}; "
            f"sequence_issues={len(self.sequence_issues)}",
        )
        if not self._raw_stream.closed:
            self._raw_stream.close()
        if not self._protocol_stream.closed:
            self._protocol_stream.close()
        self.ended_at_utc = utc_now()
        manifest = {
            "schema_version": 1,
            "mode": self.mode,
            "started_at_utc": self.started_at_utc,
            "ended_at_utc": self.ended_at_utc,
            "repository_commit": self.repository_commit,
            "validator_source_sha256": self.validator_source_sha256,
            "requested_port": self.requested_port,
            "ports_used": self.ports_used,
            "baud_rate": BAUD_RATE,
            "device_id": self.device_id,
            "firmware_version": self.firmware_version,
            "protocol_version": 1,
            "boot_ids": self.boot_ids,
            "session_ids": self.session_ids,
            "firmware_bin_name": self.firmware_bin_name,
            "firmware_bin_sha256": self.firmware_bin_sha256,
            "operator_observations": self.operator_observations,
            "malformed_after_handshake": self.malformed_after_handshake,
            "sequence_issues": self.sequence_issues,
            "passed": self.passed,
            "results": [asdict(result) for result in self.results],
        }
        (self.output_dir / "results.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n"
        )
        lines = [
            f"# Checkpoint 02 {self.mode} report",
            "",
            f"- Started UTC: `{self.started_at_utc}`",
            f"- Ended UTC: `{self.ended_at_utc}`",
            f"- Repository commit: `{manifest['repository_commit']}`",
            f"- Port(s): `{', '.join(self.ports_used) or self.requested_port}`",
            f"- Device ID: `{self.device_id or 'not established'}`",
            f"- Firmware: `{self.firmware_version or 'not established'}`",
            f"- Boot IDs: `{', '.join(self.boot_ids) or 'none'}`",
            f"- Overall: `{'PASS' if self.passed else 'NOT PASS'}`",
            "",
            "| Check | Status | Expected | Observed |",
            "|---|---|---|---|",
        ]
        for result in self.results:
            expected = result.expected.replace("|", "\\|").replace("\n", " ")
            observed = result.observed.replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {result.name} | {result.status} | {expected} | {observed} |")
        lines.extend(
            [
                "",
                "This is prototype/development-board evidence only. It does not validate a fabricated carrier PCB.",
                "",
            ]
        )
        (self.output_dir / "report.md").write_text("\n".join(lines), encoding="utf-8", newline="\n")


class Board:
    def __init__(self, port: str, evidence: Evidence) -> None:
        self.port_name = port
        self.evidence = evidence
        self.serial: serial.Serial | None = None
        self.hello: HelloMessage | None = None
        self.device_id: str | None = None
        self.firmware_version: str | None = None
        self.allow_startup_noise = True
        self._pending_messages: deque[Message] = deque()
        self._serial_partial = bytearray()
        evidence.board = self

    def open(self) -> None:
        self._pending_messages.clear()
        self._serial_partial.clear()
        self.allow_startup_noise = True
        probe = subprocess.run(
            esptool_probe_command(self.port_name),
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        probe_text = probe.stdout + probe.stderr
        self.evidence.record_probe(probe_text)
        if probe.returncode != 0:
            raise RuntimeError(
                f"read-only esptool identity probe failed with exit code {probe.returncode}"
            )
        self.serial = serial.Serial()
        self.serial.port = self.port_name
        self.serial.baudrate = BAUD_RATE
        self.serial.timeout = 0.25
        self.serial.write_timeout = 2.0
        self.serial.dsrdtr = False
        self.serial.rtscts = False
        self.serial.dtr = False
        self.serial.rts = False
        self.serial.open()
        self.serial.dtr = True
        self.evidence.use_port(self.port_name)

    def close(self) -> None:
        if self.serial is not None:
            if self.serial.is_open:
                self.serial.close()
            self.serial = None

    def write_payload(self, payload: dict[str, Any], *, conforming: bool = True) -> bytes:
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"
        assert self.serial is not None
        self.evidence.capture("host_to_device", raw, self.port_name)
        if conforming:
            self.evidence.capture_protocol(raw)
        self.serial.write(raw)
        self.serial.flush()
        return raw

    def write_raw(self, raw: bytes) -> None:
        assert self.serial is not None
        self.evidence.capture("host_to_device", raw, self.port_name)
        self.serial.write(raw)
        self.serial.flush()

    def read_message(self, timeout_s: float, *, conforming: bool = True) -> Message | None:
        if self._pending_messages:
            self.allow_startup_noise = False
            return self._pending_messages.popleft()
        return self.read_live_message(timeout_s, conforming=conforming)

    def read_live_message(self, timeout_s: float, *, conforming: bool = True) -> Message | None:
        """Read new serial bytes, including while the operator has not answered."""
        assert self.serial is not None
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                raw = self.serial.readline()
            except SerialException:
                raise
            if not raw:
                continue
            self.evidence.capture("device_to_host", raw, self.port_name)
            self._serial_partial.extend(raw)
            if not self._serial_partial.endswith(b"\n"):
                continue
            raw = bytes(self._serial_partial)
            self._serial_partial.clear()
            try:
                message = parse_line(raw)
            except ProtocolError as exc:
                if self.allow_startup_noise:
                    continue
                detail = f"{utc_now()}: {exc}: {raw.decode('utf-8', errors='replace').rstrip()}"
                self.evidence.malformed_after_handshake.append(detail)
                continue
            self.allow_startup_noise = False
            self.evidence.observe_message(message)
            if conforming:
                self.evidence.capture_protocol(raw)
            if isinstance(message, HelloMessage):
                self.hello = message
                self.device_id = message.device_id
                self.firmware_version = message.firmware_version
            return message
        return None

    def wait_for(
        self,
        predicate: Callable[[Message], bool],
        timeout_s: float,
        description: str,
        *,
        conforming: bool = True,
    ) -> Message:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            message = self.read_message(
                max(0.05, deadline - time.monotonic()), conforming=conforming
            )
            if message is not None and predicate(message):
                return message
        raise TimeoutError(f"timed out waiting for {description}")

    def wait_for_hello(self, timeout_s: float = 12.0, *, conforming: bool = True) -> HelloMessage:
        self.allow_startup_noise = True
        message = self.wait_for(
            lambda item: isinstance(item, HelloMessage),
            timeout_s,
            "hello",
            conforming=conforming,
        )
        assert isinstance(message, HelloMessage)
        return message

    def payload(self, command: str, request_id: UUID | None = None, **extra: Any) -> dict[str, Any]:
        if self.device_id is None or self.firmware_version is None:
            raise RuntimeError("device identity has not been established")
        return {
            "protocol": "flightmill",
            "protocol_version": 1,
            "type": command,
            "device_id": self.device_id,
            "firmware_version": self.firmware_version,
            "request_id": str(request_id or uuid4()),
            **extra,
        }

    def exchange(
        self,
        payload: dict[str, Any],
        *,
        expect_error: str | None = None,
        conforming: bool = True,
    ) -> AckMessage | ErrorMessage:
        request_id = UUID(str(payload["request_id"]))
        self.write_payload(payload, conforming=conforming)
        message = self.wait_for(
            lambda item: isinstance(item, (AckMessage, ErrorMessage))
            and item.request_id == request_id,
            5.0,
            f"response to {payload.get('type')}",
            conforming=conforming,
        )
        assert isinstance(message, (AckMessage, ErrorMessage))
        if expect_error is None and not isinstance(message, AckMessage):
            raise AssertionError(
                f"{payload['type']} returned error {message.code}: {message.message}"
            )
        if expect_error is not None:
            if not isinstance(message, ErrorMessage) or message.code != expect_error:
                raise AssertionError(
                    f"{payload['type']} expected {expect_error}, received {type(message).__name__}"
                )
        return message

    def get_status(self, *, conforming: bool = True) -> StatusMessage:
        self.exchange(self.payload("get_status"), conforming=conforming)
        message = self.wait_for(
            lambda item: isinstance(item, StatusMessage),
            3.0,
            "status",
            conforming=conforming,
        )
        assert isinstance(message, StatusMessage)
        return message

    def arm(self, session_id: UUID, *, request_id: UUID | None = None) -> TrialArmedMessage:
        self.exchange(
            self.payload(
                "arm",
                request_id=request_id,
                session_id=str(session_id),
                config={"min_event_interval_us": MIN_EVENT_INTERVAL_US},
            )
        )
        message = self.wait_for(
            lambda item: isinstance(item, TrialArmedMessage) and item.session_id == session_id,
            3.0,
            "trial_armed",
        )
        assert isinstance(message, TrialArmedMessage)
        if message.config.min_event_interval_us != MIN_EVENT_INTERVAL_US:
            raise AssertionError(
                f"ARM interval mismatch: requested {MIN_EVENT_INTERVAL_US}, "
                f"received {message.config.min_event_interval_us}"
            )
        return message

    def start(self, session_id: UUID) -> TrialStartedMessage:
        self.exchange(self.payload("start", session_id=str(session_id)))
        message = self.wait_for(
            lambda item: isinstance(item, TrialStartedMessage) and item.session_id == session_id,
            3.0,
            "trial_started",
        )
        assert isinstance(message, TrialStartedMessage)
        return message

    def stop(self, session_id: UUID, reason: str) -> TrialStoppedMessage:
        self.exchange(self.payload("stop", session_id=str(session_id), stop_reason=reason))
        message = self.wait_for(
            lambda item: isinstance(item, TrialStoppedMessage) and item.session_id == session_id,
            5.0,
            "trial_stopped",
        )
        assert isinstance(message, TrialStoppedMessage)
        return message

    def disarm(self, session_id: UUID) -> None:
        self.exchange(self.payload("disarm", session_id=str(session_id)))


def operator_input(
    evidence: Evidence, prompt: str, *, allow_disconnect: bool = False
) -> str:
    """Wait for human input while retaining every received event for later checks.

    Only input() runs on the daemon thread. Serial parsing and evidence writes
    stay on this main thread, preserving protocol order without shared-file races.
    """
    board = evidence.board
    if board is None or board.serial is None or not board.serial.is_open:
        return input(prompt)
    answers: Queue[str | BaseException] = Queue()

    def receive_answer() -> None:
        try:
            answers.put(input(prompt))
        except BaseException as exc:
            answers.put(exc)

    reader = threading.Thread(target=receive_answer, name="operator-input", daemon=True)
    reader.start()
    disconnected = False
    while True:
        try:
            answer = answers.get(timeout=0.1 if disconnected else 0)
        except Empty:
            if disconnected:
                continue
            try:
                message = board.read_live_message(0.1)
            except SerialException as exc:
                if not allow_disconnect:
                    raise
                evidence.record_operator("expected_disconnect_read_error", type(exc).__name__)
                disconnected = True
                continue
            if message is not None:
                board._pending_messages.append(message)
            continue
        reader.join(timeout=1.0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


def operator_action(
    evidence: Evidence, name: str, prompt: str, *, allow_disconnect: bool = False
) -> bool:
    answer = operator_input(
        evidence, f"\nACTION: {prompt}\nType DONE when complete: ",
        allow_disconnect=allow_disconnect,
    ).strip()
    evidence.record_operator(name, answer)
    if answer.lower() == "done":
        evidence.add(name, True, "operator completed the requested action", answer)
        return True
    evidence.ambiguous(name, "operator completed the requested action", answer or "no answer")
    return False


def operator_yes_no(evidence: Evidence, name: str, prompt: str) -> bool:
    answer = operator_input(evidence, f"\nOBSERVE: {prompt}\nType YES or NO: ").strip()
    evidence.record_operator(name, answer)
    if answer.lower() == "yes":
        evidence.add(name, True, "YES", answer)
        return True
    if answer.lower() == "no":
        evidence.add(name, False, "YES", answer)
        return False
    evidence.ambiguous(name, "YES or NO", answer or "no answer")
    return False


def operator_text(evidence: Evidence, name: str, prompt: str) -> str:
    answer = operator_input(evidence, f"\nRECORD: {prompt}\nEnter the observation/method: ").strip()
    evidence.record_operator(name, answer)
    if answer:
        evidence.add(name, True, "nonempty operator record", answer)
    else:
        evidence.ambiguous(name, "nonempty operator record", "no answer")
    return answer


def validate_event_batch(events: list[EventMessage], expected_count: int) -> tuple[bool, str]:
    numbers = [event.event_n for event in events]
    expected_numbers = list(range(1, expected_count + 1))
    first_delta_ok = bool(events) and events[0].dt_us == 0
    dropped = [event.dropped_events for event in events]
    passed = numbers == expected_numbers and first_delta_ok and all(value == 0 for value in dropped)
    return (
        passed,
        f"event_n={numbers}; first_dt_us={events[0].dt_us if events else 'missing'}; dropped={dropped}",
    )


def collect_events(
    board: Board, expected_count: int, timeout_s: float = 20.0
) -> list[EventMessage]:
    events: list[EventMessage] = []
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline and len(events) < expected_count:
        message = board.read_message(max(0.05, deadline - time.monotonic()))
        if isinstance(message, EventMessage):
            events.append(message)
    settle_deadline = time.monotonic() + 1.5
    while time.monotonic() < settle_deadline:
        message = board.read_message(max(0.05, settle_deadline - time.monotonic()))
        if isinstance(message, EventMessage):
            events.append(message)
    return events


def run_conformance(board: Board, evidence: Evidence) -> None:
    valid_start = len(evidence.protocol_frames)
    hello = board.wait_for_hello()
    evidence.add(
        "hello_identity",
        hello.protocol_version == 1 and hello.state == "IDLE",
        "Protocol v1 hello in IDLE",
        f"protocol={hello.protocol_version}; state={hello.state}; boot_id={hello.boot_id}",
    )
    status = board.get_status()
    evidence.add("status_command", status.state == "IDLE", "IDLE status", status.model_dump_json())
    board.exchange(board.payload("ping"))
    session_id = uuid4()
    armed = board.arm(session_id)
    evidence.add(
        "configured_interval_echo",
        armed.config.min_event_interval_us == MIN_EVENT_INTERVAL_US,
        f"min_event_interval_us={MIN_EVENT_INTERVAL_US}",
        armed.model_dump_json(),
    )
    started = board.start(session_id)
    evidence.add(
        "command_start", started.start_source == "ui", "start_source=ui", started.start_source
    )
    recording = board.get_status()
    evidence.add(
        "recording_status",
        recording.state == "RECORDING",
        "RECORDING",
        recording.state,
    )
    summary = board.stop(session_id, "conformance")
    evidence.add(
        "zero_event_summary",
        summary.accepted_event_count == 0 and summary.dropped_events == 0,
        "accepted_event_count=0 and dropped_events=0",
        f"accepted_event_count={summary.accepted_event_count}; dropped_events={summary.dropped_events}",
    )
    board.disarm(session_id)
    valid_frames = list(evidence.protocol_frames[valid_start:])
    report = check_transcript(valid_frames)
    evidence.add(
        "real_firmware_protocol_conformance",
        report.passed,
        "zero conformance issues",
        "; ".join(f"{issue.code}@{issue.frame_index}" for issue in report.issues)
        or f"{len(report.parsed_messages)} messages",
    )

    wrong_identity = board.payload("get_status")
    wrong_identity["device_id"] = "WRONG-DEVICE"
    response = board.exchange(wrong_identity, expect_error="identity_mismatch", conforming=False)
    evidence.add(
        "invalid_identity_rejected",
        isinstance(response, ErrorMessage),
        "identity_mismatch",
        getattr(response, "code", "ack"),
    )

    bad_config = board.payload("set_config", config={"min_event_interval_us": 49_999})
    response = board.exchange(bad_config, expect_error="invalid_config", conforming=False)
    evidence.add(
        "invalid_config_rejected",
        isinstance(response, ErrorMessage),
        "invalid_config",
        getattr(response, "code", "ack"),
    )

    # D-011 permits only the new default and the explicit legacy interval.
    response = board.exchange(
        board.payload("set_config", config={"min_event_interval_us": 100_000}),
        expect_error="invalid_config", conforming=False,
    )
    evidence.add("unsupported_100ms_rejected", isinstance(response, ErrorMessage),
                 "invalid_config", getattr(response, "code", "ack"))
    legacy_session = uuid4()
    board.exchange(board.payload("arm", session_id=str(legacy_session),
                                 config={"min_event_interval_us": 50_000}))
    legacy_armed = board.wait_for(
        lambda item: isinstance(item, TrialArmedMessage) and item.session_id == legacy_session,
        3.0, "legacy trial_armed",
    )
    evidence.add("legacy_50ms_config_echo", legacy_armed.config.min_event_interval_us == 50_000,
                 "explicit legacy 50000 applied and echoed", legacy_armed.model_dump_json())
    board.disarm(legacy_session)
    board.exchange(board.payload("set_config", config={"min_event_interval_us": MIN_EVENT_INTERVAL_US}))

    response = board.exchange(
        board.payload("start", session_id=str(uuid4())),
        expect_error="invalid_state",
        conforming=False,
    )
    evidence.add(
        "out_of_state_start_rejected",
        isinstance(response, ErrorMessage),
        "invalid_state",
        getattr(response, "code", "ack"),
    )

    duplicate_session = uuid4()
    duplicate_request = uuid4()
    board.arm(duplicate_session, request_id=duplicate_request)
    duplicate_payload = board.payload(
        "arm",
        request_id=duplicate_request,
        session_id=str(duplicate_session),
        config={"min_event_interval_us": MIN_EVENT_INTERVAL_US},
    )
    response = board.exchange(duplicate_payload, expect_error="invalid_state", conforming=False)
    evidence.add(
        "duplicate_state_change_fails_closed",
        isinstance(response, ErrorMessage),
        "rejected without a second transition",
        getattr(response, "code", "ack"),
    )
    board.disarm(duplicate_session)

    board.write_raw(b"{malformed-json}\n")
    malformed = board.wait_for(
        lambda item: isinstance(item, ErrorMessage) and item.code == "malformed_command",
        3.0,
        "malformed_command error",
        conforming=False,
    )
    evidence.add(
        "malformed_command_rejected",
        isinstance(malformed, ErrorMessage),
        "malformed_command",
        getattr(malformed, "code", "missing"),
    )

    incompatible = board.payload("ping")
    incompatible["protocol_version"] = 2
    response = board.exchange(incompatible, expect_error="invalid_envelope", conforming=False)
    evidence.add(
        "incompatible_protocol_rejected",
        isinstance(response, ErrorMessage),
        "invalid_envelope",
        getattr(response, "code", "ack"),
    )


def require_safe_wiring(evidence: Evidence) -> None:
    items = [
        operator_yes_no(
            evidence,
            "prototype_opb800_present",
            "Is the actual OPB800W55Z prototype sensor connected?",
        ),
        operator_yes_no(
            evidence,
            "prototype_10k_present",
            "Is an external 10 kohm pull-up physically present from GPIO4/signal to 3.3 V?",
        ),
        operator_yes_no(
            evidence,
            "prototype_150_present",
            "Is a 150 ohm series resistor physically present in the IR LED supply path?",
        ),
        operator_yes_no(
            evidence,
            "prototype_button_present",
            "Is the physical START/STOP button connected to GPIO5 and GND?",
        ),
        operator_yes_no(
            evidence,
            "prototype_leds_present",
            "Are the PWR, READY, REC, and EVENT indicators or LED test assembly connected?",
        ),
    ]
    operator_text(
        evidence,
        "prototype_resistor_verification",
        "How were the 10 kohm and 150 ohm values verified (meter readings preferred; otherwise markings/known parts)?",
    )
    if not all(items):
        raise RuntimeError("safe prototype wiring was not confirmed; physical acceptance stopped")


def assert_sensor_status(
    board: Board, evidence: Evidence, name: str, expected: int
) -> StatusMessage:
    status = board.get_status()
    evidence.add(
        name,
        status.sensor_state == expected,
        f"sensor_state={expected}",
        f"sensor_state={status.sensor_state}; state={status.state}; event_n={status.event_n}",
    )
    return status


def run_physical(board: Board, evidence: Evidence) -> None:
    hello = board.wait_for_hello()
    evidence.add("physical_initial_hello", hello.state == "IDLE", "IDLE", hello.state)
    require_safe_wiring(evidence)
    operator_yes_no(
        evidence, "led_pwr_powered", "Is PWR visibly on while the prototype is powered?"
    )
    operator_yes_no(evidence, "led_ready_idle", "In IDLE, is READY blinking slowly and REC off?")

    if operator_action(evidence, "action_beam_clear", "Leave the sensor beam clear"):
        assert_sensor_status(board, evidence, "sensor_clear_initial", 0)
    if operator_action(
        evidence, "action_beam_blocked", "Block the sensor beam and keep it blocked"
    ):
        assert_sensor_status(board, evidence, "sensor_blocked", 1)
    if operator_action(evidence, "action_beam_clear_again", "Return the sensor beam to clear"):
        assert_sensor_status(board, evidence, "sensor_clear_restored", 0)

    idle_before = board.get_status()
    if operator_action(
        evidence,
        "action_idle_interruptions",
        "While IDLE, interrupt and clear the beam three deliberate times, then leave it clear",
    ):
        idle_after = board.get_status()
        evidence.add(
            "no_counting_idle",
            idle_after.event_n == idle_before.event_n == 0,
            "event_n remains 0",
            f"before={idle_before.event_n}; after={idle_after.event_n}",
        )
    operator_yes_no(
        evidence,
        "led_event_off_outside_recording",
        "During those IDLE interruptions, did EVENT remain off (no accepted-event pulse)?",
    )

    if operator_action(
        evidence,
        "action_unarmed_short_button",
        "While still IDLE/unarmed, press and release START/STOP briefly",
    ):
        warning = board.wait_for(
            lambda item: isinstance(item, ErrorMessage) and item.code == "button_unarmed",
            5.0,
            "button_unarmed error",
        )
        after_button = board.get_status()
        evidence.add(
            "unarmed_button_rejected",
            isinstance(warning, ErrorMessage) and after_button.state == "IDLE",
            "button_unarmed error and IDLE",
            f"error={getattr(warning, 'code', 'missing')}; state={after_button.state}",
        )
    operator_yes_no(
        evidence,
        "led_warning_pattern",
        "Did the EVENT/warning indicator show the documented three-fast-flash warning pattern?",
    )

    ten_session = uuid4()
    board.arm(ten_session)
    armed_before = board.get_status()
    evidence.add(
        "armed_counter_zero",
        armed_before.event_n == 0,
        "event_n=0",
        f"event_n={armed_before.event_n}",
    )
    operator_yes_no(evidence, "led_ready_armed", "While ARMED, is READY solid and REC off?")
    if operator_action(
        evidence,
        "action_armed_interruptions",
        "While ARMED but not recording, interrupt and clear the beam three times",
    ):
        armed_after = board.get_status()
        evidence.add(
            "no_counting_armed",
            armed_after.event_n == 0,
            "event_n remains 0",
            f"event_n={armed_after.event_n}",
        )
    started = board.start(ten_session)
    evidence.add(
        "serial_command_start",
        started.start_source == "ui",
        "RECORDING start_source=ui",
        started.model_dump_json(),
    )
    operator_yes_no(
        evidence,
        "led_rec_serial_start",
        "During serial-started RECORDING, are READY and REC solid?",
    )
    if operator_action(
        evidence,
        "action_ten_interruptions",
        f"Perform exactly TEN clearly separated beam interruptions (more than {MIN_EVENT_INTERVAL_US // 1000} ms apart), ending with the beam clear",
    ):
        ten_events = collect_events(board, 10)
        passed, observed = validate_event_batch(ten_events, 10)
        evidence.add(
            "ten_deliberate_interruptions",
            passed,
            "exactly event_n 1..10, first dt_us=0, dropped=0",
            observed,
        )
    else:
        ten_events = []
    operator_yes_no(
        evidence, "led_event_pulses", "Did EVENT visibly pulse for each accepted interruption?"
    )
    ten_summary = board.stop(ten_session, "ten_interruptions")
    evidence.add(
        "ten_event_final_summary",
        ten_summary.accepted_event_count == 10 and ten_summary.dropped_events == 0,
        "accepted_event_count=10; dropped_events=0",
        f"accepted_event_count={ten_summary.accepted_event_count}; dropped_events={ten_summary.dropped_events}",
    )
    board.disarm(ten_session)

    twenty_session = uuid4()
    board.arm(twenty_session)
    board.start(twenty_session)
    reset_status = board.get_status()
    evidence.add(
        "counter_reset_new_trial",
        reset_status.event_n == 0,
        "event_n=0 at new trial",
        f"event_n={reset_status.event_n}",
    )
    if operator_action(
        evidence,
        "action_twenty_revolutions",
        f"Rotate the actual flight-mill arm through exactly TWENTY complete flag passages/revolutions, each more than {MIN_EVENT_INTERVAL_US // 1000} ms apart",
    ):
        twenty_events = collect_events(board, 20, timeout_s=30.0)
        passed, observed = validate_event_batch(twenty_events, 20)
        evidence.add(
            "twenty_known_revolutions",
            passed,
            "exactly event_n 1..20, first dt_us=0, dropped=0",
            observed,
        )
    twenty_summary = board.stop(twenty_session, "twenty_revolutions")
    evidence.add(
        "twenty_event_final_summary",
        twenty_summary.accepted_event_count == 20 and twenty_summary.dropped_events == 0,
        "accepted_event_count=20; dropped_events=0",
        f"accepted_event_count={twenty_summary.accepted_event_count}; dropped_events={twenty_summary.dropped_events}",
    )
    board.disarm(twenty_session)

    button_session = uuid4()
    board.arm(button_session)
    if operator_action(
        evidence,
        "action_physical_start",
        "Press and release START/STOP briefly to start the ARMED trial",
    ):
        physical_started = board.wait_for(
            lambda item: isinstance(item, TrialStartedMessage)
            and item.session_id == button_session,
            5.0,
            "physical-button trial_started",
        )
        evidence.add(
            "physical_button_start",
            isinstance(physical_started, TrialStartedMessage)
            and physical_started.start_source == "physical_button",
            "trial_started start_source=physical_button",
            getattr(physical_started, "start_source", "missing"),
        )
    operator_yes_no(evidence, "led_rec_physical_start", "After physical start, is REC solid?")
    if operator_action(
        evidence,
        "action_recording_short_press",
        "While RECORDING, press and release START/STOP briefly",
    ):
        time.sleep(0.5)
        short_status = board.get_status()
        evidence.add(
            "recording_short_press_ignored",
            short_status.state == "RECORDING",
            "state remains RECORDING",
            short_status.state,
        )
    if operator_action(
        evidence,
        "action_physical_long_stop",
        "Press and hold START/STOP for at least 2 seconds, then release",
    ):
        physical_stop = board.wait_for(
            lambda item: isinstance(item, TrialStoppedMessage)
            and item.session_id == button_session,
            6.0,
            "physical-button trial_stopped",
        )
        evidence.add(
            "physical_button_long_stop",
            isinstance(physical_stop, TrialStoppedMessage)
            and physical_stop.stop_source == "physical_button",
            "one trial_stopped stop_source=physical_button",
            getattr(physical_stop, "stop_source", "missing"),
        )
        time.sleep(0.5)
        duplicate = False
        settle = time.monotonic() + 1.5
        while time.monotonic() < settle:
            message = board.read_message(max(0.05, settle - time.monotonic()))
            duplicate = duplicate or (
                isinstance(message, TrialStoppedMessage) and message.session_id == button_session
            )
        evidence.add(
            "physical_stop_not_duplicated",
            not duplicate,
            "no second trial_stopped",
            f"duplicate={duplicate}",
        )
    board.disarm(button_session)

    reset_session = uuid4()
    board.arm(reset_session)
    board.start(reset_session)
    if operator_action(
        evidence,
        "action_reset_event",
        "Interrupt and clear the beam once so the reset trial has at least one accepted event",
    ):
        reset_events = collect_events(board, 1)
        evidence.add(
            "reset_trial_event_present",
            len(reset_events) == 1,
            "one event before reset",
            f"events={len(reset_events)}",
        )
    old_boot_id = board.hello.boot_id if board.hello else ""
    frame_index_before_reset = len(evidence.protocol_frames)
    board.allow_startup_noise = True
    if operator_action(evidence, "action_board_reset", "Press the board RESET button once"):
        new_hello = board.wait_for_hello(15.0)
        after_reset_frames = evidence.protocol_frames[frame_index_before_reset:]
        clean_stop_seen = False
        for frame in after_reset_frames:
            try:
                clean_stop_seen = clean_stop_seen or isinstance(
                    parse_line(frame), TrialStoppedMessage
                )
            except ProtocolError:
                pass
        evidence.add(
            "reset_new_boot_id",
            new_hello.boot_id != old_boot_id,
            "new boot_id",
            f"before={old_boot_id}; after={new_hello.boot_id}",
        )
        evidence.add(
            "reset_stable_device_id",
            new_hello.device_id == evidence.device_id,
            "stable device_id",
            new_hello.device_id,
        )
        evidence.add(
            "reset_not_clean_stop",
            not clean_stop_seen,
            "no trial_stopped before new hello",
            f"trial_stopped_seen={clean_stop_seen}",
        )
        evidence.add("reset_post_boot_state", new_hello.state == "IDLE", "IDLE", new_hello.state)

    serial_number = next(
        (item.serial_number for item in list_ports.comports() if item.device == board.port_name),
        None,
    )
    if operator_action(
        evidence, "action_usb_disconnect", "Disconnect the USB cable while the board is idle",
        allow_disconnect=True,
    ):
        deadline = time.monotonic() + 15.0
        disappeared = False
        while time.monotonic() < deadline:
            if not any(item.device == board.port_name for item in list_ports.comports()):
                disappeared = True
                break
            time.sleep(0.25)
        evidence.add(
            "usb_disconnect_detected",
            disappeared,
            "serial device disappears",
            f"disappeared={disappeared}",
        )
        board.close()
    if operator_action(
        evidence,
        "action_usb_reconnect",
        "Reconnect the same USB cable and wait for the board to enumerate",
    ):
        deadline = time.monotonic() + 30.0
        found_port: str | None = None
        while time.monotonic() < deadline:
            candidates = [
                item
                for item in list_ports.comports()
                if item.vid == EXPECTED_VID
                and item.pid == EXPECTED_PID
                and (serial_number is None or item.serial_number == serial_number)
            ]
            if len(candidates) == 1:
                found_port = candidates[0].device
                break
            time.sleep(0.5)
        if found_port is None:
            evidence.add(
                "usb_reconnect_detected",
                False,
                "one matching ESP32-S3 serial port",
                "none or ambiguous",
            )
        else:
            board.port_name = found_port
            board.open()
            reconnect_hello = board.wait_for_hello(15.0)
            reconnect_status = board.get_status()
            evidence.add(
                "usb_reconnect_detected",
                reconnect_hello.device_id == evidence.device_id
                and reconnect_status.state == "IDLE",
                "stable device_id, valid hello/status, IDLE",
                f"port={found_port}; device_id={reconnect_hello.device_id}; boot_id={reconnect_hello.boot_id}; state={reconnect_status.state}",
            )
            operator_yes_no(
                evidence,
                "led_pwr_after_reconnect",
                "After reconnect, is PWR on and READY showing the documented IDLE pattern?",
            )


def run_stability(board: Board, evidence: Evidence, seconds: int) -> None:
    start_frame = len(evidence.protocol_frames)
    hello = board.wait_for_hello()
    status = board.get_status()
    evidence.add("stability_initial_state", status.state == "IDLE", "IDLE", status.state)
    start = time.monotonic()
    deadline = start + seconds
    heartbeats: list[HeartbeatMessage] = []
    extra_hellos: list[HelloMessage] = []
    wrong_identity: list[str] = []
    wrong_state: list[str] = []
    while time.monotonic() < deadline:
        message = board.read_message(max(0.05, min(1.0, deadline - time.monotonic())))
        if isinstance(message, HeartbeatMessage):
            heartbeats.append(message)
            if message.device_id != hello.device_id or message.boot_id != hello.boot_id:
                wrong_identity.append(message.model_dump_json())
            if message.state != "IDLE":
                wrong_state.append(message.state)
        elif isinstance(message, HelloMessage):
            extra_hellos.append(message)
    elapsed = time.monotonic() - start
    frames = evidence.protocol_frames[start_frame:]
    report = check_transcript(frames)
    minimum_heartbeats = max(1, int(seconds * 0.90))
    passed = (
        elapsed >= seconds
        and len(heartbeats) >= minimum_heartbeats
        and not extra_hellos
        and not wrong_identity
        and not wrong_state
        and not evidence.malformed_after_handshake
        and report.passed
    )
    evidence.add(
        "heartbeat_idle_stability",
        passed,
        f">={seconds}s, >={minimum_heartbeats} heartbeats, no reset/malformed/identity/state/conformance issue",
        f"elapsed_s={elapsed:.3f}; heartbeats={len(heartbeats)}; extra_hellos={len(extra_hellos)}; malformed={len(evidence.malformed_after_handshake)}; identity_issues={len(wrong_identity)}; state_issues={len(wrong_state)}; conformance_issues={len(report.issues)}",
    )


def default_output(mode: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return ROOT / "hardware" / "validation" / "evidence" / f"{stamp}_{mode}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("conformance", "physical", "stability"))
    parser.add_argument("--port", required=True)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--firmware-bin", type=Path)
    parser.add_argument("--seconds", type=int, default=600)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_dir = args.output_dir or default_output(args.mode)
    firmware_bin = args.firmware_bin.resolve() if args.firmware_bin else None
    evidence = Evidence(output_dir.resolve(), args.mode, args.port, firmware_bin)
    board = Board(args.port, evidence)
    try:
        board.open()
        if args.mode == "conformance":
            run_conformance(board, evidence)
        elif args.mode == "physical":
            run_physical(board, evidence)
        else:
            if args.seconds < 1:
                raise ValueError("--seconds must be positive")
            run_stability(board, evidence, args.seconds)
    except (AssertionError, RuntimeError, SerialException, TimeoutError, ValueError) as exc:
        evidence.add(
            "runner_exception",
            False,
            "run completes without exception",
            f"{type(exc).__name__}: {exc}",
        )
    except (EOFError, KeyboardInterrupt):
        evidence.ambiguous(
            "runner_interrupted", "operator completes all prompts", "input ended or run interrupted"
        )
    finally:
        board.close()
        evidence.finalize()
    print(f"Evidence directory: {evidence.output_dir}")
    print(f"Overall: {'PASS' if evidence.passed else 'NOT PASS'}")
    return 0 if evidence.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
