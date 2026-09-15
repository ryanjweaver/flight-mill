# Data integrity invariants

1. Only messages for the active `session_id` and current `boot_id` may become
   raw data rows.
2. Trial events must originate in the confirmed device RECORDING session.
   The host may receive and persist its in-flight events while RECORDING or
   STOPPING, before consuming the matching terminal summary or closing the trial
   through the bounded failure path. Host STOPPING does not extend device capture.
3. `message_seq`, `event_n`, `event_us`, and cumulative `dropped_events` never
   move backward within one boot/session.
4. Duplicates, gaps, resets, malformed lines, and out-of-session events are
   retained in the diagnostic log and surfaced as integrity findings.
5. Received integer timing is never altered to hide a fault.
6. The raw CSV header and column order never vary.
7. The first event has `dt_us=0`, `dt_ms=0`, `dt_s=0`, and blank speed.
8. Radius is positive, defaults to 0.10 m, and is persisted with each trial.
9. Accepted events are never removed by downstream speed or bout rules.
10. Final paths are reserved before ARM and are never overwritten.
11. Partial files exist before ARM completion and survive abnormal termination.
12. `incomplete=false` is written only after a correlated, valid final summary.
13. A clean zero-event trial produces a header-only CSV and complete metadata.
14. The finalized raw file's SHA-256 is stored in metadata.
15. Local timestamps include an ISO 8601 UTC offset; device timing remains
    monotonic integer microseconds relative to trial start.
