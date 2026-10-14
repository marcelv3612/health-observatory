"""Local Apple Health sleep ingestion and transparent interval arithmetic. Stdlib only."""
from __future__ import annotations

import hashlib
import heapq
import math
import re
import statistics
import time
import zipfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import BinaryIO, Any
from xml.parsers import expat
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MAX_UPLOAD = 256 * 1024 * 1024
MAX_XML = 512 * 1024 * 1024
MAX_SLEEP_RECORDS = 250_000
MAX_PARSE_SECONDS = 45
SLEEP_TYPE = "HKCategoryTypeIdentifierSleepAnalysis"
STAGES = {
    "HKCategoryValueSleepAnalysisInBed": "inbed",
    "HKCategoryValueSleepAnalysisAsleep": "unspecified",
    "HKCategoryValueSleepAnalysisAsleepUnspecified": "unspecified",
    "HKCategoryValueSleepAnalysisAwake": "awake",
    "HKCategoryValueSleepAnalysisAsleepCore": "core",
    "HKCategoryValueSleepAnalysisAsleepDeep": "deep",
    "HKCategoryValueSleepAnalysisAsleepREM": "rem",
    "0": "inbed", "1": "unspecified", "2": "awake",
    "3": "core", "4": "deep", "5": "rem",
}
ASLEEP = {"deep", "core", "rem", "unspecified"}


class ImportError(ValueError):
    """A safe, user-visible import rejection."""


def parse_date(value: str) -> datetime:
    try:
        try:
            result = datetime.strptime(value, "%Y-%m-%d %H:%M:%S %z")
        except ValueError:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ImportError("A sleep record contains an invalid timestamp.") from exc
    if result.utcoffset() is None:
        raise ImportError("Sleep timestamps need a UTC offset; the importer will not guess.")
    return result


def normalize(attributes: dict[str, str], metadata: dict[str, str] | None = None) -> dict[str, Any] | None:
    if attributes.get("type") != SLEEP_TYPE:
        return None
    stage = STAGES.get(attributes.get("value", ""))
    if stage is None:
        return None
    start, end = parse_date(attributes.get("startDate", "")), parse_date(attributes.get("endDate", ""))
    seconds = (end - start).total_seconds()
    if seconds <= 0 or seconds > 36 * 3600:
        raise ImportError("A sleep interval has zero, negative, or more than 36 hours duration.")
    name = attributes.get("sourceName", "Unknown source")[:300]
    device = attributes.get("device", "")[:1200]
    # Exported HKDevice descriptions contain transient pointers and software versions.
    # Stable identity fields retain user priority across re-exports and OS updates.
    device_fields = dict(re.findall(r"(?:^|,\s*)(name|manufacturer|model|hardware|localIdentifier|UDID):\s*([^,>]+)", device))
    device_key = repr(sorted(device_fields.items())) if device_fields else device
    source_id = hashlib.sha256((name + "\0" + device_key).encode()).hexdigest()[:16]
    meta = metadata or {}
    tz = meta.get("HKTimeZone", "")[:100]
    return {
        "start": start.timestamp(), "end": end.timestamp(), "stage": stage,
        "source_id": source_id, "source": name, "device": device,
        "source_version": attributes.get("sourceVersion", "")[:100],
        "offset": int(start.utcoffset().total_seconds()),
        "end_offset": int(end.utcoffset().total_seconds()), "timezone": tz,
    }


