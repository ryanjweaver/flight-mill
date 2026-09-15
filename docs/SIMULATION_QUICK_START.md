# Practice with the simulator

Install the [Windows package](WINDOWS_DISTRIBUTION.md), or follow the
[developer guide](DEVELOPER_GUIDE.md) to run from source.

1. In Instrument settings select the simulation acquisition source, then connect.
   Confirm the persistent SIMULATION label.
2. Enter Trial setup, including species code, trial type/number, well/position,
   attempt, optional specimen ID and optional **Trial duration**. Validate setup.
3. Confirm the output folder and arm radius. Choose a simulation profile, such
   as steady flight, in Simulation lab.
4. Arm, start, and stop the trial, or let Trial duration stop it. Wait for saving.
5. Open Trial archive to inspect/export the saved bundle. Use a new attempt for
   another trial; existing filenames are never silently overwritten.

The default minimum accepted interval is 150,000 microseconds. The explicit
legacy option uses 50,000 microseconds. Simulation rehearses the interface and
integrity handling; it is not an electrical or biological validation.

## Interpretation

One accepted event represents one revolution. Distance is accepted count times
the armed circumference. Latest interval speed describes a completed interval;
the first event alone cannot provide speed. A stale display is not a measured
zero. The bee schematic indicates pulse activity, not position or wingbeats.

Steady, ramp, flight/rest, zero-event, stress and manual profiles provide
repeatable practice. Deliberate disconnect/reset/loss controls can produce
incomplete trials; retain their files for inspection instead of editing them.

The source launcher defaults to `data/simulations`; the installed shortcut uses
`Documents/FlightMill/Trials`. Confirm or change the output folder under
Instrument settings. Keep raw CSV, metadata, protocol log, manifest and journal
together. The simulation manifest identifies synthetic data, while raw CSV
retains the nine-column analysis contract.

In browser mode, closing or refreshing a tab does not stop an active trial.
Return to the same local address to stop it, and keep the application process
running. Desktop mode manages the application-close workflow.
