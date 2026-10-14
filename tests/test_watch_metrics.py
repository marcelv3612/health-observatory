"""Entirely invented companion records; no device data, exports or networking."""
from __future__ import annotations

import copy
import unittest
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from watch_metrics import (CATALOG, ContractError, daily_metrics, day_parts, normalize_batch,
                           sleep_dashboard, timestamp)


def uid(label: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, "invented-watch-test-" + label))


def sample(kind="heart_rate", value=67, *, label="sample", start="2031-03-10T10:00:00Z",
           end="2031-03-10T10:00:30Z", tz="UTC", source_bundle="com.example.synthetic.watch", **extra):
    return {"id": uid(label), "type": kind, "start": start, "end": end, "timezone": tz,
            "source": {"bundle_id": source_bundle, "name": "Invented Watch", "version": "1.0",
                       "device": {"manufacturer": "Synthetic", "model": "Imaginary model", "hardware_version": "A"}},
            "unit": CATALOG[kind]["unit"], "value": value, **extra}


def query(kind="heart_rate", samples=None, *, previous=None, next_anchor="anchor-1", read_state=None, deleted=None,
          error=None, query_kind=None):
    values = [sample(kind)] if samples is None else samples
    return {"type": kind, "kind": query_kind or ("baseline" if previous is None else "incremental"),
            "previous_anchor": previous, "next_anchor": next_anchor,
            "read_state": read_state or ("nonempty" if values else "empty"),
            "samples": values, "deleted_ids": deleted or [], "error": error}


def batch(queries=None, *, label="batch", mode="synthetic", stream="stream", source_bundle="com.example.synthetic.watch"):
    return {"schema_version": 1, "batch_id": uid(label), "mode": mode, "stream_id": uid(stream),
            "source_bundle_id": source_bundle, "generated_at": "2031-03-11T12:00:00Z", "types": queries or [query()]}


def normalized(*samples):
    kinds = {}
    for item in samples:
        kinds.setdefault(item["type"], []).append(item)
    return [s for q in normalize_batch(batch([query(k, v) for k, v in kinds.items()]))["types"] for s in q["samples"]]


