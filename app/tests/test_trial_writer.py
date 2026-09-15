from __future__ import annotations

import base64
import csv
import hashlib
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from flightmill.acquisition.calculations import SourceEvent, circumference_m, derive_event
from flightmill.constants import RAW_CSV_HEADER
from flightmill.hardware import DEV_BOARD_MODEL
from flightmill.storage import trial_writer
from flightmill.storage.metadata import TrialMetadata
from flightmill.storage.naming import FilenameCollisionError, TrialName
from flightmill.storage.raw_csv import write_raw_csv
from flightmill.storage.trial_writer import TrialWriter, discover_trials


NAME = TrialName("Mrot", "prelim", 2, "A6", 1)


def initial_metadata() -> TrialMetadata:
    session = uuid4()
    return TrialMetadata(
        trial_uuid=session, session_id=session,
        created_at_local_iso8601=datetime(2026, 9, 8, 10, tzinfo=timezone.utc),
        species_code=NAME.species_code, trial_type=NAME.trial_type,
        trial_number=NAME.trial_number, well_id=NAME.well_id, attempt=NAME.attempt,
        circumference_m=circumference_m(0.10), device_id="SIM-FM-0001",
        exact_esp32s3_dev_board_model=DEV_BOARD_MODEL, firmware_version="sim-0.1.0",
        application_version="0.1.0-dev", serial_port="SIM", baud_rate=115200,
    )


def complete_metadata(writer: TrialWriter, metadata: TrialMetadata, duration: float = 60) -> TrialMetadata:
    values = metadata.model_dump()
    values.update(
        started_at_local_iso8601=metadata.created_at_local_iso8601,
        stopped_at_local_iso8601=metadata.created_at_local_iso8601 + timedelta(seconds=duration),
        actual_duration_s=duration, stop_reason="user_stop", incomplete=False,
        accepted_event_count=writer.row_count, raw_csv_sha256=writer.raw_sha256(),
    )
    return TrialMetadata.model_validate(values)


class TrialWriterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.metadata = initial_metadata()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def create(self) -> TrialWriter:
        return TrialWriter.create(self.directory, NAME, self.metadata,
                                  {"scenario": "steady", "seed": 73})

    def test_streamed_csv_is_identical_to_legacy_serializer_and_bundle_commits_last(self) -> None:
        writer = self.create()
        events = [derive_event(SourceEvent(n, n * 1_000_000, 0 if n == 1 else 1_000_000, 0), 0.10)
                  for n in range(1, 21)]
        for event in events:
            writer.append_event(event)
        evidence = b"invalid\xff\r\nfragment"
        writer.log_protocol("rx", evidence)
        writer.log_action("note", {"text": "Synthetic bee begins rest"}, 20.0)
        files = writer.finalize(complete_metadata(writer, self.metadata))
        golden = self.directory / "expected.csv"
        write_raw_csv(golden, events)
        raw = self.directory / NAME.raw_filename
        self.assertEqual(raw.read_bytes(), golden.read_bytes())
        self.assertEqual(len(files), 5)
        self.assertFalse(any(self.directory.glob("*.partial.*")))
        journal = json.loads((self.directory / f"{NAME.base}_journal.json").read_text())
        self.assertEqual(journal["status"], "complete")
        self.assertEqual(journal["raw_sha256"], hashlib.sha256(raw.read_bytes()).hexdigest())
        rows = list(csv.reader(raw.read_text().splitlines()))
        self.assertEqual(rows[1][7], "")
        self.assertEqual(rows[2][7], "0.6283185307")
        self.assertEqual(rows[-1][6], "12.5663706144")
        protocol = [json.loads(line) for line in
                    (self.directory / f"{NAME.base}_protocol.jsonl").read_text().splitlines()]
        captured = next(entry for entry in protocol if entry["kind"] == "protocol")
        self.assertEqual(base64.b64decode(captured["raw_base64"]), evidence)
        trials = discover_trials(self.directory)
        self.assertEqual(len(trials), 1)
        self.assertFalse(trials[0]["incomplete"])
        self.assertEqual(trials[0]["row_count"], 20)
        self.assertEqual(trials[0]["simulation"]["actions"][-1]["elapsed_s"], 20)

    def test_zero_event_trial_keeps_elapsed_duration_and_header_only_csv(self) -> None:
        writer = self.create()
        writer.finalize(complete_metadata(writer, self.metadata, 60))
        self.assertEqual((self.directory / NAME.raw_filename).read_bytes(),
                         (",".join(RAW_CSV_HEADER) + "\n").encode())
        result = discover_trials(self.directory)[0]
        self.assertEqual(result["metadata"]["actual_duration_s"], 60)
        self.assertEqual(result["status"], "complete")

    def test_every_companion_and_reservation_blocks_reuse_without_overwriting(self) -> None:
        suffixes = [suffix for pair in trial_writer._SUFFIXES.values() for suffix in pair]
        suffixes += ["_journal.json", "_journal.update.json"]
        for suffix in suffixes:
            with self.subTest(suffix=suffix):
                path = self.directory / f"{NAME.base}{suffix}"
                path.write_bytes(b"existing data")
                with self.assertRaises(FilenameCollisionError):
                    self.create()
                self.assertEqual(path.read_bytes(), b"existing data")
                self.assertEqual(len(list(self.directory.iterdir())), 1)
                path.unlink()
        writer = self.create()
        try:
            with self.assertRaises(FilenameCollisionError):
                self.create()
        finally:
            writer.close()

    def test_abort_retains_partials_and_read_only_recovery_reports_reason(self) -> None:
        writer = self.create()
        writer.append_event(derive_event(SourceEvent(1, 1_000_000, 0, 0), 0.10))
        incomplete = self.metadata.model_copy(update={"stop_reason": "device_disconnected",
                                                     "accepted_event_count": 1})
        writer.abort(incomplete)
        before = {path.name: path.read_bytes() for path in self.directory.iterdir()}
        trial = discover_trials(self.directory)[0]
        self.assertEqual(trial["status"], "incomplete")
        self.assertIn("device_disconnected", trial["reason"])
        self.assertEqual(trial["row_count"], 1)
        self.assertIsNone(trial["metadata"]["stopped_at_local_iso8601"])
        self.assertTrue((self.directory / f"{NAME.base}.partial.csv").exists())
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.directory.iterdir()})

    def test_interrupted_rename_never_reports_complete_and_keeps_all_artifacts(self) -> None:
        writer = self.create()
        original = trial_writer._rename_no_replace
        calls = 0

        def interrupted(source: Path, destination: Path) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated power interruption during promotion")
            original(source, destination)

        with patch.object(trial_writer, "_rename_no_replace", side_effect=interrupted):
            with self.assertRaises(OSError):
                writer.finalize(complete_metadata(writer, self.metadata))
        self.assertTrue((self.directory / NAME.raw_filename).exists())
        self.assertTrue((self.directory / f"{NAME.base}_metadata.partial.csv").exists())
        recovered = discover_trials(self.directory)[0]
        self.assertEqual(recovered["status"], "incomplete")
        self.assertIn("finalization_failed", recovered["reason"])
        self.assertEqual(len(recovered["files"]), 5)
        self.assertTrue(recovered["metadata"]["incomplete"])

    def test_external_final_file_created_after_arm_is_never_overwritten(self) -> None:
        writer = self.create()
        destination = self.directory / NAME.raw_filename
        destination.write_bytes(b"another program's data")
        with self.assertRaises(FileExistsError):
            writer.finalize(complete_metadata(writer, self.metadata))
        self.assertEqual(destination.read_bytes(), b"another program's data")
        self.assertTrue((self.directory / f"{NAME.base}.partial.csv").exists())
        trial = discover_trials(self.directory)[0]
        self.assertEqual(trial["status"], "incomplete")
        registered_raw = next(entry for entry in trial["files"] if entry["kind"] == "raw")
        self.assertEqual(registered_raw["name"], f"{NAME.base}.partial.csv")

    def test_bad_checksum_or_count_cannot_commit(self) -> None:
        writer = self.create()
        final = complete_metadata(writer, self.metadata)
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            writer.finalize(final.model_copy(update={"raw_csv_sha256": "0" * 64}))
        with self.assertRaisesRegex(ValueError, "count"):
            writer.finalize(final.model_copy(update={"accepted_event_count": 5}))
        writer.close()
        self.assertEqual(discover_trials(self.directory)[0]["status"], "incomplete")

    def test_checksum_revalidation_detects_modified_complete_csv(self) -> None:
        writer = self.create()
        writer.finalize(complete_metadata(writer, self.metadata))
        with (self.directory / NAME.raw_filename).open("ab") as stream:
            stream.write(b"1,1000.000,1.000000,0.000,0.000000,1,0.6283185307,,0\n")
        trial = discover_trials(self.directory)[0]
        self.assertEqual(trial["status"], "incomplete")
        self.assertIn("checksum", trial["reason"])

    def test_sync_failure_does_not_advance_durable_count_or_commit(self) -> None:
        writer = self.create()
        writer.append_event(derive_event(SourceEvent(1, 1_000_000, 0, 0), 0.10))
        with patch.object(trial_writer.os, "fsync", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                writer.checkpoint(force=True)
        self.assertEqual(writer.durable_row_count, 0)
        writer._close_streams()  # Model abrupt loss: no orderly final journal transaction.
        trial = discover_trials(self.directory)[0]
        self.assertEqual(trial["status"], "incomplete")
        self.assertEqual(trial["row_count"], 1)

    def test_orphan_partial_is_reported_without_inventing_metadata_or_stop(self) -> None:
        partial = self.directory / f"{NAME.base}.partial.csv"
        write_raw_csv(partial, [])
        trials = discover_trials(self.directory)
        self.assertEqual(len(trials), 1)
        self.assertEqual(trials[0]["status"], "incomplete")
        self.assertEqual(trials[0]["metadata"], {})
        self.assertIn("journal", trials[0]["reason"])

    def test_all_files_promoted_without_commit_marker_are_still_incomplete(self) -> None:
        writer = self.create()
        original = writer._write_journal

        def reject_commit() -> None:
            if writer._status == "complete":
                raise OSError("commit marker failed")
            original()

        with patch.object(writer, "_write_journal", side_effect=reject_commit):
            with self.assertRaisesRegex(OSError, "commit marker"):
                writer.finalize(complete_metadata(writer, self.metadata))
        self.assertFalse(any(self.directory.glob("*.partial.*")))
        result = discover_trials(self.directory)[0]
        self.assertEqual(result["status"], "incomplete")
        self.assertTrue(result["metadata"]["incomplete"])

    def test_checkpoint_flushes_silent_trial_and_preserves_durability_boundary(self) -> None:
        with patch.object(trial_writer.time, "monotonic", return_value=0):
            writer = self.create()
        writer.log_action("note", {"text": "No pulses"}, 0.1)
        with patch.object(trial_writer.time, "monotonic", return_value=0.25):
            writer.checkpoint()
        manifest = self.directory / f"{NAME.base}_simulation.partial.json"
        self.assertEqual(json.loads(manifest.read_text())["actions"][-1]["action"], "note")
        with patch.object(trial_writer.time, "monotonic", return_value=1.0):
            writer.checkpoint()
        journal = json.loads((self.directory / f"{NAME.base}_journal.json").read_text())
        self.assertIsNotNone(journal["last_checkpoint_at"])
        self.assertEqual(journal["durable_row_count"], 0)
        writer.close()

    def test_service_profile_is_preserved_as_manifest_scenario(self) -> None:
        writer = TrialWriter.create(self.directory, NAME, self.metadata,
                                    {"profile": "zero", "seed": 42})
        writer.finalize(complete_metadata(writer, self.metadata))
        manifest = discover_trials(self.directory)[0]["simulation"]
        self.assertEqual(manifest["scenario"], "zero")
        self.assertEqual(manifest["profile"], "zero")

    def test_transient_windows_journal_denial_retries_the_same_synced_source(self) -> None:
        writer = self.create()
        original_replace = trial_writer.os.replace
        observed = []

        def briefly_locked(source: Path, destination: Path) -> None:
            observed.append((source, destination, source.read_bytes()))
            if len(observed) < 3:
                failure = PermissionError("transient Windows read handle")
                failure.winerror = 5
                raise failure
            original_replace(source, destination)

        with patch.object(trial_writer.os, "replace", side_effect=briefly_locked), \
                patch.object(trial_writer.time, "sleep") as sleep:
            writer.checkpoint(force=True)
        self.assertEqual(len(observed), 3)
        self.assertEqual(observed[0], observed[1])
        self.assertEqual(observed[1], observed[2])
        self.assertEqual(sleep.call_count, 2)
        self.assertFalse((self.directory / f"{NAME.base}_journal.update.json").exists())
        writer.finalize(complete_metadata(writer, self.metadata))
        self.assertEqual(discover_trials(self.directory)[0]["status"], "complete")

    def test_persistent_windows_journal_denial_exhausts_budget_and_preserves_evidence(self) -> None:
        writer = self.create()
        failure = PermissionError("persistent Windows access denial")
        failure.winerror = 5
        with patch.object(trial_writer.os, "replace", side_effect=failure) as replace, \
                patch.object(trial_writer.time, "sleep") as sleep:
            with self.assertRaises(PermissionError):
                writer.checkpoint(force=True)
        self.assertEqual(replace.call_count, 6)
        self.assertEqual(sleep.call_count, 5)
        self.assertAlmostEqual(sum(call.args[0] for call in sleep.call_args_list), 0.25)
        writer._close_streams()
        trial = discover_trials(self.directory)[0]
        self.assertEqual(trial["status"], "incomplete")
        self.assertTrue((self.directory / f"{NAME.base}_journal.update.json").exists())

    def test_non_windows_replace_error_is_not_retried(self) -> None:
        writer = self.create()
        with patch.object(trial_writer.os, "replace", side_effect=OSError("disk full")) as replace, \
                patch.object(trial_writer.time, "sleep") as sleep:
            with self.assertRaises(OSError):
                writer.checkpoint(force=True)
        self.assertEqual(replace.call_count, 1)
        sleep.assert_not_called()
        writer._close_streams()

    def test_failed_journal_update_does_not_block_incomplete_recovery_checkpoint(self) -> None:
        writer = self.create()
        writer.append_event(derive_event(SourceEvent(1, 1_000_000, 0, 0), 0.10))
        failure = PermissionError("SOFTWARE ONLY exhausted journal replacement")
        failure.winerror = 5
        with patch.object(trial_writer, "_replace_journal_with_retry", side_effect=failure):
            with self.assertRaises(PermissionError):
                writer.checkpoint(force=True)
        pending = self.directory / f"{NAME.base}_journal.update.json"
        failed_snapshot = pending.read_bytes()
        incomplete = self.metadata.model_copy(update={"stop_reason": "storage_failure",
                                                      "accepted_event_count": 1})
        files = writer.abort(incomplete)
        journal = json.loads((self.directory / f"{NAME.base}_journal.json").read_bytes())
        self.assertEqual(journal["status"], "incomplete")
        self.assertEqual(journal["reason"], "storage_failure")
        self.assertEqual(journal["row_count"], 1)
        self.assertEqual(journal["durable_row_count"], 1)
        self.assertEqual(journal["raw_sha256"], writer.raw_sha256())
        self.assertEqual(pending.read_bytes(), failed_snapshot)
        self.assertIn("journal_pending", {entry["kind"] for entry in files})
        self.assertEqual(discover_trials(self.directory)[0]["status"], "incomplete")

    def test_persistent_denial_retains_both_failed_and_incomplete_snapshots(self) -> None:
        writer = self.create()
        writer.append_event(derive_event(SourceEvent(1, 1_000_000, 0, 0), 0.10))
        failure = PermissionError("SOFTWARE ONLY persistent journal denial")
        failure.winerror = 5
        pending = self.directory / f"{NAME.base}_journal.update.json"
        with patch.object(trial_writer, "_replace_journal_with_retry", side_effect=failure):
            with self.assertRaises(PermissionError):
                writer.checkpoint(force=True)
            failed_snapshot = pending.read_bytes()
            with self.assertRaises(PermissionError):
                writer.abort(self.metadata.model_copy(update={"stop_reason": "storage_failure",
                                                             "accepted_event_count": 1}))
        self.assertEqual(pending.read_bytes(), failed_snapshot)
        recovery = self.directory / f"{NAME.base}_journal.recovery.json"
        snapshot = json.loads(recovery.read_bytes())
        self.assertEqual(snapshot["status"], "incomplete")
        self.assertEqual(snapshot["reason"], "storage_failure")
        self.assertEqual(snapshot["row_count"], 1)
        self.assertEqual(snapshot["raw_sha256"], writer.raw_sha256())
        self.assertIn("journal_recovery", {entry["kind"] for entry in writer.files()})
        self.assertEqual(discover_trials(self.directory)[0]["status"], "incomplete")

    def test_existing_recovery_companion_blocks_basename_reservation(self) -> None:
        recovery = self.directory / f"{NAME.base}_journal.recovery.json"
        recovery.write_bytes(b"existing evidence must not be replaced")
        writer = None
        try:
            with self.assertRaises(FilenameCollisionError):
                writer = self.create()
        finally:
            if writer is not None:
                writer.close()
        self.assertEqual(recovery.read_bytes(), b"existing evidence must not be replaced")
        self.assertFalse((self.directory / f"{NAME.base}_journal.json").exists())

    @unittest.skipUnless(os.name == "nt", "Windows file-sharing behavior")
    def test_real_windows_reader_releases_journal_during_bounded_retry(self) -> None:
        writer = self.create()
        held = (self.directory / f"{NAME.base}_journal.json").open("r")
        release = threading.Timer(0.05, held.close)
        release.start()
        try:
            writer.checkpoint(force=True)
        finally:
            release.join()
            held.close()
        writer.finalize(complete_metadata(writer, self.metadata))
        self.assertEqual(discover_trials(self.directory)[0]["status"], "complete")

    @unittest.skipUnless(os.name == "nt", "Windows atomic no-clobber rename")
    def test_final_promotion_recovers_transient_windows_sharing_without_changing_bytes(self) -> None:
        writer = self.create()
        writer.append_event(derive_event(SourceEvent(1, 1_000_000, 0, 0), 0.10))
        final = complete_metadata(writer, self.metadata)
        original_rename = trial_writer.os.rename
        raw_attempts = []

        def transient_reader(source: Path, destination: Path) -> None:
            if source.name == f"{NAME.base}.partial.csv":
                raw_attempts.append((source, destination, source.read_bytes()))
                if len(raw_attempts) < 3:
                    failure = PermissionError("transient Windows source reader")
                    failure.winerror = 32
                    raise failure
            original_rename(source, destination)

        with patch.object(trial_writer.os, "rename", side_effect=transient_reader), \
                patch.object(trial_writer.time, "sleep") as sleep:
            writer.finalize(final)
        self.assertEqual(len(raw_attempts), 3)
        self.assertEqual(raw_attempts[0], raw_attempts[1])
        self.assertEqual(raw_attempts[1], raw_attempts[2])
        self.assertGreaterEqual(sleep.call_count, 2)
        trial = discover_trials(self.directory)[0]
        self.assertEqual(trial["status"], "complete")
        self.assertEqual(trial["row_count"], 1)

    @unittest.skipUnless(os.name == "nt", "Windows atomic no-clobber rename")
    def test_persistent_final_sharing_failure_retains_incomplete_bundle(self) -> None:
        writer = self.create()
        final = complete_metadata(writer, self.metadata)
        failure = PermissionError("persistent Windows source reader")
        failure.winerror = 33
        with patch.object(trial_writer.os, "rename", side_effect=failure) as rename, \
                patch.object(trial_writer.time, "sleep"):
            with self.assertRaises(PermissionError):
                writer.finalize(final)
        self.assertEqual(rename.call_count, 6)
        trial = discover_trials(self.directory)[0]
        self.assertEqual(trial["status"], "incomplete")
        self.assertTrue(trial["metadata"]["incomplete"])
        self.assertTrue((self.directory / f"{NAME.base}.partial.csv").exists())

    @unittest.skipUnless(os.name == "nt", "Windows atomic no-clobber rename")
    def test_final_destination_collision_is_never_retried(self) -> None:
        writer = self.create()
        final = complete_metadata(writer, self.metadata)
        destination = self.directory / NAME.raw_filename
        destination.write_bytes(b"existing external trial")
        original_rename = trial_writer.os.rename
        with patch.object(trial_writer.os, "rename", wraps=original_rename) as rename, \
                patch.object(trial_writer.time, "sleep"):
            with self.assertRaises(FileExistsError):
                writer.finalize(final)
        self.assertEqual(rename.call_count, 1)
        self.assertEqual(destination.read_bytes(), b"existing external trial")
        self.assertTrue((self.directory / f"{NAME.base}.partial.csv").exists())


if __name__ == "__main__":
    unittest.main()