def parse_xml(stream: BinaryIO) -> tuple[list[dict[str, Any]], dict[str, int]]:
    parser = expat.ParserCreate()
    records: list[dict[str, Any]] = []
    current: dict[str, str] | None = None
    metadata: dict[str, str] = {}
    seen: set[tuple[Any, ...]] = set()
    counts = {"sleep_records": 0, "exact_duplicates": 0, "unsupported_sleep_values": 0}
    root_seen = False
    depth = 0
    started = time.monotonic()

    def start(name: str, attrs: dict[str, str]) -> None:
        nonlocal current, metadata, root_seen, depth
        depth += 1
        if depth > 40:
            raise ImportError("The XML nesting limit was exceeded.")
        if not root_seen:
            if name != "HealthData":
                raise ImportError("Choose an Apple Health export whose root is HealthData.")
            root_seen = True
        if name == "Record":
            current = attrs if attrs.get("type") == SLEEP_TYPE else None
            metadata = {}
        elif name == "MetadataEntry" and current is not None:
            if attrs.get("key") == "HKTimeZone":
                metadata["HKTimeZone"] = attrs.get("value", "")

    def end(name: str) -> None:
        nonlocal current, depth
        depth -= 1
        if name != "Record" or current is None:
            return
        counts["sleep_records"] += 1
        if counts["sleep_records"] > MAX_SLEEP_RECORDS:
            raise ImportError("Too many sleep records. Limit the export to 250,000 sleep intervals.")
        result = normalize(current, metadata)
        if result is None:
            counts["unsupported_sleep_values"] += 1
        else:
            key = (result["start"], result["end"], result["stage"], result["source_id"])
            if key in seen:
                counts["exact_duplicates"] += 1
            else:
                seen.add(key)
                records.append(result)
        current = None

    def forbidden(*_: Any) -> None:
        raise ImportError("XML entities and external references are not supported.")

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.EntityDeclHandler = forbidden
    parser.ExternalEntityRefHandler = forbidden
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    total = 0
    try:
        while chunk := stream.read(64 * 1024):
            total += len(chunk)
            if total > MAX_XML:
                raise ImportError("Expanded XML exceeds the 512 MiB local resource limit.")
            if time.monotonic() - started > MAX_PARSE_SECONDS:
                raise ImportError("Import exceeded the 45-second processing limit. Try a smaller export.")
            parser.Parse(chunk, False)
        parser.Parse(b"", True)
    except expat.ExpatError as exc:
        raise ImportError("The file is incomplete or is not valid Apple Health XML.") from exc
    if not records:
        raise ImportError("No supported sleep records were found. The current dataset was kept.")
    return sorted(records, key=lambda r: (r["start"], r["end"])), counts


def load_export(path: Path) -> dict[str, Any]:
    if path.stat().st_size > MAX_UPLOAD:
        raise ImportError("File exceeds the 256 MiB import limit.")
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                members = archive.infolist()
                if len(members) > 10_000:
                    raise ImportError("The archive has too many entries.")
                candidates = [i for i in members if Path(i.filename).name == "export.xml" and not i.is_dir()]
                if len(candidates) != 1:
                    raise ImportError("The ZIP must contain exactly one export.xml.")
                info = candidates[0]
                if info.file_size > MAX_XML:
                    raise ImportError("Expanded export.xml exceeds 512 MiB.")
                if info.flag_bits & 1:
                    raise ImportError("Encrypted ZIP files are not supported.")
                # Stream one member. Never extract filenames or routes, workouts, ECGs, or other health data.
                with archive.open(info) as stream:
                    records, counts = parse_xml(stream)
        else:
            with path.open("rb") as stream:
                records, counts = parse_xml(stream)
    except (zipfile.BadZipFile, RuntimeError) as exc:
        raise ImportError("The ZIP is damaged or uses an unsupported encoding.") from exc
    return {"mode": "imported", "records": records, "import_stats": counts,
            "imported_at": datetime.now(timezone.utc).isoformat()}