class ContractTests(unittest.TestCase):
    def test_unknown_historical_timezone_uses_explicit_utc_reference(self):
        plain = normalize_batch(batch())["types"][0]["samples"][0]
        self.assertEqual(plain["timezone_provenance"], "recorded")
        raw = sample(timezone_provenance="utc_reference")
        accepted = normalize_batch(batch([query(samples=[raw])]))["types"][0]["samples"][0]
        self.assertEqual(accepted["timezone_provenance"], "utc_reference")
        self.assertEqual(accepted["timezone"], "UTC")
        for provenance in (None, {}, "current_phone_timezone"):
            bad = sample(timezone_provenance=provenance)
            with self.assertRaises(ContractError): normalize_batch(batch([query(samples=[bad])]))
        bad = sample(tz="Europe/Prague", start="2031-03-10T10:00:00+01:00",
                     end="2031-03-10T10:01:00+01:00", timezone_provenance="utc_reference")
        with self.assertRaises(ContractError): normalize_batch(batch([query(samples=[bad])]))

    def test_catalog_units_all_supported_metrics_and_hrv_label(self):
        values = {k: ("core" if k == "sleep_analysis" else .97 if k == "oxygen_saturation" else 31.4 if k == "sleeping_wrist_temperature" else 42) for k in CATALOG}
        parsed = normalize_batch(batch([query(k, [sample(k, v, label=k)]) for k, v in values.items()]))
        self.assertEqual(len(parsed["types"]), 11)
        self.assertIn("SDNN", CATALOG["hrv_sdnn"]["label"])
        self.assertIn("absolute", CATALOG["sleeping_wrist_temperature"]["label"])
        self.assertEqual(parsed["types"][0]["samples"][0]["id"], uid("sleep_analysis"))

    def test_laboratory_metrics_and_statistics_aggregates_are_rejected(self):
        bad = batch()
        bad["types"][0]["type"] = "blood_glucose"
        with self.assertRaises(ContractError): normalize_batch(bad)
        bad = batch(); bad["types"][0]["samples"][0]["aggregation"] = "daily_sum"
        with self.assertRaises(ContractError): normalize_batch(bad)

    def test_rejects_unbounded_or_unsupported_metadata(self):
        for field, value in (("location", "private"), ("metadata", {"workout_route": "private"}), ("extra", "private")):
            bad = batch(); bad["types"][0]["samples"][0][field] = value
            with self.assertRaises(ContractError): normalize_batch(bad)
        bad = batch(); bad["types"][0]["samples"][0]["source"]["device"]["serial_number"] = "invented"
        with self.assertRaises(ContractError): normalize_batch(bad)
        bad = batch(); bad["types"][0]["next_anchor"] = "x" * 2049
        with self.assertRaises(ContractError): normalize_batch(bad)

    def test_units_finite_values_fraction_and_original_uuid_validation(self):
        for value in (float("nan"), float("inf"), -1, True, "67", 10 ** 400):
            with self.subTest(value_type=type(value).__name__), self.assertRaises(ContractError):
                normalize_batch(batch([query(samples=[sample(value=value)])]))
        for changes in ({"unit": "bpm"}, {"id": "surrogate-1"}, {"id": str(uuid.UUID(int=0))}):
            bad = batch(); bad["types"][0]["samples"][0].update(changes)
            with self.assertRaises(ContractError): normalize_batch(bad)
        for kind, value in (("oxygen_saturation", 97), ("step_count", 1.5)):
            with self.assertRaises(ContractError): normalize_batch(batch([query(kind, [sample(kind, value)])]))

    def test_offsets_timezone_duration_and_travel_are_explicit(self):
        for changes in ({"start": "2031-03-10T10:00:00"}, {"timezone": "Invalid/Zone"},
                        {"start": "2031-03-10T10:00:00+01:00"}, {"end": "2031-03-09T09:00:00Z"},
                        {"end": "2031-03-12T10:00:00Z"}):
            bad = sample(); bad.update(changes)
            with self.assertRaises(ContractError): normalize_batch(batch([query(samples=[bad])]))
        travel = sample(start="2031-03-10T10:00:00+01:00", end="2031-03-10T10:30:00Z", tz="Europe/Prague", end_timezone="UTC")
        item = normalized(travel)[0]
        self.assertEqual(item["offset"], 3600); self.assertEqual(item["end_offset"], 0)
        self.assertEqual(item["end"] - item["start"], 5400)
        with self.assertRaises(ContractError): timestamp("NaN")

    def test_read_visibility_cannot_advance_on_unknown_or_error(self):
        for state, error in (("unknown", None), ("error", "read_failed")):
            valid = query(samples=[], next_anchor=None, read_state=state, error=error)
            self.assertEqual(normalize_batch(batch([valid]))["types"][0]["read_state"], state)
            bad = copy.deepcopy(valid); bad["next_anchor"] = "advance"
            with self.assertRaises(ContractError): normalize_batch(batch([bad]))
        for invalid in (query(samples=[], read_state="nonempty"), query(read_state="empty"),
                        query(samples=[], read_state=["error"]), query(samples=[], read_state="denied")):
            with self.assertRaises(ContractError): normalize_batch(batch([invalid]))

    def test_duplicate_uuid_or_type_operations_and_source_escape_are_rejected(self):
        for queries in ([query(samples=[sample(), sample()])], [query(), query()],
                        [query(samples=[sample(source_bundle="com.example.other")])],
                        [query(samples=[], deleted=[uid("gone")])]):
            with self.assertRaises(ContractError): normalize_batch(batch(queries))
        bad = batch([query(previous="a", deleted=[uid("sample")])])
        with self.assertRaises(ContractError): normalize_batch(bad)

    def test_version_size_and_mode_are_bounded(self):
        for changes in ({"schema_version": True}, {"schema_version": 2}, {"mode": "demo"}, {"extra": 1}, {"batch_id": "x"}):
            bad = batch(); bad.update(changes)
            with self.assertRaises(ContractError): normalize_batch(bad)
        bad = batch(); bad["extra"] = "x" * 1048576
        with self.assertRaises(ContractError): normalize_batch(bad)

    def test_adversarial_json_types_and_unicode_fail_with_safe_contract_errors(self):
        for field in ("schema_version", "batch_id", "mode", "stream_id", "source_bundle_id", "generated_at", "types"):
            for value in ([], {}, True, None, "\ud800"):
                bad = batch(); bad[field] = value
                with self.subTest(field=field, value_type=type(value).__name__), self.assertRaises(ContractError):
                    normalize_batch(bad)
        for field in ("type", "kind", "previous_anchor", "next_anchor", "read_state", "samples", "deleted_ids", "error"):
            for value in ([], {}):
                if field == "deleted_ids" and value == []:
                    continue  # This is the contract's valid no-deletions representation.
                bad = batch(); bad["types"][0][field] = value
                with self.subTest(field=field), self.assertRaises(ContractError): normalize_batch(bad)


