# Data dictionary

## Raw CSV

The raw file is UTF-8 without a byte-order mark and has exactly this header:

```csv
event_n,time_ms,time_s,dt_ms,dt_s,revolutions,cumulative_distance_m,speed_m_s,dropped_events
```

| Column | Definition |
|---|---|
| `event_n` | Positive accepted revolution count assigned by the device. |
| `time_ms` | `event_us / 1,000`, relative to trial START. |
| `time_s` | `event_us / 1,000,000`, relative to trial START. |
| `dt_ms` | `dt_us / 1,000`; zero for the first event. |
| `dt_s` | `dt_us / 1,000,000`; zero for the first event. |
| `revolutions` | Equal to `event_n` in single-channel V1. |
| `cumulative_distance_m` | `event_n * 2 * pi * arm_radius_m`. |
| `speed_m_s` | `circumference_m / dt_s`; blank for the first event. |
| `dropped_events` | Cumulative device ring-buffer loss count. |

One accepted interruption produces one row. A zero-event trial contains only the
header. No metadata preamble or extra product columns are allowed.

Raw `speed_m_s` is the average over the entire preceding pulse interval,
including any rest. One flag per revolution cannot reveal when motion resumed
between pulses, so this value cannot recover moving speed for the first pass
after a rest. The first event of a trial has no preceding interval and is blank.

The prototype display marks an interval longer than `max(2 seconds, 2 * the
preceding dt_s)` as a long gap and shows unavailable speed for that event in the
readout, chart and event table. This uses device timing, not USB receipt timing.
Subsequent intervals use the same rule. This display hint is not a scientific
flight-bout classifier: it may also flag an abrupt slowdown. Accepted rows,
distance, timestamps and raw CSV interval averages remain intact. The API's
`speed_m_s` follows this display rule; `interval_speed_m_s` retains the raw
interval average and `speed_after_gap` explains a withheld display value.

## Default calculation fixture

At radius 0.10 m, circumference is `0.6283185307179586` m. Event 1 has
`0.6283185307` m cumulative distance and blank speed. Two events one second
apart give event 2 speed `0.6283185307` m/s. Twenty revolutions give
`12.5663706144` m.

## Files

Default base name is
`{species_code}_{trial_type}{trial_number}_{well_id}_Attempt{attempt}`. The raw
file adds `.csv`, metadata adds `_metadata.csv`, and diagnostics add
`_protocol.jsonl`. Timestamps belong in metadata, never after `AttemptN`.
