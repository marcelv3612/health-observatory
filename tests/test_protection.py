"""Protection regressions using newly invented UTC sessions and local policies."""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from health import SLEEP_TYPE, dashboard, normalize
from server import DEFAULT_SETTINGS, LocalServer, Store, validate_settings
import http.client
import json
import threading


def stamp(value):
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp()


def sample(start, end, stage="Core", source="Invented watch"):
    return normalize({"type": SLEEP_TYPE, "value": "HKCategoryValueSleepAnalysisAsleep" + stage,
                      "startDate": start + " +0000", "endDate": end + " +0000",
                      "sourceName": source, "device": "Synthetic device"})


def invented_data():
    return {"mode": "demo", "records": [
        sample("2031-06-11 21:40:00", "2031-06-11 22:19:00", "Deep"),
        sample("2031-06-11 22:19:00", "2031-06-12 01:10:00"),
        sample("2031-06-12 01:10:00", "2031-06-12 02:18:00", "REM"),
        sample("2031-06-13 23:05:00", "2031-06-14 00:15:00")], "import_stats": {}}


def window(**overrides):
    return {"start": stamp("2031-06-11T12:00:00"), "end": stamp("2031-06-12T12:00:00"),
            "owner": "Invented guest", "note": "Synthetic borrowed-watch example",
            "reported_gaps": [{"start": stamp("2031-06-12T02:18:00"),
                               "end": stamp("2031-06-12T02:55:00"),
                               "label": "Invented missing recording"}], **overrides}


def settings(**overrides):
    return validate_settings({**deepcopy(DEFAULT_SETTINGS), **overrides})


