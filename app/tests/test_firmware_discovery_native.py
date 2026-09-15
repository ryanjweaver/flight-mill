"""Execute the real C++ command parser with fake hardware and real ArduinoJson.

This does not test USB, Windows, ESP32 timing, or physical acceptance. Set CXX and
FLIGHTMILL_ARDUINOJSON_INCLUDE to use compilers/headers outside the local cache.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
NATIVE = ROOT / "firmware/tests/native"
DEVICE = "FM-S3-020000000001"
BOOT = "00000000-00000000-00000001"
REQUEST = "115e55e4-2ccf-43d4-9933-a729ff6f98a6"
SECOND_REQUEST = "39e642aa-bf5b-400e-9d6a-ceaf52843393"
SESSION = "20000000-0000-4000-8000-000000000001"


@pytest.fixture(scope="module")
def firmware_harness(tmp_path_factory):
    compiler = os.environ.get("CXX") or shutil.which("g++") or shutil.which("clang++")
    if not compiler and os.name == "nt":
        for path in (
            Path("C:/rtools43/x86_64-w64-mingw32.static.posix/bin/g++.exe"),
            Path("C:/rtools40/mingw64/bin/g++.exe"),
        ):
            if path.is_file():
                compiler = str(path)
                break
    if not compiler:
        pytest.skip("Native C++ compiler unavailable; set CXX to g++/clang++")
    headers = Path(os.environ.get(
        "FLIGHTMILL_ARDUINOJSON_INCLUDE",
        ROOT / "firmware/.pio/libdeps/supermini_hw747_v002/ArduinoJson/src",
    ))
    if not (headers / "ArduinoJson.h").is_file():
        pytest.skip("ArduinoJson headers unavailable; build firmware or set include override")
    target = tmp_path_factory.mktemp("firmware-native") / "discovery_harness.exe"
    command = [
        compiler, "-std=c++17", "-O0", "-Wall", "-Wextra",
        "-I", str(NATIVE / "stubs"), "-I", str(ROOT / "firmware/include"),
        "-I", str(headers), str(NATIVE / "discovery_harness.cpp"), "-o", str(target),
    ]
    if os.name == "nt":
        command += ["-static-libgcc", "-static-libstdc++"]
    environment = os.environ.copy()
    # Rtools' GCC driver locates its assembler/linker via PATH.
    compiler_path = Path(shutil.which(compiler) or compiler).resolve()
    environment["PATH"] = str(compiler_path.parent) + os.pathsep + environment.get("PATH", "")
    result = subprocess.run(command, text=True, capture_output=True, timeout=90, env=environment)
    assert result.returncode == 0, result.stdout + result.stderr
    return target


def command(*, exact=False, request=REQUEST, **changes):
    frame = {
        "protocol": "flightmill", "protocol_version": 1, "type": "get_status",
        "device_id": DEVICE if exact else "*",
        "firmware_version": "0.1.5-dev" if exact else "unknown", "request_id": request,
    }
    frame.update(changes)
    return frame


def dispatch(firmware_harness, *commands, state="IDLE"):
    source = "\n".join(
        item if isinstance(item, str) else json.dumps(item) for item in commands
    ) + "\n"
    result = subprocess.run(
        [str(firmware_harness), state], input=source, text=True,
        capture_output=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    messages = [json.loads(line) for line in result.stdout.splitlines()]
    report = json.loads(result.stderr)
    assert report["before"] == report["after"], "Discovery changed device/trial/hardware state"
    assert report["next_message_seq"] == 4000 + len(messages)
    for number, message in enumerate(messages, 4000):
        assert message["message_seq"] == number
        assert message["device_id"] == DEVICE
        assert message["boot_id"] == BOOT
        assert message["firmware_version"] == "0.1.5-dev"
        assert message["protocol"] == "flightmill" and message["protocol_version"] == 1
        assert message["state"] == state
    return messages, report


@pytest.mark.parametrize("state", [
    "BOOTING", "IDLE", "ARMED", "RECORDING", "STOPPING", "COMPLETE", "ERROR",
])
def test_wildcard_status_emits_genuine_current_boot_hello_without_reset(firmware_harness, state):
    messages, report = dispatch(firmware_harness, command(), state=state)
    assert [message["type"] for message in messages] == ["hello", "ack", "status"]
    hello, ack, status = messages
    assert hello["reset_reason"] == "power_on"
    assert ack["request_id"] == REQUEST and ack["command"] == "get_status"
    assert status["uptime_ms"] == 3600000  # Long after finite startup buffering.
    assert status["event_n"] == 42 and status["dropped_events"] == 3
    assert status["sensor_state"] == 0
    if state in {"ARMED", "RECORDING", "STOPPING", "COMPLETE"}:
        assert status["session_id"] == SESSION
    else:
        assert "session_id" not in status
    for field in ("serial_begins", "serial_resizes", "delays", "digital_writes", "interrupts"):
        assert report["after"][field] == 0


def test_exact_identity_status_preserves_existing_ack_status_contract(firmware_harness):
    messages, _ = dispatch(firmware_harness, command(exact=True))
    assert [message["type"] for message in messages] == ["ack", "status"]
    assert messages[0]["request_id"] == REQUEST


def test_repeated_discovery_uses_same_boot_and_advancing_sequence(firmware_harness):
    messages, _ = dispatch(
        firmware_harness, command(), command(exact=True, request=SECOND_REQUEST), command(),
    )
    assert [message["type"] for message in messages] == [
        "hello", "ack", "status", "ack", "status", "hello", "ack", "status",
    ]
    assert [message["request_id"] for message in messages if message["type"] == "ack"] == [
        REQUEST, SECOND_REQUEST, REQUEST,
    ]


def test_ping_does_not_emit_an_unsolicited_hello(firmware_harness):
    messages, _ = dispatch(firmware_harness, command(type="ping"))
    assert [message["type"] for message in messages] == ["ack"]
    assert messages[0]["request_id"] == REQUEST


@pytest.mark.parametrize(("invalid", "error"), [
    ("not json", "malformed_command"),
    ("[]", "malformed_command"),
    (command(request="not-a-uuid"), "invalid_envelope"),
    (command(protocol="other"), "invalid_envelope"),
    (command(protocol_version=2), "invalid_envelope"),
    (command(type="other"), "invalid_envelope"),
    (command(unexpected=True), "unexpected_field"),
    (command(session_id=SESSION), "unexpected_field"),
    (command(device_id="FM-WRONG"), "identity_mismatch"),
    (command(device_id=DEVICE), "identity_mismatch"),
    (command(firmware_version="0.1.5-dev"), "identity_mismatch"),
    (command(exact=True, firmware_version="0.1.4-dev"), "identity_mismatch"),
])
def test_invalid_discovery_cannot_trigger_hello(firmware_harness, invalid, error):
    messages, _ = dispatch(firmware_harness, invalid)
    assert [message["type"] for message in messages] == ["error"]
    assert messages[0]["code"] == error