def sources_for(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: dict[str, dict[str, Any]] = {}
    for r in records:
        item = items.setdefault(r["source_id"], {
            "id": r["source_id"], "name": r["source"], "device": r["device"], "records": 0,
            "watch": "watch" in (r["source"] + " " + r["device"]).lower(),
        })
        item["records"] += 1
    return sorted(items.values(), key=lambda x: (not x["watch"], x["name"], x["id"]))


def resolve(records: list[dict[str, Any]], source_order: list[str]) -> tuple[list[dict[str, Any]], float]:
    """Sweep-line union; each UTC instant contributes at most once to total sleep.

    In-bed records describe opportunity, not stages. A separate union retains them.
    Priority: preferred source first; within source, awake then specific stage then unspecified.
    Same-priority stage conflicts are deterministic and are surfaced, never double-counted.
    """
    rank = {sid: i for i, sid in enumerate(source_order)}
    stage_rank = {"awake": 0, "deep": 1, "core": 1, "rem": 1, "unspecified": 2}
    events: dict[float, list[tuple[bool, int]]] = defaultdict(list)
    for i, r in enumerate(records):
        if r["stage"] == "inbed":
            continue
        events[r["start"]].append((True, i))
        events[r["end"]].append((False, i))
    active: set[int] = set()
    heap: list[tuple[Any, ...]] = []
    segments: list[dict[str, Any]] = []
    overlap = 0.0
    points = sorted(events)
    for p, next_p in zip(points, points[1:]):
        for begins, i in events[p]:
            if begins:
                active.add(i)
                r = records[i]
                heapq.heappush(heap, (rank.get(r["source_id"], len(rank)), stage_rank[r["stage"]],
                                      r["stage"], r["start"], r["end"], i))
            else:
                active.discard(i)
        while heap and heap[0][-1] not in active:
            heapq.heappop(heap)
        if not heap:
            continue
        chosen = records[heap[0][-1]]
        if len(active) > 1:
            overlap += next_p - p
        segment = {"start": p, "end": next_p, "stage": chosen["stage"],
                   "source_id": chosen["source_id"], "source": chosen["source"]}
        if segments and all(segments[-1][k] == segment[k] for k in ("stage", "source_id")) and segments[-1]["end"] == p:
            segments[-1]["end"] = next_p
        else:
            segments.append(segment)
    return segments, overlap / 60


def union(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    result: list[tuple[float, float]] = []
    for start, end in sorted(intervals):
        if result and start <= result[-1][1]:
            result[-1] = (result[-1][0], max(end, result[-1][1]))
        else:
            result.append((start, end))
    return result


def zone_for(record: dict[str, Any], settings: dict[str, Any]) -> tuple[Any, str]:
    if settings.get("timezone_mode") == "fixed":
        name = settings.get("timezone", "UTC")
        try:
            return ZoneInfo(name), name
        except ZoneInfoNotFoundError:
            pass
    if record.get("timezone"):
        try:
            return ZoneInfo(record["timezone"]), record["timezone"]
        except ZoneInfoNotFoundError:
            pass
    offset = int(record["offset"])
    sign = "+" if offset >= 0 else "−"
    label = f"UTC{sign}{abs(offset)//3600:02}:{abs(offset)%3600//60:02}"
    return timezone(timedelta(seconds=offset)), label


def night_from(records: list[dict[str, Any]], settings: dict[str, Any]) -> dict[str, Any]:
    source_order = settings.get("source_order") or [s["id"] for s in sources_for(records)]
    segments, overlap = resolve(records, source_order)
    asleep = [s for s in segments if s["stage"] in ASLEEP]
    # Opportunity is the span of reported in-bed/observed records; no invented sleep at either edge.
    start, end = min(r["start"] for r in records), max(r["end"] for r in records)
    bedtime = asleep[0]["start"] if asleep else start
    wake = asleep[-1]["end"] if asleep else end
    first = next((r for r in records if asleep and r["source_id"] == asleep[0]["source_id"] and
                  r["start"] <= bedtime < r["end"] and r["stage"] == asleep[0]["stage"]),
                 min(records, key=lambda r: r["start"]))
    last = next((r for r in records if asleep and r["source_id"] == asleep[-1]["source_id"] and
                 r["start"] < wake <= r["end"] and r["stage"] == asleep[-1]["stage"]),
                max(records, key=lambda r: r["end"]))
    zone, zone_label = zone_for(first, settings)
    wake_zone, wake_zone_label = zone_for({**last, "offset": last["end_offset"]}, settings)
    local_start = datetime.fromtimestamp(bedtime, zone)
    day = (local_start + timedelta(days=1)).date() if local_start.hour >= 12 else local_start.date()
    # Anchored in UTC, so timezone/source preferences do not move an exclusion to another night.
    nid = hashlib.sha256(str(int(start)).encode()).hexdigest()[:16]
    minutes = {stage: sum((s["end"] - s["start"]) / 60 for s in segments if s["stage"] == stage)
               for stage in ("deep", "core", "rem", "unspecified", "awake")}
    covered = union([(s["start"], s["end"]) for s in segments])
    gaps: list[dict[str, Any]] = []
    cursor = start
    for a, b in covered:
        if a > cursor:
            gaps.append({"start": cursor, "end": a, "minutes": (a-cursor)/60, "kind": "unclassified"})
        cursor = max(cursor, b)
    if cursor < end:
        gaps.append({"start": cursor, "end": end, "minutes": (end-cursor)/60, "kind": "unclassified"})
    # Protection is an explicit local policy, independent of ordinary annotations,
    # source identity and display timezones. Any grouped overlap remains excluded.
    protection = [w for w in settings.get("protected_windows", [])
                  if w["start"] < end and w["end"] > start]
    primary_owner = settings.get("primary_owner", "You")
    annotations = settings.get("annotations", {})
    matching = [a for a in annotations.values() if a.get("start", math.inf) < end and a.get("end", -math.inf) > start]
    annotation = annotations.get(nid) or (matching[0] if matching else {})
    protected_owners = list(dict.fromkeys(w["owner"] for w in protection))
    owner = (protected_owners[0] if len(protected_owners) == 1 else "Multiple wearers") if protection else annotation.get("owner", primary_owner)
    excluded = bool(protection) or owner.casefold() != primary_owner.casefold() or bool(annotation.get("excluded")) or any(
        a.get("excluded") or a.get("owner", primary_owner).casefold() != primary_owner.casefold() for a in matching)
    reason = " · ".join(dict.fromkeys(w.get("note", "Protected borrowed-watch window") for w in protection)) if protection else annotation.get("note", "")
    offset_set = {r["offset"] for r in records} | {r["end_offset"] for r in records}
    span = (end-start)/60
    return {
        "id": nid, "date": str(day), "start": start, "end": end, "bedtime": bedtime, "wake": wake,
        "bedtime_local": local_start.isoformat(), "wake_local": datetime.fromtimestamp(wake, wake_zone).isoformat(),
        "wake_timezone": wake_zone_label,
        "timezone": zone_label, "offset": int(local_start.utcoffset().total_seconds()),
        "travel": len(offset_set) > 1, "record_offsets": sorted(offset_set),
        "minutes": minutes, "total_minutes": sum(minutes[s] for s in ASLEEP),
        "span_minutes": span, "gaps": gaps, "gap_minutes": sum(g["minutes"] for g in gaps),
        "reported_gap": None, "reported_gaps": [], "overlap_minutes": overlap, "records": len(records),
        "segments": segments, "inbed": union([(r["start"], r["end"]) for r in records if r["stage"] == "inbed"]),
        "sources": sorted({r["source"] for r in records}), "excluded": excluded,
        "protected": bool(protection), "protected_owners": protected_owners, "owner": owner, "note": reason, "annotation": annotation,
    }


def bind_reported_gaps(nights: list[dict[str, Any]], settings: dict[str, Any]) -> None:
    # Assign display context across the full grouping before date filtering. An
    # edge gap belongs to the nearest protected session, never every night in a
    # broad policy. UTC distance, overlap and stable start order are display rules;
    # reported evidence still contributes no recorded sleep or stage minutes.
    candidates: dict[tuple[float, float, str], dict[str, dict[str, Any]]] = {}
    for window in settings.get("protected_windows", []):
        eligible = [n for n in nights if window["start"] < n["end"] and window["end"] > n["start"]]
        for gap in window.get("reported_gaps", []):
            key = (gap["start"], gap["end"], gap.get("label", "User-reported recording gap"))
            candidates.setdefault(key, {}).update({n["id"]: n for n in eligible})
    for (start, end, label), eligible in sorted(candidates.items()):
        if not eligible:
            continue
        def rank(n: dict[str, Any]) -> tuple[float, float, float, str]:
            distance = max(n["start"] - end, start - n["end"], 0)
            overlap = max(0, min(end, n["end"]) - max(start, n["start"]))
            return distance, -overlap, n["start"], n["id"]
        selected = min(eligible.values(), key=rank)
        selected["reported_gaps"].append({"start": start, "end": end, "label": label,
                                          "minutes": (end - start) / 60})
    for night in nights:
        night["reported_gap"] = night["reported_gaps"][0] if night["reported_gaps"] else None


def summarize(nights: list[dict[str, Any]], target_minutes: float) -> dict[str, Any]:
    included = [n for n in nights if not n["excluded"] and n["total_minutes"] > 0]
    staged = [n for n in included if any(n["minutes"][s] > 0 for s in ("deep", "core", "rem"))]
    avg = lambda values: statistics.mean(values) if values else None
    bedtimes = [datetime.fromisoformat(n["bedtime_local"]).hour*60 +
                datetime.fromisoformat(n["bedtime_local"]).minute for n in included]
    # Circular local-clock mean and deviations handle midnight without a false 12-hour spread.
    if bedtimes:
        angles = [m/1440*2*math.pi for m in bedtimes]
        x, y = avg([math.cos(a) for a in angles]), avg([math.sin(a) for a in angles])
        if math.hypot(x, y) < 1e-6:
            mean_bed, spread = None, None
        else:
            mean_bed = (math.atan2(y, x) % (2*math.pi))*1440/(2*math.pi)
            deviations = [((m-mean_bed+720) % 1440)-720 for m in bedtimes]
            spread = math.sqrt(avg([d*d for d in deviations])) if len(included) >= 2 else None
    else:
        mean_bed, spread = None, None
    return {
        "nights": len(included), "excluded": sum(n["excluded"] for n in nights),
        "average_minutes": avg([n["total_minutes"] for n in included]),
        "total_minutes": sum(n["total_minutes"] for n in included),
        "target_met": sum(n["total_minutes"] >= target_minutes for n in included),
        "mean_bedtime": mean_bed, "bedtime_spread": spread,
        "gap_minutes": sum(n["gap_minutes"] for n in included),
        "stage_averages": {**{s: avg([n["minutes"][s] for n in staged]) for s in ("deep", "core", "rem")},
                           "unspecified": avg([n["minutes"]["unspecified"] for n in included])},
        "stage_sessions": len(staged),
        "stage_incomplete": sum(n["gap_minutes"] > 0 or n["minutes"]["unspecified"] > 0 for n in staged),
        "zones": sorted({n["timezone"] for n in included}),
    }


def dashboard(dataset: dict[str, Any], settings: dict[str, Any], start_date: str = "", end_date: str = "") -> dict[str, Any]:
    records = dataset["records"]
    all_sources = sources_for(records)
    order = settings.get("source_order", [])
    effective_settings = {**settings, "source_order": [sid for sid in order if any(s["id"] == sid for s in all_sources)] +
                          [s["id"] for s in all_sources if s["id"] not in order]}
    clusters: list[list[dict[str, Any]]] = []
    cluster_end = 0.0
    for r in sorted(records, key=lambda r: r["start"]):
        if not clusters or r["start"] - cluster_end > settings.get("session_gap_hours", 4)*3600:
            clusters.append([r])
            cluster_end = r["end"]
        else:
            clusters[-1].append(r)
            cluster_end = max(cluster_end, r["end"])
    # Invented example windows belong to the demo dataset, never persisted settings.
    # Real imports cannot inherit those examples or activate their metadata.
    grouping_settings = effective_settings
    if dataset.get("mode") == "demo":
        grouping_settings = {**effective_settings, "protected_windows":
                             settings.get("protected_windows", []) + dataset.get("synthetic_protected_windows", [])}
    nights = [night_from(c, grouping_settings) for c in clusters]
    bind_reported_gaps(nights, grouping_settings)
    if len(nights) > 20_000:
        raise ImportError("More than 20,000 sleep sessions exceeds the dashboard limit.")
    nights = sorted([n for n in nights if (not start_date or n["date"] >= start_date) and
                     (not end_date or n["date"] <= end_date)], key=lambda n: n["start"], reverse=True)
    return {"mode": dataset["mode"], "sources": all_sources, "settings": effective_settings,
            "nights": nights, "summary": summarize(nights, settings.get("target_hours", 8)*60),
            "import_stats": dataset.get("import_stats", {}), "imported_at": dataset.get("imported_at"),
            "record_count": len(records), "date_bounds": {
                "start": min((n["date"] for n in nights), default=""),
                "end": max((n["date"] for n in nights), default="")}}
