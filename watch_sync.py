"""Local transactional companion ingestion. No HealthKit reader or network transport.

Anchors describe an exact (stream UUID, source bundle, HealthKit type) query.
Raw input is discarded; only allowlisted normalized samples and a small UUID/revision
invalidation outbox are durable. The outbox is not an upload and has no recipient.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from watch_metrics import CATALOG, MAX_OPERATIONS, MAX_REQUEST_BYTES, ContractError, canonical, daily_metrics, normalize_batch, sleep_dashboard, string

MAX_RECORDS = 50_000
MAX_IDENTITIES = 100_000
MAX_QUERY_SCOPES = 1024
MAX_BATCHES = 4096
MAX_OUTBOX = 4096
MAX_DATABASE_BYTES = 64 * 1024 * 1024
MAX_DASHBOARD_ROWS = 2000
MAX_RETRY_ATTEMPTS = 8
DESTINATION = {"schema_version": 1, "enabled": False, "configured": False, "destination": None,
               "transport": "unconnected", "requires_approval": True}


class SyncConflict(ContractError):
    """The supplied batch cannot safely replace the current query state."""


class ResourceLimit(ContractError):
    """The store is full; no anchors or sample changes were committed."""


def iso(instant: float) -> str:
    return datetime.fromtimestamp(instant, timezone.utc).isoformat()


def retry_delay(attempt: int) -> int:
    """Deterministic bounded exponential delay; transport is still unconnected."""
    if type(attempt) is not int or not 1 <= attempt <= MAX_RETRY_ATTEMPTS:
        raise ContractError("The retry attempt is outside the bounded range.")
    return min(24 * 3600, 60 * 2 ** (attempt - 1))


class WatchStore:
    def __init__(self, directory: Path, *, clock: Callable[[], float] = time.time, retention_days: int = 90):
        if type(retention_days) is not int or not 1 <= retention_days <= 366:
            raise ContractError("Choose local retention from 1 to 366 days.")
        self.directory = Path(directory).absolute()
        # macOS exposes these exact root-owned system aliases. Canonicalize only
        # their verified targets; supplied directory symlinks remain forbidden.
        for alias, target in ((Path("/tmp"), Path("/private/tmp")), (Path("/var"), Path("/private/var"))):
            if alias in self.directory.parents and alias.is_symlink():
                info = alias.lstat()
                if info.st_uid != 0 or alias.resolve() != target:
                    raise ContractError("The system temporary-directory alias is unexpected.")
                self.directory = target / self.directory.relative_to(alias)
        self.path = self.directory / "watch.sqlite3"
        self.clock, self.retention_days = clock, retention_days
        self._secure_directory()
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
            # Make the new directory entry durable before SQLite acknowledges writes.
            fd = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        with closing(self._connect()) as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS queries(
                    stream_id TEXT NOT NULL, bundle TEXT NOT NULL, type TEXT NOT NULL,
                    anchor TEXT, read_state TEXT NOT NULL, error TEXT,
                    last_attempt TEXT NOT NULL, last_success TEXT,
                    PRIMARY KEY(stream_id,bundle,type));
                CREATE TABLE IF NOT EXISTS identities(
                    id TEXT PRIMARY KEY, stream_id TEXT NOT NULL, bundle TEXT NOT NULL, type TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS samples(
                    id TEXT PRIMARY KEY, stream_id TEXT NOT NULL, bundle TEXT NOT NULL,
                    type TEXT NOT NULL, start REAL NOT NULL, end REAL NOT NULL,
                    document TEXT NOT NULL, revision TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS samples_end ON samples(end);
                CREATE TABLE IF NOT EXISTS batches(
                    id TEXT PRIMARY KEY, digest TEXT NOT NULL, response TEXT NOT NULL,
                    received REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS outbox(
                    id TEXT PRIMARY KEY REFERENCES batches(id), manifest TEXT NOT NULL,
                    created REAL NOT NULL, next_attempt REAL NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0, error TEXT);
            """)
            current_version = self._get(connection, "schema_version")
            if current_version not in (None, "1"):
                raise ContractError("The local Watch store schema is unsupported.")
            current_retention = self._get(connection, "retention_days")
            if current_retention not in (None, str(retention_days)):
                raise ContractError("Changing retention requires an explicit store migration.")
            connection.execute("BEGIN IMMEDIATE")
            self._put(connection, "schema_version", "1")
            self._put(connection, "retention_days", str(retention_days))
            connection.commit()
            self._prune_expired(connection)

    def _secure_directory(self) -> None:
        # Never follow a supplied symlink to a different private directory.
        for ancestor in (self.directory, *self.directory.parents):
            if ancestor.is_symlink():
                raise ContractError("The Watch directory must not contain symlink components.")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise ContractError("The Watch directory must be owned by the current user.")
        self.directory.chmod(0o700)

    def _check_files(self) -> None:
        self._secure_directory()
        for name in ("watch.sqlite3", "watch.sqlite3-journal", "watch.sqlite3-wal", "watch.sqlite3-shm"):
            target = self.directory / name
            try:
                info = target.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                raise ContractError("The Watch store file must be a private regular file.")
            if info.st_size > MAX_DATABASE_BYTES:
                raise ResourceLimit("The local Watch database exceeds its 64 MiB limit.")
            target.chmod(0o600)

    def _connect(self) -> sqlite3.Connection:
        self._check_files()
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA fullfsync=ON")
            connection.execute("PRAGMA secure_delete=ON")
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA busy_timeout=10000")
            page_size = connection.execute("PRAGMA page_size").fetchone()[0]
            connection.execute(f"PRAGMA max_page_count={MAX_DATABASE_BYTES // page_size}")
        except Exception:
            connection.close()
            raise
        return connection

    @staticmethod
    def _get(connection: sqlite3.Connection, key: str) -> str | None:
        item = connection.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return item[0] if item else None

    @staticmethod
    def _put(connection: sqlite3.Connection, key: str, value: str) -> None:
        connection.execute("INSERT INTO metadata VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def _now(self) -> float:
        value = self.clock()
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 946684800 <= value <= 4133980800:
            raise ContractError("The local clock is outside the supported finite range.")
        return value

    def _prune_expired(self, connection: sqlite3.Connection) -> None:
        """Physically erase expired values in a separate bounded maintenance commit.

        This runs on open/access even if the next import is a replay or rejected.
        No daemon runs while the application is closed. Retained UUID provenance,
        anchors, receipts and value-free invalidation manifests are unaffected.
        """
        cutoff = self._now() - self.retention_days * 86400
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM samples WHERE id IN (SELECT id FROM samples WHERE end<? ORDER BY end,id LIMIT ?)",
                               (cutoff, MAX_RECORDS))
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    def _write_attempt_status(self, connection: sqlite3.Connection, when: float, error: str, *, success: bool = False) -> None:
        """Publish the latest attempt result and a monotonic success timestamp.

        The caller holds the SQLite write transaction, so a delayed failure or
        earlier-started successful batch cannot replace a newer operation result.
        Sample and anchor commits remain independent of this display ordering.
        """
        # Successful commits have their own monotonic timestamp: an earlier
        # success may fill it after a later failure, without clearing that failure.
        if success:
            previous_success = self._get(connection, "last_success")
            if previous_success is None or datetime.fromisoformat(previous_success).timestamp() <= when:
                self._put(connection, "last_success", iso(when))
        previous = self._get(connection, "last_attempt")
        if previous and datetime.fromisoformat(previous).timestamp() > when:
            return
        self._put(connection, "last_attempt", iso(when))
        self._put(connection, "last_error", error)

    def _remember_failure(self, when: float, code: str) -> None:
        with closing(self._connect()) as connection:
            self._prune_expired(connection)
            connection.execute("BEGIN IMMEDIATE")
            self._write_attempt_status(connection, when, code)
            connection.commit()

    def ingest(self, payload: Any) -> dict[str, Any]:
        """Commit changes + exact query anchors + replay receipt + outbox atomically."""
        now = self._now()
        try:
            with closing(self._connect()) as connection:
                self._prune_expired(connection)
            batch = normalize_batch(payload)
            return self._ingest(batch, now)
        except (ContractError, sqlite3.Error) as exc:
            code = "sync_conflict" if isinstance(exc, SyncConflict) else "resource_limit" if isinstance(exc, ResourceLimit) else "batch_rejected" if isinstance(exc, ContractError) else "local_storage_error"
            try:
                self._remember_failure(now, code)
            except (ContractError, sqlite3.Error, OSError):
                pass
            if isinstance(exc, sqlite3.Error):
                raise ContractError("The local Watch transaction could not be committed.") from exc
            raise

    def _ingest(self, batch: dict[str, Any], now: float) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute("SELECT digest,response FROM batches WHERE id=?", (batch["batch_id"],)).fetchone()
                if existing:
                    if existing["digest"] != batch["request_hash"]:
                        raise SyncConflict("This batch UUID was already committed with different content.")
                    import json
                    response = json.loads(existing["response"])
                    response["replayed"] = True
                    connection.rollback()
                    return response
                mode = self._get(connection, "mode")
                if mode and mode != batch["mode"]:
                    raise SyncConflict("Synthetic and device datasets require separate Watch stores.")
                if connection.execute("SELECT count(*) FROM batches").fetchone()[0] >= MAX_BATCHES:
                    raise ResourceLimit("The replay receipt limit was reached; an explicit migration is required.")
                if connection.execute("SELECT count(*) FROM outbox").fetchone()[0] >= MAX_OUTBOX:
                    raise ResourceLimit("The disabled destination outbox is full; no query anchors were advanced.")
                cutoff = self._now() - self.retention_days * 86400
                upserts, deletes, query_states = [], [], []
                for query in batch["types"]:
                    scope = (batch["stream_id"], batch["source_bundle_id"], query["type"])
                    current = connection.execute("SELECT anchor,last_success FROM queries WHERE stream_id=? AND bundle=? AND type=?", scope).fetchone()
                    current_anchor = current["anchor"] if current else None
                    if current_anchor != query["previous_anchor"]:
                        raise SyncConflict("The query anchor is stale; request the delta from the current anchor.")
                    if query["kind"] == "baseline" and current_anchor is not None:
                        raise SyncConflict("A completed baseline cannot be reset by a companion batch.")
                    if query["kind"] == "incremental" and current_anchor is None:
                        raise SyncConflict("This query needs an explicit initial baseline.")
                    success = query["read_state"] in {"empty", "nonempty"}
                    for sample in query["samples"]:
                        identity = connection.execute("SELECT * FROM identities WHERE id=?", (sample["id"],)).fetchone()
                        if identity and tuple(identity[k] for k in ("stream_id", "bundle", "type")) != scope:
                            raise SyncConflict("A sample UUID cannot change its original query provenance.")
                        connection.execute("INSERT OR IGNORE INTO identities VALUES(?,?,?,?)", (sample["id"], *scope))
                        previous = connection.execute("SELECT revision FROM samples WHERE id=?", (sample["id"],)).fetchone()
                        if sample["end"] < cutoff:
                            if previous:
                                connection.execute("DELETE FROM samples WHERE id=?", (sample["id"],))
                            continue
                        document = canonical(sample)
                        revision = hashlib.sha256(document.encode()).hexdigest()
                        if previous and previous["revision"] == revision:
                            continue
                        connection.execute("""INSERT INTO samples VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                            start=excluded.start,end=excluded.end,document=excluded.document,revision=excluded.revision""",
                            (sample["id"], *scope, sample["start"], sample["end"], document, revision))
                        upserts.append({"id": sample["id"], "type": query["type"], "revision": revision})
                    for sample_id in query["deleted_ids"]:
                        previous = connection.execute("SELECT * FROM identities WHERE id=?", (sample_id,)).fetchone()
                        if previous and tuple(previous[k] for k in ("stream_id", "bundle", "type")) != scope:
                            raise SyncConflict("A deletion UUID is outside its original query provenance.")
                        connection.execute("INSERT OR IGNORE INTO identities VALUES(?,?,?,?)", (sample_id, *scope))
                        connection.execute("DELETE FROM samples WHERE id=?", (sample_id,))
                        deletes.append({"id": sample_id, "type": query["type"]})
                    last_success = iso(now) if success else current["last_success"] if current else None
                    connection.execute("""INSERT INTO queries VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(stream_id,bundle,type)
                        DO UPDATE SET anchor=excluded.anchor,read_state=excluded.read_state,error=excluded.error,
                        last_attempt=excluded.last_attempt,last_success=excluded.last_success""",
                        (*scope, query["next_anchor"], query["read_state"], query["error"], iso(now), last_success))
                    query_states.append({"type": query["type"], "read_state": query["read_state"], "coverage": "unknown"})
                if connection.execute("SELECT count(*) FROM samples").fetchone()[0] > MAX_RECORDS:
                    raise ResourceLimit("The 50,000 retained sample limit was reached.")
                if connection.execute("SELECT count(*) FROM identities").fetchone()[0] > MAX_IDENTITIES:
                    raise ResourceLimit("The 100,000 provenance identity limit was reached.")
                if connection.execute("SELECT count(*) FROM queries").fetchone()[0] > MAX_QUERY_SCOPES:
                    raise ResourceLimit("The 1,024 anchored query scope limit was reached.")
                complete = all(q["read_state"] in {"empty", "nonempty"} for q in batch["types"])
                response = {"batch_id": batch["batch_id"], "mode": batch["mode"], "replayed": False,
                            "upserted": len(upserts), "deleted": len(deletes), "query_states": query_states,
                            "committed_at": iso(now), "complete": complete, "destination_disabled": True}
                connection.execute("INSERT INTO batches VALUES(?,?,?,?)", (batch["batch_id"], batch["request_hash"], canonical(response), now))
                manifest = {"schema_version": 1, "batch_id": batch["batch_id"], "mode": batch["mode"],
                            "stream_id": batch["stream_id"], "source_bundle_id": batch["source_bundle_id"],
                            "upserts": upserts, "deletes": deletes}
                if upserts or deletes:
                    connection.execute("INSERT INTO outbox(id,manifest,created,next_attempt) VALUES(?,?,?,?)", (batch["batch_id"], canonical(manifest), now, now))
                self._put(connection, "mode", batch["mode"])
                if complete:
                    self._write_attempt_status(connection, now, "", success=True)
                else:
                    self._write_attempt_status(connection, now, "read_error" if any(q["read_state"] == "error" for q in batch["types"]) else "read_visibility_unknown")
                connection.commit()
                return response
            except BaseException:
                connection.rollback()
                raise

    def _status(self, connection: sqlite3.Connection) -> dict[str, Any]:
        now = self._now()
        return {"mode": self._get(connection, "mode") or "empty", "last_success": self._get(connection, "last_success"),
                "last_attempt": self._get(connection, "last_attempt"), "last_error": self._get(connection, "last_error") or None,
                "retry_requested": self._get(connection, "retry_requested"),
                "pending_outbox": connection.execute("SELECT count(*) FROM outbox").fetchone()[0],
                "due_outbox": connection.execute("SELECT count(*) FROM outbox WHERE next_attempt<=? AND attempts<?", (now, MAX_RETRY_ATTEMPTS)).fetchone()[0],
                "exhausted_outbox": connection.execute("SELECT count(*) FROM outbox WHERE attempts>=?", (MAX_RETRY_ATTEMPTS,)).fetchone()[0],
                "destination": dict(DESTINATION), "destination_disabled": True, "retention_days": self.retention_days,
                "limits": {"request_bytes": MAX_REQUEST_BYTES, "batch_operations": MAX_OPERATIONS,
                           "retained_samples": MAX_RECORDS, "provenance_ids": MAX_IDENTITIES, "query_scopes": MAX_QUERY_SCOPES,
                           "replay_receipts": MAX_BATCHES, "outbox_batches": MAX_OUTBOX, "database_bytes": MAX_DATABASE_BYTES,
                           "dashboard_rows": MAX_DASHBOARD_ROWS, "retry_attempts": MAX_RETRY_ATTEMPTS},
                "record_count": connection.execute("SELECT count(*) FROM samples WHERE end>=?", (now - self.retention_days*86400,)).fetchone()[0],
                "bridge_connected": False, "timing": "opportunistic_when_companion_connected"}

    def status(self) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            self._prune_expired(connection)
            return self._status(connection)

    def request_retry(self) -> dict[str, Any]:
        """Record local intent only. No phone read, device prompt or upload is made."""
        with closing(self._connect()) as connection:
            self._prune_expired(connection)
            connection.execute("BEGIN IMMEDIATE")
            self._put(connection, "retry_requested", iso(self._now()))
            connection.commit()
            return self._status(connection)

    def pending_outbox(self, limit: int = 100) -> list[dict[str, Any]]:
        """Inspect bounded invalidation manifests, not health values or transport jobs."""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ContractError("Inspect at most 100 outbox manifests at once.")
        import json
        with closing(self._connect()) as connection:
            self._prune_expired(connection)
            return [{"id": r["id"], "manifest": json.loads(r["manifest"]), "attempts": r["attempts"],
                     "next_attempt": iso(r["next_attempt"]), "error": r["error"], "transport_disabled": True}
                    for r in connection.execute("SELECT * FROM outbox ORDER BY created,id LIMIT ?", (limit,))]

    def record_outbox_failure(self, batch_id: str, code: str) -> dict[str, Any]:
        """Persist a bounded local queue failure; this hook performs no transport."""
        from watch_metrics import identifier
        import re
        identifier(batch_id)
        string(code, 64)
        if not re.fullmatch(r"[a-z0-9_]{1,64}", code):
            raise ContractError("Queue errors must be bounded codes without record details.")
        now = self._now()
        with closing(self._connect()) as connection:
            self._prune_expired(connection)
            connection.execute("BEGIN IMMEDIATE")
            item = connection.execute("SELECT attempts FROM outbox WHERE id=?", (batch_id,)).fetchone()
            if not item:
                raise ContractError("This local queue item does not exist.")
            attempt = min(MAX_RETRY_ATTEMPTS, item["attempts"] + 1)
            connection.execute("UPDATE outbox SET attempts=?,next_attempt=?,error=? WHERE id=?", (attempt, now + retry_delay(attempt), code, batch_id))
            connection.commit()
            return self._status(connection)

    def dashboard(self, timezone: str = "UTC", start: str = "", end: str = "", settings: dict[str, Any] | None = None) -> dict[str, Any]:
        import json
        with closing(self._connect()) as connection:
            self._prune_expired(connection)
            connection.execute("BEGIN")
            status = self._status(connection)
            cutoff = self._now() - self.retention_days * 86400
            records = [json.loads(r[0]) for r in connection.execute("SELECT document FROM samples WHERE end>=? ORDER BY start,id", (cutoff,))]
            queries = [{"stream_id": r["stream_id"], "source_bundle_id": r["bundle"], "type": r["type"],
                        "read_state": r["read_state"], "coverage": "unknown", "error": r["error"],
                        "last_attempt": r["last_attempt"], "last_success": r["last_success"]}
                       for r in connection.execute("SELECT * FROM queries ORDER BY bundle,type,stream_id")]
            connection.rollback()
        metrics = daily_metrics(records, timezone, start, end)
        # Retain recent dates first, then restore the dashboard's ascending order.
        # Sorting the full row key makes selection stable when dates are tied.
        selected_metrics = sorted(sorted(metrics, key=lambda r: (r["date"], r["type"], r["source_id"]), reverse=True)[:MAX_DASHBOARD_ROWS],
                                  key=lambda r: (r["date"], r["type"], r["source_id"]))
        sleep = sleep_dashboard(records, status["mode"], timezone, start, end, settings)
        # Full sleep segments remain inside health.dashboard; cap response rows and
        # strip sample-level interval arrays from this application-facing API.
        nights = sleep["nights"]
        sleep["nights"] = [{k: v for k, v in n.items() if k not in {"segments", "inbed", "gaps"}} for n in nights[:MAX_DASHBOARD_ROWS]]
        sleep["truncated"] = len(nights) > MAX_DASHBOARD_ROWS
        return {"schema_version": 1, "mode": status["mode"], "historical_timezone_unknown_samples": sum(r.get("timezone_provenance", "recorded") == "utc_reference" for r in records), "catalog": [{"type": k, **v, "availability": "device_and_region_dependent"} for k, v in CATALOG.items()],
                "metrics": selected_metrics, "metrics_truncated": len(metrics) > MAX_DASHBOARD_ROWS,
                "sleep": sleep, "sync": status, "read_states": queries[:MAX_DASHBOARD_ROWS],
                "read_states_truncated": len(queries) > MAX_DASHBOARD_ROWS, "coverage": "unknown",
                "provenance_note": "Summaries remain separate by companion stream, source bundle and reported device model; same-model devices in one stream may share a source group.",
                "cumulative_note": "No totals across sources. Overlapping or cross-midnight cumulative samples have unknown daily totals."}
