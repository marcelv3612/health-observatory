"""Strict, minimized companion sample contract and local Watch summaries.

Only individual HealthKit samples are accepted, never statistics-query aggregates.
This module does not query HealthKit, infer read authorization, or contact a sink.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import statistics
import uuid
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import health

SCHEMA_VERSION = 1
MAX_REQUEST_BYTES = 1024 * 1024
MAX_OPERATIONS = 2000
MAX_ANCHOR_BYTES = 2048
MAX_DASHBOARD_DAYS = 366
CATALOG = {
    "sleep_analysis": {"label": "Sleep stages", "unit": "category", "aggregation": "interval_union", "healthkit": "HKCategoryTypeIdentifierSleepAnalysis"},
    "heart_rate": {"label": "Heart rate", "unit": "count/min", "aggregation": "sample_mean", "healthkit": "HKQuantityTypeIdentifierHeartRate"},
    "resting_heart_rate": {"label": "Resting heart rate", "unit": "count/min", "aggregation": "sample_mean", "healthkit": "HKQuantityTypeIdentifierRestingHeartRate"},
    "hrv_sdnn": {"label": "HRV (SDNN)", "unit": "ms", "aggregation": "sample_mean", "healthkit": "HKQuantityTypeIdentifierHeartRateVariabilitySDNN"},
    "step_count": {"label": "Steps", "unit": "count", "aggregation": "individual_sample_sum", "healthkit": "HKQuantityTypeIdentifierStepCount"},
    "active_energy": {"label": "Active energy", "unit": "kcal", "aggregation": "individual_sample_sum", "healthkit": "HKQuantityTypeIdentifierActiveEnergyBurned"},
    "apple_exercise_time": {"label": "Exercise minutes", "unit": "min", "aggregation": "individual_sample_sum", "healthkit": "HKQuantityTypeIdentifierAppleExerciseTime"},
    "respiratory_rate": {"label": "Respiratory rate", "unit": "count/min", "aggregation": "sample_mean", "healthkit": "HKQuantityTypeIdentifierRespiratoryRate"},
    "oxygen_saturation": {"label": "Oxygen saturation", "unit": "fraction", "aggregation": "sample_mean", "healthkit": "HKQuantityTypeIdentifierOxygenSaturation"},
    "sleeping_wrist_temperature": {"label": "Sleeping wrist temperature (absolute)", "unit": "degC", "aggregation": "sample_mean", "healthkit": "HKQuantityTypeIdentifierAppleSleepingWristTemperature"},
    "vo2_max": {"label": "VO₂ max estimate", "unit": "mL/kg/min", "aggregation": "sample_mean", "healthkit": "HKQuantityTypeIdentifierVO2Max"},
}
CUMULATIVE = {k for k, item in CATALOG.items() if item["aggregation"] == "individual_sample_sum"}
READ_STATES = {"unknown", "empty", "error", "nonempty"}
STAGES = {"inbed", "awake", "unspecified", "core", "deep", "rem"}
STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$")
BUNDLE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,199}$")


class ContractError(ValueError):
    """Safe rejection text; it never includes supplied sample values or anchors."""


def canonical(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError, RecursionError) as exc:
        raise ContractError("The batch must contain finite JSON values.") from exc


def keys(value: Any, required: set[str], optional: set[str] | None = None) -> None:
    if not isinstance(value, dict) or not required <= value.keys() or value.keys() - required - (optional or set()):
        raise ContractError("The companion object has missing or unsupported fields.")


def string(value: Any, limit: int, *, empty: bool = False) -> str:
    try:
        valid = isinstance(value, str) and (empty or bool(value)) and len(value.encode("utf-8")) <= limit and not any(ord(c) < 32 for c in value)
    except UnicodeError:
        valid = False
    if not valid:
        raise ContractError("A companion text field is invalid or too long.")
    return value


def identifier(value: Any) -> str:
    try:
        parsed = uuid.UUID(string(value, 36))
    except (ValueError, AttributeError) as exc:
        raise ContractError("Samples, streams and batches need original canonical UUID identifiers.") from exc
    if str(parsed) != value or parsed.int == 0:
        raise ContractError("Samples, streams and batches need original canonical UUID identifiers.")
    return value


def timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not STAMP.fullmatch(value):
        raise ContractError("Timestamps need ISO 8601 seconds and an explicit UTC offset.")
    try:
        # Some runtime parsers normalize 24:00 and offset minute overflow. The
        # wire contract accepts actual civil components, never their rollover.
        if not (int(value[11:13]) < 24 and int(value[14:16]) < 60 and int(value[17:19]) < 60):
            raise ValueError
        if not value.endswith("Z"):
            offset = value[-6:]
            if int(offset[1:3]) > 14 or int(offset[4:6]) >= 60:
                raise ValueError
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if not 2000 <= result.year <= 2100 or abs(result.utcoffset().total_seconds()) > 14 * 3600 or not math.isfinite(result.timestamp()):
            raise ValueError
        return result
    except (ValueError, OverflowError) as exc:
        raise ContractError("A timestamp is outside the supported finite range.") from exc


def zone(value: Any) -> ZoneInfo:
    try:
        return ZoneInfo(string(value, 100))
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ContractError("A valid IANA timezone is required.") from exc


def bundle(value: Any) -> str:
    result = string(value, 200)
    if not BUNDLE.fullmatch(result) or "." not in result:
        raise ContractError("A source bundle identifier is required.")
    return result


def anchor(value: Any) -> str | None:
    return None if value is None else string(value, MAX_ANCHOR_BYTES)


def normalize_sample(raw: Any, kind: str, source_bundle: str) -> dict[str, Any]:
    keys(raw, {"id", "type", "start", "end", "timezone", "source", "unit", "value"}, {"end_timezone", "timezone_provenance"})
    if raw["type"] != kind or raw["unit"] != CATALOG[kind]["unit"]:
        raise ContractError("The sample type or unit does not match its query.")
    start, end = timestamp(raw["start"]), timestamp(raw["end"])
    elapsed = end.timestamp() - start.timestamp()
    if elapsed < 0 or elapsed > 36 * 3600 or (kind in CUMULATIVE | {"sleep_analysis"} and elapsed == 0):
        raise ContractError("A sample interval has an invalid elapsed duration.")
    start_zone, end_zone = zone(raw["timezone"]), zone(raw.get("end_timezone", raw["timezone"]))
    provenance = raw.get("timezone_provenance", "recorded")
    if not isinstance(provenance, str) or provenance not in {"recorded", "utc_reference"}:
        raise ContractError("Timezone provenance must be recorded or an explicit UTC reference.")
    if provenance == "utc_reference" and (start_zone.key != "UTC" or end_zone.key != "UTC"):
        raise ContractError("An unavailable historical timezone must use the UTC reference clock.")
    if start.astimezone(start_zone).utcoffset() != start.utcoffset() or end.astimezone(end_zone).utcoffset() != end.utcoffset():
        raise ContractError("The timestamp offset disagrees with its IANA timezone.")
    source = raw["source"]
    keys(source, {"bundle_id", "name"}, {"version", "device"})
    if bundle(source["bundle_id"]) != source_bundle:
        raise ContractError("The sample source is outside the anchored query scope.")
    device = source.get("device", {})
    keys(device, set(), {"manufacturer", "model", "hardware_version", "software_version"})
    safe_device = {k: string(v, 100, empty=True) for k, v in device.items()}
    safe_source = {"bundle_id": source_bundle, "name": string(source["name"], 200),
                   "version": string(source.get("version", ""), 100, empty=True), "device": safe_device}
    stable_device = {k: v for k, v in safe_device.items() if k != "software_version"}
    source_id = hashlib.sha256(canonical([source_bundle, stable_device]).encode()).hexdigest()[:24]
    value = raw["value"]
    if kind == "sleep_analysis":
        if not isinstance(value, str) or value not in STAGES:
            raise ContractError("The sleep sample needs a supported stage.")
    else:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ContractError("A quantity sample needs a finite number.")
        try:
            finite = math.isfinite(value)
        except OverflowError:
            finite = False
        minimum, maximum = (-273.15, 100) if kind == "sleeping_wrist_temperature" else (0, 1 if kind == "oxygen_saturation" else 1e9)
        if not finite or not minimum <= value <= maximum:
            raise ContractError("A quantity sample is outside its supported unit range.")
        if kind == "step_count" and int(value) != value:
            raise ContractError("Individual step samples must contain whole counts.")
    return {"id": identifier(raw["id"]), "type": kind, "start": start.timestamp(), "end": end.timestamp(),
            "offset": int(start.utcoffset().total_seconds()), "end_offset": int(end.utcoffset().total_seconds()),
            "timezone": start_zone.key, "end_timezone": end_zone.key, "timezone_provenance": provenance, "source_id": source_id,
            "source": safe_source, "unit": raw["unit"], "value": value}


def normalize_batch(payload: Any) -> dict[str, Any]:
    try:
        encoded = canonical(payload).encode("utf-8")
    except UnicodeError as exc:
        raise ContractError("The batch must contain valid UTF-8 text.") from exc
    if len(encoded) > MAX_REQUEST_BYTES:
        raise ContractError("The companion batch exceeds the 1 MiB limit.")
    keys(payload, {"schema_version", "batch_id", "mode", "stream_id", "source_bundle_id", "generated_at", "types"})
    if type(payload["schema_version"]) is not int or payload["schema_version"] != SCHEMA_VERSION:
        raise ContractError("Unsupported companion schema version.")
    if payload["mode"] not in ("synthetic", "device"):
        raise ContractError("The batch must explicitly declare synthetic or device mode.")
    batches = payload["types"]
    if not isinstance(batches, list) or not 1 <= len(batches) <= len(CATALOG):
        raise ContractError("A batch needs a bounded list of metric queries.")
    source_bundle = bundle(payload["source_bundle_id"])
    queries, seen_types, seen_ids, operations = [], set(), set(), 0
    for item in batches:
        keys(item, {"type", "kind", "previous_anchor", "next_anchor", "read_state", "samples", "deleted_ids"}, {"error"})
        kind = item["type"]
        if not isinstance(kind, str) or kind not in CATALOG or kind in seen_types:
            raise ContractError("Duplicate, unsupported, or laboratory metric type.")
        seen_types.add(kind)
        if item["kind"] not in ("baseline", "incremental") or not isinstance(item["read_state"], str) or item["read_state"] not in READ_STATES:
            raise ContractError("Unsupported query kind or read visibility state.")
        previous, next_anchor = anchor(item["previous_anchor"]), anchor(item["next_anchor"])
        samples, deletes = item["samples"], item["deleted_ids"]
        if not isinstance(samples, list) or not isinstance(deletes, list):
            raise ContractError("Query operations must be lists.")
        operations += len(samples) + len(deletes)
        if operations > MAX_OPERATIONS:
            raise ContractError("The companion batch exceeds the 2,000 operation limit.")
        error = item.get("error")
        if error is not None and (not isinstance(error, str) or not re.fullmatch(r"[a-z0-9_]{1,64}", error)):
            raise ContractError("Read errors must be bounded codes without record details.")
        state = item["read_state"]
        if state in {"unknown", "error"}:
            if samples or deletes or previous != next_anchor or (state == "error") != (error is not None):
                raise ContractError("An unknown or failed read cannot mutate samples or advance its anchor.")
        else:
            if next_anchor is None or error is not None or (state == "nonempty") != bool(samples) or (item["kind"] == "baseline" and deletes):
                raise ContractError("Read visibility, operations and baseline anchor are inconsistent.")
        if item["kind"] == "baseline" and previous is not None:
            raise ContractError("A baseline must begin with a null anchor.")
        normalized = [normalize_sample(s, kind, source_bundle) for s in samples]
        for sample in normalized:
            sample["stream_id"] = identifier(payload["stream_id"])
            sample["source_id"] = hashlib.sha256(canonical([sample["stream_id"], sample["source_id"]]).encode()).hexdigest()[:24]
        delete_ids = [identifier(i) for i in deletes]
        query_ids = [s["id"] for s in normalized] + delete_ids
        if any(i in seen_ids for i in query_ids) or len(set(query_ids)) != len(query_ids):
            raise ContractError("Duplicate or conflicting UUID operations in one batch.")
        seen_ids.update(query_ids)
        queries.append({"type": kind, "kind": item["kind"], "previous_anchor": previous, "next_anchor": next_anchor,
                        "read_state": state, "samples": normalized, "deleted_ids": delete_ids, "error": error})
    return {"schema_version": SCHEMA_VERSION, "batch_id": identifier(payload["batch_id"]), "mode": payload["mode"],
            "stream_id": identifier(payload["stream_id"]), "source_bundle_id": source_bundle,
            "generated_at": timestamp(payload["generated_at"]).astimezone(timezone.utc).isoformat(), "types": queries,
            "request_hash": hashlib.sha256(encoded).hexdigest()}


def day_parts(start: float, end: float, tz: ZoneInfo) -> list[tuple[str, float, float]]:
    """Split elapsed UTC time at real local midnights, including 23/25-hour days."""
    if start == end:
        return [(datetime.fromtimestamp(start, tz).date().isoformat(), start, end)]
    parts = []
    cursor = start
    while cursor < end:
        local_day = datetime.fromtimestamp(cursor, tz).date()
        boundary = datetime.combine(local_day + timedelta(days=1), time(), tz).timestamp()
        if boundary <= cursor:
            raise ContractError("This timezone's midnight cannot be represented safely.")
        finish = min(end, boundary)
        parts.append((local_day.isoformat(), cursor, finish))
        cursor = finish
    return parts


def date_range(start: str, end: str) -> tuple[str, str]:
    try:
        a, b = (date.fromisoformat(start) if start else None), (date.fromisoformat(end) if end else None)
        if (a and a.isoformat() != start) or (b and b.isoformat() != end) or (a and b and (b < a or (b-a).days >= MAX_DASHBOARD_DAYS)):
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise ContractError("Choose a valid date range of at most 366 days.") from exc
    return start, end


def daily_metrics(records: list[dict[str, Any]], timezone_name: str = "UTC", start: str = "", end: str = "") -> list[dict[str, Any]]:
    tz = zone(timezone_name)
    date_range(start, end)
    groups: dict[tuple[str, str, str], list[tuple[dict[str, Any], float, float, float]]] = defaultdict(list)
    for sample in records:
        if sample["type"] == "sleep_analysis":
            continue
        kind = sample["type"]
        if kind in CUMULATIVE:
            parts = day_parts(sample["start"], sample["end"], tz)
        else:
            # An individual measurement/derived nightly value belongs to its end date;
            # never manufacture extra measurements by splitting one value at midnight.
            parts = [(datetime.fromtimestamp(sample["end"], tz).date().isoformat(), sample["start"], sample["end"])]
        for day, a, b in parts:
            if (start and day < start) or (end and day > end):
                continue
            elapsed = sample["end"] - sample["start"]
            fraction = (b-a)/elapsed if elapsed else 1.0
            groups[(day, kind, sample["source_id"])].append((sample, a, b, fraction))
    result = []
    for (day, kind, source_id), items in sorted(groups.items()):
        overlap = False
        boundary_ambiguous = False
        if kind in CUMULATIVE:
            boundary_ambiguous = any(len(day_parts(s["start"], s["end"], tz)) > 1 for s, _, _, _ in items)
            cursor = -math.inf
            instants = set()
            for _, a, b, _ in sorted(items, key=lambda x: (x[1], x[2])):
                if (b > a and a < cursor) or (a == b and a in instants):
                    overlap = True
                if a == b:
                    instants.add(a)
                cursor = max(cursor, b)
            value = None if overlap or boundary_ambiguous else sum(sample["value"] for sample, _, _, _ in items)
        else:
            value = statistics.mean(sample["value"] for sample, _, _, _ in items)
        result.append({"date": day, "type": kind, "source_id": source_id, "source": items[0][0]["source"],
                       "unit": CATALOG[kind]["unit"], "value": value, "sample_count": len(items),
                       "aggregation": CATALOG[kind]["aggregation"], "overlap": overlap,
                       "boundary_ambiguous": boundary_ambiguous,
                       "coverage": "unknown", "timezone": tz.key})
    return result


def sleep_dashboard(records: list[dict[str, Any]], mode: str, timezone_name: str, start: str, end: str,
                    settings: dict[str, Any] | None = None) -> dict[str, Any]:
    zone(timezone_name)
    date_range(start, end)
    sleep_records = [{"start": r["start"], "end": r["end"], "stage": r["value"], "source_id": r["source_id"],
                      "source": r["source"]["name"], "device": canonical(r["source"]["device"]),
                      "source_version": r["source"]["version"], "offset": r["offset"], "end_offset": r["end_offset"],
                      "timezone": r["timezone"]} for r in records if r["type"] == "sleep_analysis"]
    effective = {"session_gap_hours": 4, "target_hours": 8, **(settings or {}),
                 "timezone_mode": "fixed", "timezone": timezone_name}
    result = health.dashboard({"mode": mode, "records": sleep_records}, effective, start, end)
    # Empty or excluded-only data is unknown, not zero sleep or successful coverage.
    if not result["summary"]["nights"]:
        result["summary"]["total_minutes"] = None
    result["coverage"] = "unknown"
    return result
