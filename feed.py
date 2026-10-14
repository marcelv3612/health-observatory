#!/usr/bin/env python3
"""Opt-in local incremental feeds. No phone access, network clients, or scheduled installs."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
from itertools import islice
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from health import MAX_SLEEP_RECORDS, SLEEP_TYPE, ImportError, dashboard, load_export, normalize
from server import DEFAULT_SETTINGS, ROOT, Store

MAX_JSON = 8 * 1024 * 1024
JSON_VALUES = {"Core": "AsleepCore", "Deep": "AsleepDeep", "REM": "AsleepREM",
               "Awake": "Awake", "In Bed": "InBed", "Asleep": "AsleepUnspecified",
               "Unspecified": "AsleepUnspecified"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_feed(path: Path) -> dict[str, Any]:
    if path.suffix.lower() != ".json":
        return load_export(path)
    if path.stat().st_size > MAX_JSON:
        raise ImportError("JSON feed exceeds the 8 MiB incremental-feed limit.")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise ImportError("The feed is not valid UTF-8 JSON.") from exc
    if not isinstance(payload, dict):
        raise ImportError("Expected a Health Auto Export metrics object.")
    # The documented metric array may be the root, or in the REST export data envelope.
    root = payload.get("data", payload)
    if not isinstance(root, dict) or not isinstance(root.get("metrics"), list):
        raise ImportError("Expected the documented Health Auto Export metrics array.")
    records: list[dict[str, Any]] = []
    for metric in root["metrics"]:
        if not isinstance(metric, dict) or metric.get("name") != "sleep_analysis":
            continue
        if metric.get("units") != "hr" or not isinstance(metric.get("data"), list):
            raise ImportError("Sleep JSON must contain unaggregated hour-based sleep data.")
        for sample in metric["data"]:
            if not isinstance(sample, dict) or not all(k in sample for k in ("startDate", "endDate", "value")):
                raise ImportError("Daily summarized JSON cannot supply a stage timeline. Disable aggregation.")
            value = JSON_VALUES.get(sample["value"]) if isinstance(sample["value"], str) else None
            if value is None:
                raise ImportError("A JSON sleep segment contains an unsupported stage; the feed was kept out.")
            source = sample.get("source")
            if not isinstance(source, str) or not source.strip():
                raise ImportError("Per-segment source is missing. Verify the phone exporter before enabling sync.")
            if any(not isinstance(sample[k], str) for k in ("startDate", "endDate")):
                raise ImportError("Sleep segment timestamps must be strings with explicit UTC offsets.")
            attrs = {"type": SLEEP_TYPE, "value": "HKCategoryValueSleepAnalysis"+value,
                     "startDate": sample["startDate"], "endDate": sample["endDate"],
                     "sourceName": source, "device": ""}
            record = normalize(attrs)
            if "qty" in sample:
                qty = sample["qty"]
                if isinstance(qty, bool) or not isinstance(qty, (int, float)) or not math.isfinite(qty) or qty < 0:
                    raise ImportError("Invalid segment duration quantity.")
                if abs(qty*3600 - (record["end"]-record["start"])) > 60:
                    raise ImportError("Segment quantity disagrees with its elapsed timestamps by more than one minute.")
            records.append(record)
            if len(records) > MAX_SLEEP_RECORDS:
                raise ImportError("JSON feed exceeds the sleep interval count limit.")
    if not records:
        raise ImportError("The JSON feed contains no individual sleep segments.")
    return {"mode": "imported", "records": records, "imported_at": now(),
            "import_stats": {"sleep_records": len(records), "exact_duplicates": 0, "unsupported_sleep_values": 0},
            "feed_format": "health-auto-export-unaggregated-json"}


def merge(base: dict[str, Any], incoming: dict[str, Any]) -> tuple[dict[str, Any], int]:
    # A feed starts a fresh imported dataset when the only existing data is synthetic demo.
    existing = base["records"] if base.get("mode") == "imported" else []
    combined = {(r["start"], r["end"], r["stage"], r["source_id"]): r for r in existing}
    before = len(combined)
    for r in incoming["records"]:
        key = (r["start"], r["end"], r["stage"], r["source_id"])
        combined.setdefault(key, r)
    if len(combined) > MAX_SLEEP_RECORDS:
        raise ImportError("Merged history exceeds the 250,000-interval limit.")
    result = {**base, "mode": "imported", "records": sorted(combined.values(), key=lambda r:r["start"]),
              "imported_at": now(), "feed_format": incoming.get("feed_format", "apple-health-xml")}
    result["import_stats"] = base.get("import_stats", {}) if existing else incoming.get("import_stats", {})
    return result, len(combined)-before


def ingest(path: Path, store: Store, apply: bool = False) -> dict[str, Any]:
    before = store.version("sleep.json")
    # Atomic file reads plus compare-and-swap on write prevent overwriting a newer manual import.
    base = store.read("sleep.json") or store.demo()
    incoming = load_feed(path)
    result, added = merge(base, incoming)
    settings = {**DEFAULT_SETTINGS, **(store.read("settings.json") or {})}
    dashboard(result, settings)
    if apply:
        store.write("sleep.json", result, expected_version=before)
        store.write("feed-status.json", {"last_success": now(), "added_records": added,
                                        "format": result["feed_format"], "last_error": None})
    return {"ok": True, "applied": apply, "added_records": added, "total_records": len(result["records"])}


def watch(folder: Path, store: Store, interval: int) -> None:
    # At most 10 stable files per poll. No recursive walk, symlinks, networking, or OS scheduling.
    processed: dict[str, str] = {}
    processed_versions: dict[str, tuple[int, int]] = {}
    candidate_versions: dict[str, tuple[int, int]] = {}
    while True:
        entries = list(islice(folder.iterdir(), 1001))
        if len(entries) > 1000:
            raise ValueError("Use a dedicated feed folder with at most 1,000 entries.")
        files = [p for p in entries if p.suffix.lower() in (".xml", ".zip", ".json") and p.is_file() and not p.is_symlink()]
        candidates = sorted([p for p in files if processed_versions.get(p.name) != (p.stat().st_size, p.stat().st_mtime_ns)],
                            key=lambda p:p.stat().st_mtime_ns, reverse=True)[:10]
        for path in candidates:
            stat = path.stat()
            version = (stat.st_size, stat.st_mtime_ns)
            if candidate_versions.get(path.name) != version:
                candidate_versions[path.name] = version
                continue  # First seen / still changing: wait one full polling interval.
            try:
                limit = MAX_JSON if path.suffix.lower()==".json" else 256*1024*1024
                if stat.st_size > limit:
                    raise ImportError("An incoming feed exceeds its file-size guard.")
                hasher = hashlib.sha256()
                total = 0
                with path.open("rb") as stream:
                    while chunk := stream.read(64*1024):
                        total += len(chunk)
                        if total > limit:
                            raise ImportError("Incoming feed grew beyond its file-size guard.")
                        hasher.update(chunk)
                current = path.stat()
                if (current.st_size, current.st_mtime_ns) != version:
                    candidate_versions[path.name] = (current.st_size, current.st_mtime_ns)
                    continue
                digest = hasher.hexdigest()
                if processed.get(path.name) == digest:
                    processed_versions[path.name] = version
                    continue
                ingest(path, store, True)
                processed[path.name] = digest
                processed_versions[path.name] = version
                print("Local feed imported successfully.", flush=True)
            except (ValueError, OSError) as exc:
                prior = store.read("feed-status.json") or {}
                store.write("feed-status.json", {**prior, "last_attempt": now(), "last_error": str(exc)[:500]})
                # Retry rejected inputs only after they change, so one invalid feed cannot starve others.
                processed_versions[path.name] = version
                print("Local feed rejected; previous history preserved.", flush=True)
        time.sleep(interval)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--file", type=Path, help="Validate one XML/ZIP or unaggregated JSON incremental feed")
    group.add_argument("--watch-folder", type=Path, help="Opt-in LOCAL folder with an explicitly configured phone transfer")
    parser.add_argument("--data-dir", type=Path, default=ROOT/".private")
    parser.add_argument("--apply", action="store_true", help="Commit incremental records; omitted means a read-only dry run")
    parser.add_argument("--interval", type=int, default=60, help="Polling seconds (60–3600)")
    args = parser.parse_args()
    if not 60 <= args.interval <= 3600:
        parser.error("Polling interval must be between 60 and 3600 seconds.")
    if args.watch_folder and not args.apply:
        parser.error("Watching requires --apply. It never activates implicitly.")
    store = Store(args.data_dir.resolve())
    if args.file:
        print(json.dumps(ingest(args.file.resolve(), store, args.apply)))
    else:
        folder = args.watch_folder.resolve(strict=True)
        if not folder.is_dir():
            parser.error("Choose an existing local feed folder.")
        print("Local folder watcher active. It does not export or access iPhone Health. Ctrl+C stops it.", flush=True)
        watch(folder, store, args.interval)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as exc:
        raise SystemExit(str(exc))
