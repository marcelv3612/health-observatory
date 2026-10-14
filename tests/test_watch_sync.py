"""Transactional local scaffold tests with invented sample UUIDs and values only."""
from __future__ import annotations

import copy
import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from test_watch_metrics import batch, query, sample, uid
from watch_metrics import ContractError, timestamp
from watch_sync import MAX_RETRY_ATTEMPTS, ResourceLimit, SyncConflict, WatchStore, retry_delay


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name) / "watch"
        self.now = timestamp("2031-03-11T12:00:00Z").timestamp()
        self.store = WatchStore(self.directory, clock=lambda: self.now)

    def tearDown(self):
        self.temp.cleanup()

    def rows(self, table):
        with closing(self.store._connect()) as connection:
            return [dict(r) for r in connection.execute("SELECT * FROM " + table)]

    def test_uninitialized_store_never_injects_demo_or_calls_empty_healthy(self):
        dashboard = self.store.dashboard()
        self.assertEqual(dashboard["mode"], "empty"); self.assertEqual(dashboard["metrics"], [])
        self.assertIsNone(dashboard["sleep"]["summary"]["total_minutes"])
        self.assertIsNone(dashboard["sync"]["last_success"])
        self.assertFalse(dashboard["sync"]["destination"]["enabled"])
        self.assertEqual(dashboard["coverage"], "unknown")
        self.assertEqual(dashboard["read_states"], [])

    def test_ingest_anchor_sample_receipt_and_minimized_outbox_are_durable(self):
        result = self.store.ingest(batch())
        self.assertEqual(result["upserted"], 1); self.assertTrue(result["complete"])
        self.assertEqual(self.rows("queries")[0]["anchor"], "anchor-1")
        self.assertEqual(len(self.rows("samples")), 1); self.assertEqual(len(self.rows("batches")), 1)
        manifest = self.store.pending_outbox()[0]["manifest"]
        self.assertEqual(manifest["upserts"][0]["id"], uid("sample"))
        self.assertEqual(set(manifest["upserts"][0]), {"id", "type", "revision"})
        self.assertNotIn("value", json.dumps(manifest)); self.assertNotIn("anchor-1", json.dumps(manifest))
        reopened = WatchStore(self.directory, clock=lambda: self.now)
        self.assertEqual(reopened.status()["last_success"], result["committed_at"])
        self.assertEqual(reopened.dashboard()["metrics"][0]["value"], 67)

    def test_replay_is_idempotent_and_reusing_batch_uuid_with_changes_conflicts(self):
        original = batch()
        self.store.ingest(original)
        self.now += 60
        replay = self.store.ingest(original)
        self.assertTrue(replay["replayed"])
        self.assertEqual(len(self.rows("outbox")), 1)
        bad = copy.deepcopy(original); bad["types"][0]["samples"][0]["value"] = 80
        with self.assertRaises(SyncConflict): self.store.ingest(bad)
        self.assertEqual(self.store.dashboard()["metrics"][0]["value"], 67)
        self.assertEqual(self.store.status()["last_error"], "sync_conflict")

    def test_concurrent_same_batch_commits_once_and_all_other_calls_replay(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            responses = list(pool.map(self.store.ingest, [batch()] * 6))
        self.assertEqual(sum(not r["replayed"] for r in responses), 1)
        self.assertEqual(len(self.rows("samples")), 1); self.assertEqual(len(self.rows("outbox")), 1)

    def test_stale_anchor_in_later_type_rolls_back_all_changes(self):
        self.store.ingest(batch())
        multi = batch([query("step_count", [sample("step_count", 100, label="steps")]),
                       query(samples=[sample(value=80)], previous="wrong", next_anchor="a2")], label="atomic")
        with self.assertRaises(SyncConflict): self.store.ingest(multi)
        self.assertEqual(len(self.rows("queries")), 1)
        self.assertEqual(len(self.rows("batches")), 1); self.assertEqual(len(self.rows("outbox")), 1)
        self.assertEqual(self.store.dashboard()["metrics"][0]["value"], 67)

    def test_invalid_later_sample_is_atomic_and_does_not_create_mode(self):
        bad = sample("step_count", 12, label="steps"); bad["unit"] = "kilograms"
        with self.assertRaises(ContractError): self.store.ingest(batch([query(), query("step_count", [bad])]))
        self.assertEqual(self.rows("samples"), []); self.assertEqual(self.rows("queries"), [])
        self.assertEqual(self.store.status()["mode"], "empty")
        self.assertEqual(self.store.status()["last_error"], "batch_rejected")

    def test_same_uuid_correction_and_deletes_including_unknown_are_idempotent(self):
        self.store.ingest(batch())
        self.store.ingest(batch([query(samples=[sample(value=80)], previous="anchor-1", next_anchor="a2")], label="correction"))
        self.assertEqual(len(self.rows("samples")), 1)
        self.assertEqual(self.store.dashboard()["metrics"][0]["value"], 80)
        deletion = batch([query(samples=[], previous="a2", next_anchor="a3", deleted=[uid("sample"), uid("never-present")])], label="deletion")
        self.store.ingest(deletion); self.store.ingest(deletion)
        self.assertEqual(self.rows("samples"), [])
        self.assertEqual(self.rows("queries")[0]["anchor"], "a3")
        self.assertEqual(len(self.store.pending_outbox()), 3)
        self.assertEqual(len(self.rows("identities")), 2)

    def test_scope_is_exact_and_deleted_uuid_cannot_change_provenance(self):
        self.store.ingest(batch())
        self.store.ingest(batch([query(samples=[], previous="anchor-1", next_anchor="a2", deleted=[uid("sample")])], label="delete"))
        with self.assertRaises(SyncConflict): self.store.ingest(batch(label="other-stream", stream="other-stream"))
        other = "com.example.synthetic.other"
        with self.assertRaises(SyncConflict):
            self.store.ingest(batch([query(samples=[sample(source_bundle=other)])], label="other-source", source_bundle=other))
        with self.assertRaises(SyncConflict):
            self.store.ingest(batch([query("step_count", [sample("step_count", 12)])], label="other-type"))

    def test_baseline_reset_and_incremental_without_baseline_are_refused(self):
        with self.assertRaises(SyncConflict):
            self.store.ingest(batch([query(previous="unknown", query_kind="incremental")]))
        self.store.ingest(batch())
        with self.assertRaises(SyncConflict): self.store.ingest(batch(label="reset"))
        self.assertEqual(self.rows("queries")[0]["anchor"], "anchor-1")

    def test_synthetic_device_mixing_is_refused_and_no_mode_conversion_route(self):
        self.store.ingest(batch())
        with self.assertRaises(SyncConflict): self.store.ingest(batch(label="device", mode="device"))
        self.assertEqual(self.store.status()["mode"], "synthetic")

    def test_empty_unknown_error_and_measured_zero_remain_distinct(self):
        unknown = query(samples=[], next_anchor=None, read_state="unknown")
        self.store.ingest(batch([unknown], label="unknown"))
        self.assertIsNone(self.store.status()["last_success"])
        self.assertEqual(self.store.status()["last_error"], "read_visibility_unknown")
        self.store.ingest(batch([query(samples=[], read_state="empty")], label="empty"))
        dashboard = self.store.dashboard()
        self.assertEqual(dashboard["read_states"][0]["read_state"], "empty")
        self.assertEqual(dashboard["read_states"][0]["coverage"], "unknown")
        self.assertEqual(dashboard["metrics"], [])
        success = self.store.status()["last_success"]
        self.now += 60
        failed = query(samples=[], previous="anchor-1", next_anchor="anchor-1", read_state="error", error="reader_unavailable")
        self.store.ingest(batch([failed], label="failed"))
        self.assertEqual(self.store.status()["last_success"], success)
        self.assertEqual(self.rows("queries")[0]["anchor"], "anchor-1")
        zero = query("step_count", [sample("step_count", 0, label="zero")])
        self.store.ingest(batch([zero], label="zero"))
        self.assertEqual(self.store.dashboard()["metrics"][0]["value"], 0)

    def test_partial_batch_does_not_claim_global_success_but_keeps_per_type_status(self):
        partial = batch([query(), query("step_count", [], next_anchor=None, read_state="error", error="reader_unavailable")])
        result = self.store.ingest(partial)
        self.assertFalse(result["complete"]); self.assertIsNone(self.store.status()["last_success"])
        states = {r["type"]: r for r in self.store.dashboard()["read_states"]}
        self.assertIsNotNone(states["heart_rate"]["last_success"])
        self.assertIsNone(states["step_count"]["last_success"])
        self.assertEqual(self.store.status()["last_error"], "read_error")

    def test_retry_only_records_local_intent_and_bounded_backoff_never_transports(self):
        self.store.ingest(batch())
        last_success = self.store.status()["last_success"]
        with self.assertRaises(SyncConflict): self.store.ingest(batch(label="stale"))
        for attempt in range(1, MAX_RETRY_ATTEMPTS + 1):
            self.store.record_outbox_failure(uid("batch"), "transport_unconnected")
            item = self.store.pending_outbox()[0]
            self.assertEqual(item["attempts"], attempt)
            self.assertEqual(timestamp(item["next_attempt"]).timestamp(), self.now + retry_delay(attempt))
            self.assertEqual(self.store.status()["due_outbox"], 0)
            self.now += retry_delay(attempt)
        self.assertEqual(self.store.status()["exhausted_outbox"], 1)
        before = self.rows("outbox")
        status = self.store.request_retry()
        self.assertIsNotNone(status["retry_requested"]); self.assertFalse(status["bridge_connected"])
        self.assertEqual(status["last_success"], last_success)
        self.assertEqual(status["last_error"], "sync_conflict")
        self.assertEqual(self.rows("outbox"), before)
        for code in ("private/path", "a\nvalue", "x" * 65):
            with self.assertRaises(ContractError): self.store.record_outbox_failure(uid("batch"), code)

    def test_retention_hides_and_prunes_old_values_preserving_anchor_and_identity(self):
        self.store.ingest(batch())
        self.now += 91 * 86400
        self.assertEqual(self.store.dashboard()["metrics"], [])
        self.assertEqual(self.store.status()["record_count"], 0)
        self.assertEqual(self.rows("samples"), [])
        self.store.ingest(batch([query(samples=[], previous="anchor-1", next_anchor="a2")], label="prune"))
        self.assertEqual(self.rows("samples"), [])
        self.assertEqual(len(self.rows("identities")), 1)
        self.assertEqual(self.rows("queries")[0]["anchor"], "a2")
        with self.assertRaises(ContractError): WatchStore(self.directory, retention_days=30)

    def test_physical_retention_on_idle_access_reopen_replay_rejected_import_and_retry(self):
        for operation in ("status", "dashboard", "reopen", "replay", "rejected", "retry", "pending", "queue_failure"):
            with self.subTest(operation=operation):
                directory = Path(self.temp.name) / operation
                now = timestamp("2031-03-11T12:00:00Z").timestamp()
                store = WatchStore(directory, clock=lambda: now)
                payload = batch([query(samples=[sample(value=4321.234567)])])
                store.ingest(payload)
                self.assertIn(b"4321.234567", store.path.read_bytes())
                now += 91 * 86400
                if operation == "status": store.status()
                elif operation == "dashboard": store.dashboard()
                elif operation == "reopen": WatchStore(directory, clock=lambda: now)
                elif operation == "replay": self.assertTrue(store.ingest(payload)["replayed"])
                elif operation == "rejected":
                    with self.assertRaises(ContractError): store.ingest({"schema_version": 100})
                elif operation == "retry": store.request_retry()
                elif operation == "pending": store.pending_outbox()
                else: store.record_outbox_failure(uid("batch"), "transport_unconnected")
                # Direct SQLite inspection must find no sample; hiding in the
                # returned dashboard does not satisfy physical retention.
                with closing(store._connect()) as connection:
                    self.assertEqual(connection.execute("SELECT count(*) FROM samples").fetchone()[0], 0)
                    self.assertEqual(connection.execute("SELECT anchor FROM queries").fetchone()[0], "anchor-1")
                    for table in ("batches", "identities", "outbox"):
                        self.assertEqual(connection.execute("SELECT count(*) FROM " + table).fetchone()[0], 1)
                self.assertNotIn(b"4321.234567", store.path.read_bytes())

    def test_delayed_old_failure_cannot_overwrite_newer_success_status(self):
        old = self.now
        entered, release = threading.Event(), threading.Event()
        remember = self.store._remember_failure
        def delayed(when, code):
            if when == old:
                entered.set()
                if not release.wait(5): raise RuntimeError("Synthetic failure race timed out")
            return remember(when, code)
        with patch.object(self.store, "_remember_failure", side_effect=delayed), ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.store.ingest, {"schema_version": 100})
            try:
                self.assertTrue(entered.wait(5))
                self.now += 60
                successful = self.store.ingest(batch())
            finally:
                release.set()
            with self.assertRaises(ContractError): future.result(timeout=5)
        status = self.store.status()
        self.assertEqual(status["last_success"], successful["committed_at"])
        self.assertEqual(status["last_attempt"], successful["committed_at"])
        self.assertIsNone(status["last_error"])

    def test_delayed_old_success_cannot_overwrite_newer_attempt_result(self):
        for latest in ("success", "failure"):
            with self.subTest(latest=latest):
                old = self.now
                directory = Path(self.temp.name) / ("race-" + latest)
                store = WatchStore(directory, clock=lambda: self.now)
                entered, release = threading.Event(), threading.Event()
                ingest = store._ingest
                def delayed(parsed, when):
                    if when == old:
                        entered.set()
                        if not release.wait(5): raise RuntimeError("Synthetic success race timed out")
                    return ingest(parsed, when)
                with patch.object(store, "_ingest", side_effect=delayed), ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(store.ingest, batch())
                    try:
                        self.assertTrue(entered.wait(5))
                        self.now += 60
                        if latest == "success":
                            store.ingest(batch([query("step_count", [sample("step_count", 20, label="new-steps")])], label="newer-success"))
                        else:
                            with self.assertRaises(ContractError): store.ingest({"schema_version": 100})
                        newer = store.status()
                    finally:
                        release.set()
                    older_success = future.result(timeout=5)
                status = store.status()
                self.assertEqual(status["last_attempt"], newer["last_attempt"])
                self.assertEqual(status["last_success"], newer["last_success"] if latest == "success" else older_success["committed_at"])
                self.assertEqual(status["last_error"], newer["last_error"])
                self.assertEqual(status["record_count"], 2 if latest == "success" else 1)

    def test_request_outbox_and_store_limits_keep_anchors_atomic(self):
        self.store.ingest(batch())
        for constant, value in (("MAX_OUTBOX", 1), ("MAX_BATCHES", 1), ("MAX_RECORDS", 1), ("MAX_IDENTITIES", 1), ("MAX_QUERY_SCOPES", 1)):
            additional = batch([query("step_count", [sample("step_count", 10, label="steps")])], label="more-" + constant)
            with patch("watch_sync." + constant, value), self.assertRaises(ResourceLimit):
                self.store.ingest(additional)
            self.assertEqual(len(self.rows("samples")), 1); self.assertEqual(len(self.rows("queries")), 1)
            self.assertEqual(len(self.rows("outbox")), 1)
        with self.assertRaises(ContractError): self.store.pending_outbox(101)
        with patch("watch_sync.MAX_DASHBOARD_ROWS", 0):
            dashboard = self.store.dashboard()
            self.assertEqual(dashboard["metrics"], []); self.assertTrue(dashboard["metrics_truncated"])

    def test_bounded_metrics_select_newest_dates_then_display_ascending(self):
        old = sample(label="old-day", value=61)
        recent = sample(label="recent-day", value=72, start="2031-03-11T10:00:00Z", end="2031-03-11T10:00:30Z")
        self.store.ingest(batch([query(samples=[old, recent])]))
        with patch("watch_sync.MAX_DASHBOARD_ROWS", 1):
            dashboard = self.store.dashboard()
            self.assertTrue(dashboard["metrics_truncated"])
            self.assertEqual(dashboard["metrics"][0]["date"], "2031-03-11")
            self.assertEqual(dashboard["metrics"][0]["value"], 72)
        with patch("watch_sync.MAX_DASHBOARD_ROWS", 2):
            self.assertEqual([r["date"] for r in self.store.dashboard()["metrics"]], ["2031-03-10", "2031-03-11"])

    def test_private_modes_system_temp_alias_and_user_symlink_rejection(self):
        self.assertEqual(stat.S_IMODE(self.store.directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.store.path.stat().st_mode), 0o600)
        self.assertEqual(self.store.directory, self.directory.resolve())
        alias = Path(self.temp.name) / "alias"
        alias.symlink_to(self.store.directory, target_is_directory=True)
        with self.assertRaises(ContractError): WatchStore(alias)
        with self.assertRaises(ContractError): WatchStore(alias / "nested")
        journal = self.store.directory / "watch.sqlite3-journal"
        journal.symlink_to(self.store.path)
        with self.assertRaises(ContractError): self.store.status()
        journal.unlink()
        hard = self.store.directory / "watch.sqlite3-wal"
        os.link(self.store.path, hard)
        with self.assertRaises(ContractError): self.store.status()
        hard.unlink()

    def test_sqlite_durability_and_minimization_pragmas_and_disclosed_caps(self):
        with closing(self.store._connect()) as connection:
            self.assertEqual(connection.execute("PRAGMA synchronous").fetchone()[0], 2)
            self.assertEqual(connection.execute("PRAGMA fullfsync").fetchone()[0], 1)
            self.assertEqual(connection.execute("PRAGMA secure_delete").fetchone()[0], 1)
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        self.assertEqual(self.store.status()["retention_days"], 90)
        self.assertEqual(self.store.status()["limits"]["outbox_batches"], 4096)

    def test_current_date_synthetic_fixture_requires_explicit_ingest(self):
        path = Path(__file__).resolve().parents[1] / "fixtures" / "watch-demo.json"
        invented = json.loads(path.read_text())
        self.now = timestamp("2026-10-13T14:00:00Z").timestamp()
        self.assertEqual(self.store.dashboard()["mode"], "empty")
        result = self.store.ingest(invented)
        dashboard = self.store.dashboard()
        self.assertEqual(result["mode"], "synthetic")
        self.assertGreater(len(dashboard["metrics"]), 0)
        self.assertGreater(dashboard["sleep"]["summary"]["total_minutes"], 0)

    def test_commit_failure_rolls_back_samples_anchors_receipts_and_outbox(self):
        with closing(self.store._connect()) as connection:
            connection.execute("CREATE TRIGGER fail_outbox BEFORE INSERT ON outbox BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
        with self.assertRaises(ContractError): self.store.ingest(batch())
        self.assertEqual(self.rows("samples"), []); self.assertEqual(self.rows("queries"), [])
        self.assertEqual(self.rows("batches"), []); self.assertEqual(self.rows("outbox"), [])
        with closing(self.store._connect()) as connection:
            connection.execute("DROP TRIGGER fail_outbox")
        self.assertFalse(self.store.ingest(batch())["replayed"])

    def test_abrupt_process_exit_before_commit_recovers_then_replays_safely(self):
        script = """
import json,os,sys
from pathlib import Path
from watch_sync import WatchStore
class CrashStore(WatchStore):
    def _connect(self):
        connection=super()._connect()
        connection.set_trace_callback(lambda sql: os._exit(23) if sql.startswith('INSERT INTO outbox') else None)
        return connection
store=CrashStore(Path(sys.argv[1]),clock=lambda:float(sys.argv[2]))
store.ingest(json.loads(sys.stdin.read()))
"""
        process = subprocess.run([sys.executable, "-c", script, str(self.directory), str(self.now)], input=json.dumps(batch()), text=True, capture_output=True)
        self.assertEqual(process.returncode, 23, process.stderr)
        recovered = WatchStore(self.directory, clock=lambda: self.now)
        self.assertEqual(recovered.status()["record_count"], 0)
        self.assertEqual(self.rows("queries"), []); self.assertEqual(self.rows("batches"), [])
        self.assertFalse(recovered.ingest(batch())["replayed"])
        self.assertTrue(recovered.ingest(batch())["replayed"])

    def test_requested_timezone_wins_legacy_settings_without_exposing_raw_intervals(self):
        sleep = sample("sleep_analysis", "core", start="2031-03-10T23:00:00Z", end="2031-03-11T06:00:00Z")
        self.store.ingest(batch([query("sleep_analysis", [sleep])]))
        dashboard = self.store.dashboard("UTC", settings={"timezone_mode": "recorded", "timezone": "Asia/Tokyo"})
        self.assertEqual(dashboard["sleep"]["settings"]["timezone"], "UTC")
        self.assertNotIn("segments", dashboard["sleep"]["nights"][0])
        self.assertNotIn("inbed", dashboard["sleep"]["nights"][0])
        self.assertEqual(dashboard["sleep"]["summary"]["total_minutes"], 420)


if __name__ == "__main__":
    unittest.main()