class ProtectionTests(unittest.TestCase):
    def test_defaults_have_no_embedded_person_or_date_policy(self):
        self.assertEqual(DEFAULT_SETTINGS["primary_owner"], "You")
        self.assertEqual(DEFAULT_SETTINGS["timezone"], "UTC")
        self.assertEqual(DEFAULT_SETTINGS["protected_windows"], [])
        result = dashboard(invented_data(), settings())
        self.assertEqual(result["summary"]["nights"], 2)
        self.assertTrue(all(n["owner"] == "You" and not n["protected"] for n in result["nights"]))

    def test_protected_measured_totals_and_reported_gap_are_separate(self):
        result = dashboard(invented_data(), settings(protected_windows=[window()]))
        n = next(n for n in result["nights"] if n["protected"])
        self.assertEqual(n["total_minutes"], 278)
        self.assertEqual([n["minutes"][s] for s in ("deep", "core", "rem")], [39, 171, 68])
        self.assertEqual(n["reported_gaps"][0]["minutes"], 37)
        self.assertEqual(n["reported_gap"], n["reported_gaps"][0])
        self.assertEqual(n["gap_minutes"], 0)
        self.assertEqual(result["summary"]["total_minutes"], 70)
        self.assertEqual(result["summary"]["nights"], 1)
        self.assertTrue(n["excluded"])

    def test_forged_annotation_cannot_change_protected_owner_or_exclusion(self):
        policy = settings(protected_windows=[window()])
        n = next(n for n in dashboard(invented_data(), policy)["nights"] if n["protected"])
        for interval in ({"start": n["start"], "end": n["end"]},
                         {"start": n["end"] + 3600, "end": n["end"] + 7200}, {}):
            policy["annotations"] = {n["id"]: {"owner": "You", "excluded": False,
                                              "note": "Invented forged context", **interval}}
            protected = next(n for n in dashboard(invented_data(), policy)["nights"] if n["protected"])
            self.assertEqual(protected["owner"], "Invented guest")
            self.assertTrue(protected["excluded"])
            self.assertEqual(protected["note"], "Synthetic borrowed-watch example")

    def test_missing_source_identity_does_not_defeat_time_protection(self):
        data = invented_data()
        for r in data["records"]:
            r["source"], r["source_id"], r["device"] = "Unknown source", "unknown", ""
        result = dashboard(data, settings(protected_windows=[window()]))
        self.assertEqual(result["summary"]["nights"], 1)
        self.assertTrue(next(n for n in result["nights"] if n["protected"])["excluded"])

    def test_overlapping_windows_stay_protected_when_regrouped_and_timezone_changes(self):
        data = {"mode": "demo", "records": [
            sample("2031-06-21 19:10:00", "2031-06-21 20:20:00"),
            sample("2031-06-21 23:30:00", "2031-06-22 00:05:00")]}
        policies = [window(start=stamp("2031-06-21T19:30:00"), end=stamp("2031-06-21T20:00:00"), reported_gaps=[]),
                    window(start=stamp("2031-06-21T23:00:00"), end=stamp("2031-06-22T01:00:00"),
                           owner="Another invented guest", reported_gaps=[])]
        narrow = dashboard(data, settings(session_gap_hours=2, protected_windows=policies))
        self.assertEqual(len(narrow["nights"]), 2)
        self.assertTrue(all(n["protected"] and n["excluded"] for n in narrow["nights"]))
        wide = dashboard(data, settings(session_gap_hours=6, protected_windows=policies,
                                        timezone_mode="fixed", timezone="Pacific/Auckland"))
        self.assertEqual(len(wide["nights"]), 1)
        n = wide["nights"][0]
        self.assertEqual(n["owner"], "Multiple wearers")
        self.assertEqual(n["protected_owners"], ["Invented guest", "Another invented guest"])
        self.assertTrue(n["excluded"])
        utc = dashboard(data, settings(session_gap_hours=6, protected_windows=policies, timezone_mode="fixed"))
        self.assertEqual(n["id"], utc["nights"][0]["id"])
        self.assertEqual(n["total_minutes"], 105)

    def test_window_boundaries_do_not_protect_disjoint_sessions(self):
        data = invented_data()
        first = data["records"][0]
        for policy in (window(start=first["start"]-3600, end=first["start"], reported_gaps=[]),
                       window(start=stamp("2031-06-12T02:18:00"), end=stamp("2031-06-12T03:00:00"), reported_gaps=[])):
            self.assertFalse(any(n["protected"] for n in dashboard(data, settings(protected_windows=[policy]))["nights"]))

    def test_owner_comparison_uses_configured_primary_and_casefold(self):
        data = invented_data()
        initial = dashboard(data, settings(primary_owner="Current wearer"))
        n = initial["nights"][0]
        primary = settings(primary_owner="  Current wearer  ", annotations={n["id"]: {"owner": "CURRENT WEARER", "excluded": False}})
        self.assertEqual(primary["primary_owner"], "Current wearer")
        self.assertFalse(dashboard(data, primary)["nights"][0]["excluded"])
        primary["annotations"][n["id"]]["owner"] = "Guest"
        self.assertTrue(dashboard(data, primary)["nights"][0]["excluded"])
        # An explicit protected window still excludes even if its owner is primary.
        own_window = window(owner="Current wearer")
        protected = dashboard(data, settings(primary_owner="Current wearer", protected_windows=[own_window]))
        self.assertTrue(any(n["protected"] and n["excluded"] for n in protected["nights"]))

    def test_demo_windows_do_not_persist_or_follow_real_imports(self):
        data = invented_data();data["synthetic_protected_windows"] = [window()]
        policy = settings()
        demo = dashboard(data, policy)
        self.assertTrue(any(n["protected"] for n in demo["nights"]))
        self.assertEqual(demo["settings"]["protected_windows"], [])
        self.assertEqual(policy["protected_windows"], [])
        data["mode"] = "imported"
        self.assertFalse(any(n["protected"] for n in dashboard(data, policy)["nights"]))
        # Explicit local windows still apply to a real import.
        self.assertTrue(any(n["protected"] for n in dashboard(data, settings(protected_windows=[window()]))["nights"]))

    def test_settings_round_trip_preserves_policy_during_ordinary_annotation_save(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            policy = settings(primary_owner="Current wearer", protected_windows=[window()])
            store.write("settings.json", policy)
            reopened = Store(Path(directory))
            self.assertEqual(reopened.settings["protected_windows"], policy["protected_windows"])
            changed = validate_settings({**reopened.settings, "annotations": {"0123456789abcdef": {"excluded": False, "owner": "You"}}})
            reopened.write("settings.json", changed)
            self.assertEqual(Store(Path(directory)).settings["protected_windows"], policy["protected_windows"])

    def test_duplicate_gap_evidence_is_not_repeated(self):
        policy = settings(protected_windows=[window(), window(owner="Another guest")])
        n = next(n for n in dashboard(invented_data(), policy)["nights"] if n["protected"])
        self.assertEqual(len(n["reported_gaps"]), 1)
        self.assertEqual(n["total_minutes"], 278)

    def test_broad_window_gap_stays_with_nearest_session_before_date_filter(self):
        data = invented_data()
        broad = window(end=stamp("2031-06-15T12:00:00"))
        policy = settings(protected_windows=[broad])
        nights = sorted(dashboard(data, policy)["nights"], key=lambda n: n["start"])
        self.assertTrue(all(n["protected"] and n["excluded"] for n in nights))
        self.assertEqual([len(n["reported_gaps"]) for n in nights], [1, 0])
        self.assertEqual([n["total_minutes"] for n in nights], [278, 70])
        filtered = dashboard(data, policy, start_date=nights[1]["date"])
        self.assertEqual(len(filtered["nights"]), 1)
        self.assertEqual(filtered["nights"][0]["reported_gaps"], [])
        clock = dashboard(data, {**policy, "timezone_mode": "fixed", "timezone": "Pacific/Auckland"})
        self.assertEqual({n["id"]: n["reported_gaps"] for n in nights},
                         {n["id"]: n["reported_gaps"] for n in clock["nights"]})

    def test_gap_overlap_and_edge_association_recompute_when_regrouped(self):
        data = {"mode": "demo", "records": [
            sample("2031-06-21 19:00:00", "2031-06-21 20:00:00"),
            sample("2031-06-21 23:00:00", "2031-06-22 00:00:00")]}
        start, end = stamp("2031-06-21T18:00:00"), stamp("2031-06-22T01:00:00")
        gaps = [{"start": stamp("2031-06-21T20:00:00"), "end": stamp("2031-06-21T20:20:00")},
                {"start": stamp("2031-06-21T22:50:00"), "end": stamp("2031-06-21T23:15:00")}]
        broad = window(start=start, end=end, reported_gaps=gaps)
        narrow = sorted(dashboard(data, settings(session_gap_hours=2, protected_windows=[broad]))["nights"], key=lambda n: n["start"])
        self.assertEqual([len(n["reported_gaps"]) for n in narrow], [1, 1])
        wide = dashboard(data, settings(session_gap_hours=6, protected_windows=[broad]))["nights"]
        self.assertEqual(len(wide), 1)
        self.assertEqual(len(wide[0]["reported_gaps"]), 2)
        self.assertEqual(wide[0]["total_minutes"], 120)
        self.assertTrue(wide[0]["excluded"])

    def test_equal_distance_gap_tie_is_stable_and_duplicate_windows_do_not_repeat(self):
        data = {"mode": "demo", "records": [
            sample("2031-06-21 19:00:00", "2031-06-21 20:00:00"),
            sample("2031-06-21 23:00:00", "2031-06-22 00:00:00")]}
        gap = {"start": stamp("2031-06-21T21:20:00"), "end": stamp("2031-06-21T21:40:00")}
        broad = window(start=stamp("2031-06-21T18:00:00"), end=stamp("2031-06-22T01:00:00"), reported_gaps=[gap])
        policy = settings(session_gap_hours=2, protected_windows=[broad, {**broad, "owner": "Another guest"}])
        nights = sorted(dashboard(data, policy)["nights"], key=lambda n: n["start"])
        self.assertEqual([len(n["reported_gaps"]) for n in nights], [1, 0])
        self.assertTrue(all(n["protected"] and n["excluded"] for n in nights))
        data["records"].reverse()
        self.assertEqual({n["id"]: n["reported_gaps"] for n in nights},
                         {n["id"]: n["reported_gaps"] for n in dashboard(data, policy)["nights"]})

    def test_invalid_protection_settings_are_rejected(self):
        changes = [
            {"primary_owner": " "}, {"primary_owner": 1}, {"primary_owner": "x"*1001},
            {"protected_windows": {}}, {"protected_windows": [window()]*1001},
            {"protected_windows": [None]}, {"protected_windows": [window(extra=True)]},
            {"protected_windows": [window(owner=" ")]}, {"protected_windows": [window(owner=1)]},
            {"protected_windows": [window(note=3)]}, {"protected_windows": [window(note="x"*1001)]},
            {"protected_windows": [window(start=10**1000)]}, {"protected_windows": [window(start=True)]}, {"protected_windows": [window(start=float("nan"))]},
            {"protected_windows": [window(end=float("inf"))]}, {"protected_windows": [window(start="2031-06-11")]},
            {"protected_windows": [window(start=0, end=0)]}, {"protected_windows": [window(start=-62135596801)]},
            {"protected_windows": [window(end=253402300800)]},
            {"protected_windows": [window(reported_gaps={})]}, {"protected_windows": [window(reported_gaps=[{}])]},
            {"protected_windows": [window(reported_gaps=[{"start": True, "end": 3}])]},
            {"protected_windows": [window(reported_gaps=[{"start": 0, "end": 1}])]},
            {"protected_windows": [window(reported_gaps=[{**window()["reported_gaps"][0], "label": 1}])]},
            {"protected_windows": [window(reported_gaps=[{**window()["reported_gaps"][0], "other": 1}])]},
            {"protected_windows": [window(reported_gaps=window()["reported_gaps"]*101)]},
            {"protected_windows": [window(reported_gaps=window()["reported_gaps"]*100)]*11},
            {"annotations": {"0123456789abcdef": {"start": True, "end": 9}}},
            {"target_hours": 10**1000}, {"target_hours": True}, {"session_gap_hours": True},
        ]
        for change in changes:
            with self.subTest(change=change):
                with self.assertRaises(ValueError):
                    settings(**change)


class ProtectionHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name))
        self.store.dataset = invented_data()
        self.server = LocalServer(0, self.store)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.worker.join();self.temp.cleanup()

    def request(self, changes=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        headers = {}
        if changes is not None:
            headers = {"Origin": f"http://127.0.0.1:{self.server.server_port}",
                       "X-Local-Token": self.store.token, "Content-Type": "application/json"}
        connection.request("POST" if changes is not None else "GET",
                           "/api/settings" if changes is not None else "/api/state",
                           body=json.dumps(changes) if changes is not None else None, headers=headers)
        response = connection.getresponse()
        result = response.status, json.loads(response.read())
        connection.close()
        return result

    def test_annotation_post_preserves_protection_and_primary_wearer(self):
        self.assertEqual(self.request({"primary_owner": "Current wearer", "protected_windows": [window()]})[0], 200)
        status, before = self.request();self.assertEqual(status, 200)
        n = next(n for n in before["nights"] if n["protected"])
        self.assertEqual(self.request({"annotations": {n["id"]: {"owner": "Current wearer", "excluded": False,
                                                                 "start": n["start"], "end": n["end"]}}})[0], 200)
        status, after = self.request();self.assertEqual(status, 200)
        protected = next(n for n in after["nights"] if n["protected"])
        self.assertTrue(protected["excluded"])
        self.assertEqual(protected["owner"], "Invented guest")
        self.assertEqual(after["settings"]["primary_owner"], "Current wearer")
        self.assertEqual(after["settings"]["protected_windows"], before["settings"]["protected_windows"])
        self.assertEqual(Store(Path(self.temp.name)).settings["protected_windows"], before["settings"]["protected_windows"])

    def test_invalid_policy_does_not_replace_saved_settings(self):
        self.assertEqual(self.request({"protected_windows": [window()]})[0], 200)
        saved = deepcopy(self.store.settings)
        self.assertEqual(self.request({"protected_windows": [window(start=True)]})[0], 400)
        self.assertEqual(self.request({"primary_owner": " "})[0], 400)
        self.assertEqual(self.store.settings, saved)
        self.assertEqual(Store(Path(self.temp.name)).settings, saved)


if __name__ == "__main__":
    unittest.main()