class MetricSummaryTests(unittest.TestCase):
    def test_midnight_and_dst_day_lengths_are_elapsed_utc(self):
        tz = ZoneInfo("America/New_York")
        for day, next_day, hours in (("2031-03-09", "2031-03-10", 23), ("2031-11-02", "2031-11-03", 25)):
            start = datetime.fromisoformat(day).replace(tzinfo=tz).timestamp()
            end = datetime.fromisoformat(next_day).replace(tzinfo=tz).timestamp()
            parts = day_parts(start, end, tz)
            self.assertEqual(len(parts), 1); self.assertEqual((parts[0][2]-parts[0][1])/3600, hours)
        parts = day_parts(timestamp("2031-03-10T23:00:00Z").timestamp(), timestamp("2031-03-11T01:00:00Z").timestamp(), ZoneInfo("UTC"))
        self.assertEqual([p[0] for p in parts], ["2031-03-10", "2031-03-11"])

    def test_cumulative_samples_do_not_double_sum_sources_or_overlaps(self):
        one = sample("step_count", 100, start="2031-03-10T10:00:00Z", end="2031-03-10T11:00:00Z", label="one")
        two = sample("step_count", 200, start="2031-03-10T11:00:00Z", end="2031-03-10T12:00:00Z", label="two")
        result = daily_metrics(normalized(one, two))
        self.assertEqual(result[0]["value"], 300)
        two["start"] = "2031-03-10T10:30:00Z"
        self.assertIsNone(daily_metrics(normalized(one, two))[0]["value"])
        two["source"]["device"]["model"] = "Another imaginary model"
        result = daily_metrics(normalized(one, two))
        self.assertEqual(len(result), 2); self.assertEqual(sorted(r["value"] for r in result), [100, 200])

    def test_cross_midnight_cumulative_distribution_is_unknown(self):
        one = sample("step_count", 1000, start="2031-03-10T23:00:00Z", end="2031-03-11T01:00:00Z")
        rows = daily_metrics(normalized(one))
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["boundary_ambiguous"] and r["value"] is None for r in rows))
        measurement = sample("resting_heart_rate", 55, start=one["start"], end=one["end"])
        rows = daily_metrics(normalized(measurement))
        self.assertEqual(len(rows), 1); self.assertEqual(rows[0]["value"], 55)

    def test_recorded_zero_is_not_missing_and_sample_mean_is_labelled(self):
        self.assertEqual(daily_metrics(normalized(sample("step_count", 0)))[0]["value"], 0)
        records = normalized(sample(value=60), sample(value=80, label="second"))
        row = daily_metrics(records)[0]
        self.assertEqual(row["value"], 70); self.assertEqual(row["aggregation"], "sample_mean")
        self.assertEqual(daily_metrics([]), [])

    def test_cumulative_points_are_rejected_inside_and_at_interval_boundaries(self):
        for kind in ("step_count", "active_energy", "apple_exercise_time"):
            interval = sample(kind, 100, start="2031-03-10T10:00:00Z", end="2031-03-10T11:00:00Z")
            for point in ("2031-03-10T10:00:00Z", "2031-03-10T10:30:00Z", "2031-03-10T11:00:00Z"):
                value = sample(kind, 20, label="point", start=point, end=point)
                with self.subTest(kind=kind, point=point), self.assertRaises(ContractError):
                    normalize_batch(batch([query(kind, [interval, value])]))
            measured_zero = sample(kind, 0)
            self.assertEqual(daily_metrics(normalized(measured_zero))[0]["value"], 0)
        # Point measurements remain valid for discrete non-cumulative quantities.
        heart = sample(start="2031-03-10T10:30:00Z", end="2031-03-10T10:30:00Z")
        self.assertEqual(daily_metrics(normalized(heart))[0]["value"], 67)

    def test_sleep_union_stage_totals_and_empty_unknown(self):
        core = sample("sleep_analysis", "core", start="2031-03-10T23:00:00Z", end="2031-03-11T01:00:00Z")
        unspecified = sample("sleep_analysis", "unspecified", label="unspecified", start=core["start"], end="2031-03-11T02:00:00Z")
        result = sleep_dashboard(normalized(core, unspecified), "synthetic", "UTC", "", "")
        self.assertEqual(result["summary"]["total_minutes"], 180)
        self.assertEqual(result["nights"][0]["minutes"]["core"], 120)
        self.assertEqual(result["nights"][0]["minutes"]["unspecified"], 60)
        result = sleep_dashboard([], "empty", "UTC", "", "")
        self.assertIsNone(result["summary"]["total_minutes"]); self.assertIsNone(result["summary"]["average_minutes"])

    def test_sleep_dst_and_circular_bedtime_regularity(self):
        dst = sample("sleep_analysis", "core", start="2031-03-09T00:00:00-05:00", end="2031-03-09T08:00:00-04:00", tz="America/New_York")
        result = sleep_dashboard(normalized(dst), "synthetic", "America/New_York", "", "")
        self.assertEqual(result["summary"]["total_minutes"], 420)
        a = sample("sleep_analysis", "core", label="night-a", start="2031-03-10T23:50:00Z", end="2031-03-11T06:00:00Z")
        b = sample("sleep_analysis", "core", label="night-b", start="2031-03-12T00:10:00Z", end="2031-03-12T06:00:00Z")
        result = sleep_dashboard(normalized(a, b), "synthetic", "UTC", "", "")
        self.assertAlmostEqual(result["summary"]["bedtime_spread"], 10)

    def test_range_is_bounded_and_explicit_exclusions_are_honored(self):
        with self.assertRaises(ContractError): daily_metrics([], start="2030-01-01", end="2032-01-01")
        with self.assertRaises(ContractError): daily_metrics([], timezone_name="Invalid/Zone")
        records = normalized(sample("sleep_analysis", "core"))
        settings = {"annotations": {"invented": {"start": records[0]["start"], "end": records[0]["end"], "excluded": True}}}
        result = sleep_dashboard(records, "synthetic", "UTC", "", "", settings)
        self.assertEqual(result["summary"]["excluded"], 1)
        self.assertIsNone(result["summary"]["total_minutes"])


if __name__ == "__main__":
    unittest.main()
