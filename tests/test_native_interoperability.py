"""Cross-language contract regression using only the invented Swift fixture."""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
import uuid
from pathlib import Path

from watch_metrics import ContractError, normalize_batch, timestamp
from watch_sync import WatchStore


FIXTURE = Path(__file__).resolve().parents[1] / "ios-companion/Fixtures/synthetic-interoperability.json"


class NativeInteroperabilityTests(unittest.TestCase):
    def setUp(self):
        self.payload = json.loads(FIXTURE.read_text())

    def test_compiled_swift_encoder_matches_strict_receiver_contract(self):
        self.assertEqual(self.payload["mode"], "synthetic")
        query = self.payload["types"][0]
        self.assertIn("previous_anchor", query)
        self.assertIn("next_anchor", query)
        self.assertIsNone(query["previous_anchor"])
        sample = query["samples"][0]
        for value in (self.payload["batch_id"], self.payload["stream_id"], sample["id"]):
            self.assertEqual(str(uuid.UUID(value)), value)
        normalized = normalize_batch(self.payload)
        self.assertEqual(normalized["types"][0]["samples"][0]["timezone_provenance"], "utc_reference")

    def test_receiver_commits_and_replays_native_synthetic_batch_once(self):
        clock = timestamp(self.payload["generated_at"]).timestamp() + 60
        with tempfile.TemporaryDirectory() as directory:
            store = WatchStore(Path(directory) / "watch", clock=lambda: clock)
            first = store.ingest(self.payload)
            replay = store.ingest(self.payload)
            self.assertFalse(first["replayed"])
            self.assertTrue(replay["replayed"])
            dashboard = store.dashboard()
            self.assertEqual(dashboard["mode"], "synthetic")
            self.assertEqual(dashboard["historical_timezone_unknown_samples"], 1)
            self.assertEqual(len(store.pending_outbox()), 1)
            self.assertFalse(dashboard["sync"]["destination"]["enabled"])

    def test_invalid_civil_dates_from_wire_cannot_create_a_receiver_checkpoint(self):
        # These strings were accepted by the native formatter before strict
        # civil-component validation. Reject the original wire, never normalize it.
        invalid = ["2031-02-29T06:00:00Z", "2031-03-11T24:00:00Z",
                   "2031-03-11T06:60:00Z", "2031-03-11T06:00:60Z",
                   "2031-03-11T06:00:00+01:60", "2031-03-11T06:00:00+14:01",
                   "1999-12-31T23:59:59Z", "2101-01-01T00:00:00Z"]
        clock = timestamp(self.payload["generated_at"]).timestamp() + 60
        with tempfile.TemporaryDirectory() as directory:
            store = WatchStore(Path(directory) / "watch", clock=lambda: clock)
            for text in invalid:
                for field in ("generated_at", "sample"):
                    with self.subTest(timestamp=text, field=field):
                        payload = copy.deepcopy(self.payload)
                        if field == "generated_at":
                            payload[field] = text
                        else:
                            payload["types"][0]["samples"][0]["start"] = text
                            payload["types"][0]["samples"][0]["end"] = text
                        with self.assertRaises(ContractError):
                            store.ingest(payload)
                        self.assertIsNone(store.status()["last_success"])
                        self.assertEqual(store.pending_outbox(), [])


if __name__ == "__main__":
    unittest.main()
