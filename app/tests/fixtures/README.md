# Synthetic serial fixture

`serial_zero_event_session.jsonl` is generated entirely by
`generate_serial_session.py` using `SimulatedDevice`, fixed synthetic identifiers,
five host commands and a clock advance of 26,287 microseconds. It contains no
physical device capture or research recording.

The fixture exercises boot discovery, IDLE confirmation, ARM, START, a short
zero-event trial, STOP and DISARM through the real host serial worker. Request
and session UUIDs are rebound to the host's generated values during replay.

To regenerate from the repository root in PowerShell:

```powershell
$env:PYTHONPATH = "app/src;."
python app/tests/fixtures/generate_serial_session.py
```

The fixture is software-test data, not evidence of physical hardware acceptance.

## Checkout-independent diagnostic tests

The diagnostic unit tests use fake ports and synthetic captures. Their shared
test configuration supplies the unrelated Git provenance field as
`synthetic-test-checkout`, so these tests run from a source ZIP or a new checkout
without a Git `HEAD`. Production evidence collection and its provenance behavior
are unchanged.
