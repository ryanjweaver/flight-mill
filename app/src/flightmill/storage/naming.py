"""R-compatible and Windows-safe trial filename handling."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path


_TOKEN_BREAKS = re.compile(r"[<>:\"/\\\\|?*._\s]+")
_DISALLOWED = re.compile(r"[^A-Za-z0-9-]+")
_MULTI_HYPHEN = re.compile(r"-+")
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


class FilenameError(ValueError):
    """Raised when safe R-compatible naming is impossible."""


class FilenameCollisionError(FileExistsError):
    """Raised when any final or partial trial path already exists."""


def sanitize_token(value: str, *, field: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.strip()).encode("ascii", "ignore").decode()
    normalized = _TOKEN_BREAKS.sub("-", normalized)
    normalized = _DISALLOWED.sub("-", normalized)
    normalized = _MULTI_HYPHEN.sub("-", normalized).strip("- .")
    if not normalized:
        raise FilenameError(f"{field} has no usable filename characters")
    if normalized.upper() in _RESERVED:
        raise FilenameError(f"{field} is a reserved Windows device name")
    return normalized


@dataclass(frozen=True, slots=True)
class TrialName:
    species_code: str
    trial_type: str
    trial_number: int
    well_id: str
    attempt: int

    def __post_init__(self) -> None:
        if self.trial_number < 1:
            raise FilenameError("trial_number must be a positive integer")
        if self.attempt < 1:
            raise FilenameError("attempt must be a positive integer")

    @property
    def base(self) -> str:
        species = sanitize_token(self.species_code, field="species_code")
        trial_type = sanitize_token(self.trial_type, field="trial_type")
        well = sanitize_token(self.well_id, field="well_id")
        return f"{species}_{trial_type}{self.trial_number}_{well}_Attempt{self.attempt}"

    @property
    def raw_filename(self) -> str:
        return f"{self.base}.csv"


@dataclass(frozen=True, slots=True)
class TrialPaths:
    raw: Path
    metadata: Path
    protocol: Path
    raw_partial: Path
    metadata_partial: Path
    protocol_partial: Path

    @classmethod
    def build(cls, output_dir: Path, name: TrialName) -> "TrialPaths":
        base = name.base
        return cls(
            raw=output_dir / f"{base}.csv",
            metadata=output_dir / f"{base}_metadata.csv",
            protocol=output_dir / f"{base}_protocol.jsonl",
            raw_partial=output_dir / f"{base}.partial.csv",
            metadata_partial=output_dir / f"{base}_metadata.partial.csv",
            protocol_partial=output_dir / f"{base}_protocol.partial.jsonl",
        )

    def assert_available(self) -> None:
        collisions = [path for path in self.all_paths() if path.exists()]
        if collisions:
            joined = ", ".join(str(path) for path in collisions)
            raise FilenameCollisionError(
                "Trial files already exist; change attempt or destination: " + joined
            )

    def all_paths(self) -> tuple[Path, ...]:
        return (
            self.raw,
            self.metadata,
            self.protocol,
            self.raw_partial,
            self.metadata_partial,
            self.protocol_partial,
        )
