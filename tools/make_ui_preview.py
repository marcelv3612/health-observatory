"""Create an isolated UI preview from wholly invented records; no live reader."""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import health
from server import DEFAULT_SETTINGS
from watch_metrics import CATALOG
from watch_sync import WatchStore


def uid(label):
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, "invented-ui-preview-" + label))


def create(directory: Path):
    if directory.exists():
        raise SystemExit("Refusing to replace an existing data directory.")
    os.umask(0o077)
    directory.mkdir(parents=True, mode=0o700)
    zone = ZoneInfo("UTC")
    base = datetime(2031, 3, 14, 23, 10, tzinfo=zone)
    records = []
    for day in range(28):
        if day in {5, 12, 21}:
            continue
        start = base + timedelta(days=day, minutes=(day * 11) % 55 - 20)
        cursor = start
        stages = [("AsleepCore", 82), ("AsleepDeep", 58 + day % 4 * 5),
                  ("AsleepCore", 116 + day % 6 * 7), ("AsleepREM", 77),
                  ("AsleepCore", 68)]
        if day == 7:
            stages = [("AsleepUnspecified", 366)]
        for index, (stage, minutes) in enumerate(stages):
            if day == 16 and index == 3:
                cursor += timedelta(minutes=25)
            end = cursor + timedelta(minutes=minutes)
            records.append(health.normalize({
                "type": health.SLEEP_TYPE, "value": "HKCategoryValueSleepAnalysis" + stage,
                "startDate": cursor.strftime("%Y-%m-%d %H:%M:%S %z"),
                "endDate": end.strftime("%Y-%m-%d %H:%M:%S %z"),
                "sourceName": "Invented Watch", "device": "Synthetic device A",
            }, {"HKTimeZone": "UTC"}))
            cursor = end
        records.append(health.normalize({
            "type": health.SLEEP_TYPE, "value": "HKCategoryValueSleepAnalysisInBed",
            "startDate": (start - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S %z"),
            "endDate": (cursor + timedelta(minutes=12)).strftime("%Y-%m-%d %H:%M:%S %z"),
            "sourceName": "Invented Watch", "device": "Synthetic device A",
        }, {"HKTimeZone": "UTC"}))
    borrowed_start = base + timedelta(days=16, minutes=(16 * 11) % 55 - 20)
    gap_start = borrowed_start + timedelta(minutes=82 + 58 + 116 + 4 * 7)
    dataset = {"mode": "demo", "records": records,
               "synthetic_protected_windows": [{
                   "start": (borrowed_start - timedelta(hours=11)).timestamp(),
                   "end": (borrowed_start + timedelta(hours=13)).timestamp(),
                   "owner": "Invented guest", "note": "Invented borrowed-watch demonstration",
                   "reported_gaps": [{"start": gap_start.timestamp(),
                                      "end": (gap_start + timedelta(minutes=25)).timestamp(),
                                      "label": "Invented recording gap; no sleep inferred"}]}],
               "import_stats": {"sleep_records": len(records), "exact_duplicates": 0, "unsupported_sleep_values": 0}}
    settings = {**DEFAULT_SETTINGS, "timezone_mode": "fixed", "timezone": "UTC"}
    for name, value in [("sleep.json", dataset), ("settings.json", settings)]:
        (directory / name).write_text(json.dumps(value, allow_nan=False))
    source = {"bundle_id": "com.example.invented.ui", "name": "Invented Watch",
              "device": {"manufacturer": "Synthetic", "model": "Preview device"}}
    quantities = {"resting_heart_rate": lambda d: 57 + d % 7,
                  "heart_rate": lambda d: 69 + d % 11,
                  "hrv_sdnn": lambda d: 38 + d % 9 * 2,
                  "step_count": lambda d: 0 if d == 9 else 5200 + d % 6 * 630,
                  "active_energy": lambda d: 280 + d % 6 * 34,
                  "apple_exercise_time": lambda d: 25 + d % 4 * 8,
                  "respiratory_rate": lambda d: 14 + d % 3 * .4,
                  "oxygen_saturation": lambda d: .965 + d % 4 * .003,
                  "sleeping_wrist_temperature": lambda d: 33.5 + d % 3 * .2,
                  "vo2_max": lambda d: 42 + d % 4 * .4}
    types = []
    sleep_samples = [{"id": uid("watch-sleep-" + str(index)), "type": "sleep_analysis",
                      "start": datetime.fromtimestamp(record["start"], zone).isoformat(),
                      "end": datetime.fromtimestamp(record["end"], zone).isoformat(),
                      "timezone": "UTC", "source": source,
                      "unit": "category", "value": record["stage"]}
                     for index, record in enumerate(records)]
    types.append({"type": "sleep_analysis", "kind": "baseline", "previous_anchor": None,
                  "next_anchor": "invented-preview-sleep", "read_state": "nonempty",
                  "samples": sleep_samples, "deleted_ids": []})
    for kind, value in quantities.items():
        samples = []
        for day in range(28):
            if day in {5, 12, 21}:
                continue
            start = (base + timedelta(days=day)).replace(hour=10, minute=0)
            samples.append({"id": uid(kind + str(day)), "type": kind,
                            "start": start.isoformat(), "end": (start + timedelta(minutes=5)).isoformat(),
                            "timezone": "UTC", "source": source,
                            "unit": CATALOG[kind]["unit"], "value": value(day)})
        types.append({"type": kind, "kind": "baseline", "previous_anchor": None,
                      "next_anchor": "invented-preview-" + kind, "read_state": "nonempty",
                      "samples": samples, "deleted_ids": []})
    store = WatchStore(directory / "watch")
    batch = {"schema_version": 1, "batch_id": uid("batch"), "mode": "synthetic",
                  "stream_id": uid("stream"), "source_bundle_id": source["bundle_id"],
                  "generated_at": "2031-04-12T12:00:00Z", "types": types}
    store.ingest(batch)
    print("Invented UI preview created. No HealthKit, export, credential or network reader was used.")
    return {"sleep": dataset, "settings": settings, "watch_batch": batch}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    create(parser.parse_args().data_dir.resolve())
