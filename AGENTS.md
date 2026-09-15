# Flight Mill operator-run testing policy

Applies to this project and future Flight Mill work. Whenever a visual UI check
or another interactive/manual functional check is needed, give the user a clear
task identifying the build/window, steps, expected observations, and what to
report. Group related observations. Pause at the acceptance gate, wait for the
user's report, and evaluate it before proceeding. An unavailable or ambiguous
report leaves the gate pending.

Do not use computer-use automation for these checks: no agent browser control,
Playwright-style UI driving, native-window automation, simulated mouse/keyboard
input, injected window-close messages, or automated screenshot collection in
place of the user's observation. Do not route around this policy with another
tool. User-supplied screenshots may be inspected; a sufficient report does not
require screenshots.

Noninteractive command-line Python/Node tests, static analysis, API-level tests
that do not operate the UI, file/hash comparisons, log analysis, and read-only
process/port checks remain allowed. Attribute these separately from operator
observations. Computed styles, mocked callbacks, HTTP responses, and automated
test passes are not user-performed visual/interactive acceptance checks.

Preserve historical computer-use evidence with its original attribution; do not
repeat or relabel it. Checkpoint 04E-R requires no new visual/interactive check,
application/browser launch, recording, reservation, or dialog matrix. If an
unexpected issue needs operator interaction, stop that path and ask the user.
