"""Durable, non-overwriting trial bundles and conservative startup discovery.

The exclusively created journal reserves a basename across cooperating writers.
Final promotion also refuses existing destinations, including files created by
another program after ARM. A bundle is complete only when its last journal
transaction says complete AND its metadata, artifacts and raw checksum agree.
Discovery never renames, repairs or resumes interrupted recordings.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import json
import logging
import os
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from flightmill.acquisition.calculations import DerivedEvent, raw_csv_row
from flightmill.constants import RAW_CSV_HEADER
from flightmill.storage.metadata import TrialMetadata
from flightmill.storage.naming import FilenameCollisionError, TrialName, TrialPaths


_SUFFIXES = {
    "raw": (".csv", ".partial.csv"),
    "metadata": ("_metadata.csv", "_metadata.partial.csv"),
    "protocol": ("_protocol.jsonl", "_protocol.partial.jsonl"),
    "simulation": ("_simulation.json", "_simulation.partial.json"),
}
_LOGGER = logging.getLogger(__name__)
_WINDOWS_FILE_DELAYS = (0.01, 0.02, 0.04, 0.08, 0.10)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _sync_directory(directory: Path) -> None:
    # Windows has no portable directory fsync through Python. File contents and
    # the journal are synced; filesystem/power-loss guarantees remain OS-owned.
    if os.name != "nt":
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _rename_no_replace(source: Path, destination: Path) -> None:
    if os.name == "nt":
        # Retry preserves Windows rename's atomic no-clobber behavior. Existing
        # destinations raise WinError 183/80 immediately and are never replaced.
        _retry_windows_file_operation(lambda: os.rename(source, destination), "Final promotion")
    else:
        os.link(source, destination)  # Atomic no-clobber promotion on POSIX.
        source.unlink()


def _retry_windows_file_operation(operation: Callable[[], None], label: str) -> None:
    """Retry only transient Windows sharing/access denial, for at most 250 ms.

    Windows readers/indexers can briefly hold a source or destination without
    delete sharing. The already-synced source stays unchanged across retries.
    Real permission failures still propagate after this bounded retry budget;
    disk-full, missing-file and other errors propagate immediately.
    """
    for attempt in range(len(_WINDOWS_FILE_DELAYS) + 1):
        try:
            operation()
            if attempt:
                _LOGGER.info("%s recovered after %d Windows sharing/access retries", label, attempt)
            return
        except OSError as exc:
            if (getattr(exc, "winerror", None) not in {5, 32, 33}
                    or attempt == len(_WINDOWS_FILE_DELAYS)):
                raise
            time.sleep(_WINDOWS_FILE_DELAYS[attempt])


def _replace_journal_with_retry(source: Path, destination: Path) -> None:
    _retry_windows_file_operation(lambda: os.replace(source, destination), "Journal replacement")


def _artifact_files(directory: Path, base: str) -> list[dict[str, str]]:
    result = []
    for kind, suffixes in _SUFFIXES.items():
        # If an external file occupied the final name after ARM, the writer's
        # recoverable partial is the artifact to inspect/export.
        for suffix in reversed(suffixes):
            path = directory / f"{base}{suffix}"
            if path.is_file() and not path.is_symlink():
                result.append({"kind": kind, "name": path.name, "path": str(path)})
                break
    for kind, suffix in (("journal", "_journal.json"), ("journal_pending", "_journal.update.json"),
                         ("journal_recovery", "_journal.recovery.json")):
        path = directory / f"{base}{suffix}"
        if path.is_file() and not path.is_symlink():
            result.append({"kind": kind, "name": path.name, "path": str(path)})
    return result


class TrialWriter:
    """One ordered owner for raw rows, protocol evidence and trial sidecars."""

    flush_interval_s = 0.250
    sync_interval_s = 1.0

    @classmethod
    def create(
        cls, output_dir: Path, name: TrialName, metadata: TrialMetadata,
        simulation: dict | None, *, source: str = "simulation",
    ) -> TrialWriter:
        metadata = TrialMetadata.model_validate(metadata.model_dump())
        if source not in {"simulation", "serial"}:
            raise ValueError("New trials require explicit simulation or serial provenance")
        if (source == "simulation") != (simulation is not None):
            raise ValueError("Simulation provenance must match the selected source")
        if not metadata.incomplete:
            raise ValueError("A newly reserved trial must be incomplete")
        metadata_name = TrialName(
            metadata.species_code, metadata.trial_type, metadata.trial_number,
            metadata.well_id, metadata.attempt,
        )
        if metadata_name.base != name.base:
            raise ValueError("Filename and metadata identify different trials")
        directory = Path(output_dir).expanduser().resolve()
        directory.mkdir(parents=True, exist_ok=True)
        self = cls()
        self.output_dir = directory
        self.name = name
        self.source = source
        self.paths = TrialPaths.build(directory, name)
        self._journal_path = directory / f"{name.base}_journal.json"
        self._journal_update_path = directory / f"{name.base}_journal.update.json"
        self._journal_recovery_path = directory / f"{name.base}_journal.recovery.json"
        self._journal_update_failed = False
        self._final = {kind: directory / f"{name.base}{suffixes[0]}"
                       for kind, suffixes in _SUFFIXES.items()}
        self._partial = {kind: directory / f"{name.base}{suffixes[1]}"
                         for kind, suffixes in _SUFFIXES.items()}
        candidates = [*self._final.values(), *self._partial.values(),
                      self._journal_path, self._journal_update_path, self._journal_recovery_path]
        collisions = [str(path) for path in candidates if path.exists() or path.is_symlink()]
        if collisions:
            raise FilenameCollisionError("Trial files already exist: " + ", ".join(collisions))
        if source == "serial":
            self._final.pop("simulation")
            self._partial.pop("simulation")
        self._metadata = metadata
        simulation = simulation or {}
        self._simulation = {
            **json.loads(json.dumps(simulation, allow_nan=False)),
            "manifest_schema_version": 1,
            "acquisition_mode": "simulation",
            "mode": "simulation",
            "trial_uuid": str(metadata.trial_uuid),
            "scenario": simulation.get("scenario", simulation.get("profile", "steady")),
            "scenario_version": simulation.get("scenario_version", 1),
            "seed": simulation.get("seed"),
            "clock_mode": simulation.get("clock_mode", "real-time-1x"),
            "notice": "SIMULATION. Keep this manifest and metadata with the raw CSV.",
            "actions": [],
        }
        self._streams: dict[str, TextIO] = {}
        self._promoted: set[str] = set()
        self._status = "reserved"
        self._reason: str | None = None
        self._closed = False
        self._sidecars_dirty = True
        self.row_count = 0
        self.durable_row_count = 0
        self.last_checkpoint_at: str | None = None
        self._last_flush = time.monotonic()
        self._last_sync = self._last_flush
        # The journal's exclusive creation is the reservation linearization point.
        try:
            with self._journal_path.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(self._journal(), stream, indent=2, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError as exc:
            raise FilenameCollisionError(f"Trial reservation already exists: {name.base}") from exc
        try:
            # Recheck after reservation to detect non-cooperating external writers.
            if any(path.exists() or path.is_symlink() for path in self._final.values()):
                raise FilenameCollisionError("A final trial filename was created during reservation")
            for kind, path in self._partial.items():
                self._streams[kind] = path.open("x+", encoding="utf-8", newline="")
            self._raw_writer = csv.writer(self._streams["raw"], lineterminator="\n")
            self._raw_writer.writerow(RAW_CSV_HEADER)
            self.log_action("reserved", {"filename": name.raw_filename})
            self.checkpoint(force=True)
        except BaseException:
            self._status = "incomplete"
            self._reason = "reservation_or_initial_write_failed"
            self._best_effort_journal()
            self._close_streams()
            raise
        return self

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("Trial writer is closed")

    def _journal(self) -> dict[str, Any]:
        return {
            "journal_schema_version": 1,
            "acquisition_mode": self.source,
            "required_artifacts": list(self._final),
            "trial_uuid": str(self._metadata.trial_uuid),
            "base": self.name.base,
            "status": self._status,
            "reason": self._reason,
            "row_count": self.row_count,
            "durable_row_count": self.durable_row_count,
            "last_checkpoint_at": self.last_checkpoint_at,
            "metadata": self._metadata.model_dump(mode="json"),
            "raw_sha256": self._metadata.raw_csv_sha256,
            "updated_at": _utc_now(),
        }

    def _write_journal(self) -> None:
        # Only replace this writer's own journal; no user artifact is overwritten.
        # An incomplete checkpoint gets one separately reserved snapshot after
        # this writer's failed update. Keep the original failure bytes intact.
        update_path = (self._journal_recovery_path
                       if self._status == "incomplete" and self._journal_update_failed
                       else self._journal_update_path)
        created = False
        try:
            with update_path.open("x", encoding="utf-8", newline="\n") as stream:
                created = True
                json.dump(self._journal(), stream, indent=2, allow_nan=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            _replace_journal_with_retry(update_path, self._journal_path)
            _sync_directory(self.output_dir)
        except BaseException:
            # A pending update is retained as evidence if sync or replacement fails.
            if created and update_path == self._journal_update_path and update_path.exists():
                self._journal_update_failed = True
            raise

    def _best_effort_journal(self) -> None:
        try:
            self._write_journal()
        except (OSError, ValueError):
            pass

    def append_event(self, event: DerivedEvent) -> None:
        self._require_open()
        self._raw_writer.writerow(raw_csv_row(event))
        self.row_count += 1
        self._status = "recording"
        self.checkpoint()

    def log_protocol(self, direction: str, raw: bytes) -> None:
        self._require_open()
        entry = {"kind": "protocol", "at": _utc_now(), "direction": direction,
                 "raw_base64": base64.b64encode(raw).decode("ascii"),
                 "text": raw.decode("utf-8", errors="replace")}
        self._streams["protocol"].write(json.dumps(entry, ensure_ascii=True) + "\n")

    def log_action(self, action: str, details: dict, elapsed_s: float = 0) -> None:
        self._require_open()
        entry = {"kind": "action", "at": _utc_now(), "elapsed_s": elapsed_s,
                 "action": action, "details": details}
        rendered = json.dumps(entry, ensure_ascii=True, allow_nan=False)
        if self.source == "simulation":
            self._simulation["actions"].append(json.loads(rendered))
        self._streams["protocol"].write(rendered + "\n")
        self._sidecars_dirty = True

    def update_metadata(self, metadata: TrialMetadata) -> None:
        self._require_open()
        validated = TrialMetadata.model_validate(metadata.model_dump())
        immutable = ("trial_uuid", "session_id", "species_code", "trial_type", "trial_number",
                     "well_id", "attempt", "arm_radius_m", "min_event_interval_us",
                     "created_at_local_iso8601", "planned_duration_s", "device_id",
                     "firmware_version", "serial_port", "baud_rate", "application_version",
                     "exact_esp32s3_dev_board_model", "hardware_revision")
        if any(getattr(validated, field) != getattr(self._metadata, field) for field in immutable):
            raise ValueError("Armed identity, filename and measurement settings cannot change")
        self._metadata = validated
        self._sidecars_dirty = True

    def _write_sidecars(self) -> None:
        if not self._sidecars_dirty:
            return
        stream = self._streams["metadata"]
        stream.seek(0)
        stream.truncate()
        csv.writer(stream, lineterminator="\n").writerows(self._metadata.field_value_rows())
        if self.source == "simulation":
            stream = self._streams["simulation"]
            stream.seek(0)
            stream.truncate()
            json.dump(self._simulation, stream, ensure_ascii=True, indent=2, allow_nan=False)
            stream.write("\n")
        self._sidecars_dirty = False

    def checkpoint(self, force: bool = False) -> None:
        self._require_open()
        now = time.monotonic()
        sync = force or now - self._last_sync >= self.sync_interval_s
        if not (sync or now - self._last_flush >= self.flush_interval_s):
            return
        self._write_sidecars()
        for stream in self._streams.values():
            stream.flush()
        self._last_flush = now
        if sync:
            for stream in self._streams.values():
                os.fsync(stream.fileno())
            self.durable_row_count = self.row_count
            self.last_checkpoint_at = _utc_now()
            self._write_journal()
            self._last_sync = now

    def raw_sha256(self) -> str:
        if not self._closed:
            self._streams["raw"].flush()
        path = self._partial["raw"]
        if not path.exists():
            path = self._final["raw"]
        return _sha256(path)

    def finalize(self, metadata: TrialMetadata) -> list[dict[str, str]]:
        self._require_open()
        metadata = TrialMetadata.model_validate(metadata.model_dump())
        if metadata.incomplete:
            raise ValueError("Use abort() to retain an incomplete trial")
        actual_hash = self.raw_sha256()
        if metadata.raw_csv_sha256 != actual_hash:
            raise ValueError("Raw CSV SHA-256 does not match complete metadata")
        if metadata.accepted_event_count != self.row_count:
            raise ValueError("Complete trial event count does not match persisted rows")
        self.update_metadata(metadata)
        self._status = "finalizing"
        try:
            self.checkpoint(force=True)
            self._close_streams()
            for kind, source in self._partial.items():
                _rename_no_replace(source, self._final[kind])
                self._promoted.add(kind)
                self._write_journal()
            _sync_directory(self.output_dir)
            self._status = "complete"
            self._write_journal()  # The commit marker is always written last.
        except BaseException:
            self._status = "incomplete"
            self._reason = "finalization_failed"
            self._mark_metadata_incomplete()
            self._best_effort_journal()
            self._close_streams()
            raise
        return self.files()

    def _mark_metadata_incomplete(self) -> None:
        """Best-effort downgrade after failure, even after metadata promotion.

        The journal remains authoritative when the storage error also prevents
        rewriting the sidecar. Never touch a final destination we did not create.
        """
        values = self._metadata.model_dump()
        values["incomplete"] = True
        self._metadata = TrialMetadata.model_validate(values)
        stream = self._streams.get("metadata")
        opened_here = False
        try:
            if stream is None or stream.closed:
                path = self._partial["metadata"]
                if not path.exists():
                    if "metadata" not in self._promoted:
                        return
                    path = self._final["metadata"]
                if path.is_symlink() or _read_metadata(path).trial_uuid != self._metadata.trial_uuid:
                    return
                stream = path.open("r+", encoding="utf-8", newline="")
                opened_here = True
            stream.seek(0)
            stream.truncate()
            csv.writer(stream, lineterminator="\n").writerows(self._metadata.field_value_rows())
            stream.flush()
            os.fsync(stream.fileno())
        except (OSError, ValueError):
            pass
        finally:
            if opened_here and stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

    def abort(self, metadata: TrialMetadata) -> list[dict[str, str]]:
        self._require_open()
        values = metadata.model_dump()
        values.update(incomplete=True, raw_csv_sha256=self.raw_sha256())
        self.update_metadata(TrialMetadata.model_validate(values))
        self._status = "incomplete"
        self._reason = metadata.stop_reason or "trial_aborted"
        try:
            self.log_action("incomplete", {"reason": self._reason},
                            metadata.actual_duration_s or 0)
            self.checkpoint(force=True)
        finally:
            self._close_streams()
        return self.files()

    def files(self) -> list[dict[str, str]]:
        return _artifact_files(self.output_dir, self.name.base)

    def _close_streams(self) -> None:
        first_error = None
        for stream in self._streams.values():
            if not stream.closed:
                try:
                    stream.close()
                except OSError as exc:
                    first_error = first_error or exc
        self._closed = True
        if first_error is not None:
            raise first_error

    def close(self) -> None:
        if self._closed:
            return
        if self._status not in {"complete", "incomplete"}:
            self._status = "incomplete"
            self._reason = "writer_closed_before_finalization"
        try:
            self.checkpoint(force=True)
        finally:
            self._close_streams()


def _read_metadata(path: Path) -> TrialMetadata:
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.reader(stream)
        if next(reader, None) != ["field", "value"]:
            raise ValueError("Invalid metadata CSV header")
        values = {field: value if value else None for field, value in reader}
    return TrialMetadata.model_validate(values)


def discover_trials(output_dir: Path) -> list[dict[str, Any]]:
    """Inspect registered bundles and orphan partials, never modifying data.

    Call at startup / review refresh. Even a valid complete metadata CSV is
    insufficient without the final journal commit and a verified raw checksum.
    All artifact paths come from the selected root and fixed filename suffixes,
    never path values stored in an on-disk journal.
    """
    directory = Path(output_dir).expanduser().resolve()
    if not directory.is_dir():
        return []
    bases = {path.name.removesuffix("_journal.json")
             for path in directory.glob("*_journal.json")}
    for kind, suffixes in _SUFFIXES.items():
        for suffix in suffixes:
            if kind == "raw" and suffix == ".csv":
                continue  # Legacy CSVs without a companion are not new trial bundles.
            bases.update(path.name.removesuffix(suffix) for path in directory.glob(f"*{suffix}")
                         if not (kind == "raw" and path.name.endswith("_metadata.partial.csv")))
    # Keep only bases owning a journal or raw file, including orphan raw partials.
    bases = {base for base in bases if any((directory / f"{base}{suffix}").is_file()
             for suffix in ("_journal.json", ".csv", ".partial.csv"))}
    trials = []
    for base in sorted(bases):
        files = _artifact_files(directory, base)
        by_kind = {entry["kind"]: Path(entry["path"]) for entry in files}
        journal: dict[str, Any] = {}
        issues: list[str] = []
        try:
            journal = json.loads(by_kind["journal"].read_text(encoding="utf-8"))
            if not isinstance(journal, dict) or journal.get("journal_schema_version") != 1:
                raise ValueError("Unsupported journal")
        except (OSError, ValueError, KeyError):
            journal = {}
            issues.append("Recovery journal is missing or unreadable")
        metadata: TrialMetadata | None = None
        try:
            metadata = _read_metadata(by_kind["metadata"])
        except (OSError, ValueError, KeyError):
            issues.append("Metadata sidecar is missing or invalid")
            try:
                metadata = TrialMetadata.model_validate(journal.get("metadata", {}))
            except ValueError:
                pass
        simulation: dict[str, Any] = {}
        try:
            simulation = json.loads(by_kind["simulation"].read_text(encoding="utf-8"))
            if not isinstance(simulation, dict) or simulation.get("acquisition_mode") != "simulation":
                raise ValueError("Invalid simulation manifest")
        except (OSError, ValueError, KeyError):
            simulation = {}
        source = journal.get("acquisition_mode")
        if source is None and simulation:
            source = "simulation"  # Historical manifest is positive provenance.
        if source not in {"simulation", "serial"}:
            source = "unknown"
            issues.append("Acquisition provenance is unknown")
        if source == "simulation" and not simulation:
            issues.append("Simulation provenance is missing or invalid")
        if source == "serial" and "simulation" in by_kind:
            issues.append("Serial trial unexpectedly contains simulation provenance")
        required = ["raw", "metadata", "protocol"] + (["simulation"] if source == "simulation" else [])
        if "acquisition_mode" in journal and journal.get("required_artifacts") != required:
            issues.append("Journal artifact requirements do not match acquisition source")
        count = 0
        try:
            with by_kind["raw"].open(encoding="utf-8", newline="") as stream:
                reader = csv.reader(stream)
                if next(reader, None) != list(RAW_CSV_HEADER):
                    raise ValueError("Invalid raw CSV header")
                for row in reader:
                    if len(row) != len(RAW_CSV_HEADER):
                        raise ValueError("Truncated or malformed raw CSV row")
                    count += 1
        except (OSError, ValueError, KeyError):
            issues.append("Raw CSV is missing, invalid, or has an interrupted final row")
        committed = journal.get("status") == "complete"
        if not committed:
            issues.append(str(journal.get("reason") or "Recording was not committed"))
        if committed:
            for kind in required:
                suffixes = _SUFFIXES[kind]
                if by_kind.get(kind) != directory / f"{base}{suffixes[0]}":
                    issues.append(f"Final {kind} artifact is missing")
            if metadata is None or metadata.incomplete:
                issues.append("Complete metadata is unavailable")
            else:
                if count != metadata.accepted_event_count or count != journal.get("row_count"):
                    issues.append("Persisted event count does not match committed metadata")
                try:
                    if _sha256(by_kind["raw"]) != metadata.raw_csv_sha256:
                        issues.append("Raw CSV checksum does not match metadata")
                except (OSError, KeyError):
                    issues.append("Raw CSV checksum cannot be verified")
                if journal.get("raw_sha256") != metadata.raw_csv_sha256:
                    issues.append("Journal checksum does not match metadata")
                if journal.get("trial_uuid") != str(metadata.trial_uuid):
                    issues.append("Journal trial identity does not match metadata")
                if journal.get("metadata") != metadata.model_dump(mode="json"):
                    issues.append("Metadata differs from the committed journal snapshot")
                if source == "simulation" and simulation.get("trial_uuid") != str(metadata.trial_uuid):
                    issues.append("Simulation trial identity does not match metadata")
        complete = committed and not issues
        values = metadata.model_dump(mode="json") if metadata else {}
        trials.append({
            "id": str(metadata.trial_uuid) if metadata else str(journal.get("trial_uuid") or base),
            "base": base,
            "status": "complete" if complete else "incomplete",
            "incomplete": not complete,
            "metadata": values,
            "simulation": simulation,
            "acquisition_mode": source,
            "row_count": count,
            "reason": "; ".join(issues) if issues else None,
            "files": files,
        })
    return sorted(trials, key=lambda item: item["metadata"].get("created_at_local_iso8601", ""),
                  reverse=True)
