"""Exact raw CSV serialization primitives."""

from __future__ import annotations

import csv
from collections.abc import Iterable
from pathlib import Path

from flightmill.acquisition.calculations import DerivedEvent, raw_csv_row
from flightmill.constants import RAW_CSV_HEADER


def write_raw_csv(path: Path, events: Iterable[DerivedEvent]) -> None:
    """Create a new UTF-8 raw CSV and never overwrite an existing path."""

    with path.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(RAW_CSV_HEADER)
        for event in events:
            writer.writerow(raw_csv_row(event))
        stream.flush()
