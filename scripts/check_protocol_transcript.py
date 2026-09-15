"""Check a newline-delimited protocol transcript for V1 conformance."""

from __future__ import annotations

import argparse
from pathlib import Path

from flightmill.protocol.conformance import check_transcript


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("transcript", type=Path)
    args = parser.parse_args()
    frames = args.transcript.read_bytes().splitlines(keepends=True)
    report = check_transcript([frame for frame in frames if frame.strip()])
    if report.passed:
        print(f"Protocol conformance passed: {len(report.parsed_messages)} messages")
        return 0
    print("Protocol conformance failed:")
    for issue in report.issues:
        print(f"- frame {issue.frame_index}: {issue.code}: {issue.detail}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
