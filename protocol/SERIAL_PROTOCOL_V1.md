# FlightMill Serial Protocol v1

## Transport and envelope

FlightMill Serial Protocol v1 is newline-delimited UTF-8 JSON over USB serial at
115200 baud. Each physical line contains exactly one JSON object and ends in
`LF`; receivers also accept `CRLF`. A malformed byte sequence, JSON value, or
schema-invalid object is diagnostic evidence: log it and warn, but never
reinterpret it as an event. During serial attachment before identity is known,
the host may log a discarded leading fragment as an informational diagnostic;
normal malformed-message warnings still apply after a genuine hello.

Every object contains:

- `protocol`: exactly `flightmill`
- `protocol_version`: integer `1`
- `type`: one enumerated message type
- `device_id`: stable device identity, or `*` on a discovery command
- `firmware_version`: device firmware, or `unknown` on a discovery command

Host commands carry a unique UUID `request_id`. Device-originated messages carry
a boot-scoped, monotonically increasing integer `message_seq` and a nonempty
`boot_id`. Each command produces exactly one correlated `ack` or `error` before
any command-specific notification. Firmware 0.1.5+ may precede a successful
wildcard `get_status` ACK with a `hello` identity preamble, as specified below.

## Discovery identity rule

Before `hello` establishes identity, `ping` or `get_status` may use
`device_id="*"` and `firmware_version="unknown"`. All subsequent commands must
echo the discovered device and firmware values. A mismatch fails closed.

### On-demand identity discovery (firmware 0.1.5+)

A fully validated `get_status` addressed to `device_id="*"` and
`firmware_version="unknown"` emits, in order:

1. A fresh, device-generated `hello` with the current boot ID, current state,
   next monotonically increasing message sequence, and that boot's reset reason.
2. One `ack` with the command's UUID `request_id`, command `get_status` and current state.
3. The current `status`, immediately following that ACK in message sequence.

The identity preamble is an explicit exception to the ACK-first order. It adds
no message fields or types, so existing Protocol 1 parsers can read it. It is
not a reset or replay of sequence zero. Discovery does not change the boot,
state, session, configuration, counters, queued events or trial timing.
Repeated requests report current identity with new sequence numbers. An exact-
identity `get_status` continues to emit only ACK/status; `ping` remains ACK only.
Invalid envelopes, unexpected fields and identity mismatches are rejected before
any identity preamble is emitted.

The application accepts identity only from a valid device `hello`. It then
requires the matching `get_status` ACK in IDLE and the immediately following
status from the same device/firmware/boot, also IDLE with no session, before
becoming ready. Neither buffered telemetry nor an ACK/status without hello
establishes identity. A foreign ARMED/RECORDING/STOPPING/COMPLETE session is not
adopted or cleared by discovery, and an ERROR device cannot become ready.

This supports plugging the instrument in before opening the app, with no
startup timing requirement. Firmware through 0.1.4 emits hello only at boot;
those devices still need the boot hello to remain available or the guided USB
reconnect recovery path. The host retains that legacy behavior until firmware
is updated. The 8192-byte USB TX queue remains finite and is not itself the
on-demand discovery mechanism.

## Commands

| Type | Additional fields | Valid state |
|---|---|---|
| `ping` | none | any responsive state |
| `get_status` | none | any responsive state |
| `arm` | `session_id`, `config` | IDLE |
| `start` | `session_id` | ARMED |
| `stop` | `session_id`, `stop_reason` | RECORDING |
| `disarm` | `session_id` | ARMED or COMPLETE |
| `set_config` | `config` | IDLE |
| `self_test` | none | IDLE or ARMED |

`config.min_event_interval_us` defaults to 150,000 as of firmware 0.1.3-dev
(owner-requested decision D-011, 2026-09-10). The bounded supported set is
50,000 and 150,000; all other values remain invalid. The exact requested value
is applied and echoed in `trial_armed`. Rejection is strictly below the
configured interval since the last accepted event; equality is accepted.

This explicitly extends the original v1 allowed-value set without changing
message fields or event semantics. Updated parsers still accept old 50,000 us
records, and updated firmware accepts explicit 50,000 us legacy requests.
Firmware through 0.1.2-dev and old strict parsers do not accept 150,000 us;
update the paired host/firmware before using the new value. Never silently
fall back to 50,000 or label a 50,000 us trial as 150,000 us. Historical examples
retain their original values. Physical acceptance of the new setting remains
a separate requirement.

## Device messages

| Type | Purpose |
|---|---|
| `hello` | Device identity, version, current state, and last reset reason; sent at boot and as the 0.1.5+ discovery preamble. |
| `ack` | Correlates successful command acceptance. |
| `error` | Correlates a rejected command or reports an asynchronous fault. |
| `status` / `heartbeat` | State, optional session, sensor value, counts, and uptime. |
| `sensor_state` | Clear (`0`) or blocked (`1`) observation. |
| `trial_armed` | Confirms session/config association without starting time. |
| `trial_started` | Confirms atomic counter/timing reset and RECORDING entry. |
| `event` | One accepted revolution with lossless integer source timing. |
| `trial_stopped` | Final accepted count, dropped count, duration, and stop cause. |

An event contains `session_id`, positive `event_n`, `event_us` relative to trial
start, `dt_us`, and cumulative `dropped_events`. The first event has `event_n=1`
and `dt_us=0`; subsequent events have positive `dt_us`. Firmware does not send
floating-point distance or speed as authoritative values.

## State machine

```text
BOOTING -> IDLE -> ARMED -> RECORDING -> STOPPING -> COMPLETE
              ^                                    |
              +--------------- DISARM -------------+
Any state may enter ERROR. Reset returns to BOOTING with a new boot_id.
```

- ARM associates a host UUID and clears pending trial counters without timing.
- START resets event count, dropped count, event timing, and trial zero before
  entering RECORDING.
- A short physical press starts only while ARMED.
- A press held at least 1,500 ms requests stop only while RECORDING. Short
  recording presses are ignored.
- A physical press while IDLE emits a visible warning and starts nothing.
- STOP emits a final summary before COMPLETE. The host then finalizes files or
  marks them incomplete if correlation, session, boot, or timeout checks fail.

## Integrity handling

The host records only events matching the active session and current boot.
Duplicate/nonmonotonic messages, sequence gaps, event gaps, nonmonotonic time,
and changes in cumulative dropped events are logged and surfaced. A boot change
or disconnect while RECORDING makes the trial incomplete.

The machine-readable contract is `serial_protocol_v1.schema.json`; canonical
valid and invalid lines are in `examples/`.
