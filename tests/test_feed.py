from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import feed
from health import ImportError, dashboard
from server import DEFAULT_SETTINGS, Store


def payload(**overrides):
    segment={"startDate":"2026-09-01 23:00:00 +0900","endDate":"2026-09-02 00:00:00 +0900",
             "qty":1.0,"value":"Core","source":"Synthetic Apple Watch DEMO"}
    segment.update(overrides)
    return {"data":{"metrics":[{"name":"sleep_analysis","units":"hr","data":[segment]},
                                 {"name":"heart_rate","units":"bpm","data":[{"qty":65}]}]}}


class FeedTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.path=self.root/'latest.json';self.store=Store(self.root/'private')
        self.path.write_text(json.dumps(payload()))

    def tearDown(self):self.temp.cleanup()

    def test_documented_segments_preserve_stage_source_offset_and_ignore_other_metrics(self):
        data=feed.load_feed(self.path)
        self.assertEqual(len(data['records']),1)
        r=data['records'][0]
        self.assertEqual(r['stage'],'core');self.assertEqual(r['offset'],9*3600)
        self.assertEqual(r['source'],'Synthetic Apple Watch DEMO')
        self.assertNotIn('heart_rate',json.dumps(data))
        self.assertEqual(dashboard(data,DEFAULT_SETTINGS)['nights'][0]['total_minutes'],60)

    def test_root_metric_array_is_also_supported(self):
        self.path.write_text(json.dumps(payload()['data']))
        self.assertEqual(len(feed.load_feed(self.path)['records']),1)

    def test_daily_summaries_missing_source_and_ambiguous_clock_are_rejected(self):
        for changes in ({'startDate':None},{'source':''},{'startDate':'2026-09-01 23:00:00'},{'value':'future-stage'},{'qty':3},{'qty':True}):
            self.path.write_text(json.dumps(payload(**changes)))
            with self.assertRaises(ImportError):feed.load_feed(self.path)
        p=payload();p['data']['metrics'][0]['data']=[{'date':'2026-09-02','deep':1,'core':4,'rem':2}]
        self.path.write_text(json.dumps(p))
        with self.assertRaisesRegex(ImportError,'summarized'):feed.load_feed(self.path)

    def test_feed_is_dry_run_until_explicit_apply_and_never_merges_demo(self):
        outcome=feed.ingest(self.path,self.store)
        self.assertFalse(outcome['applied']);self.assertFalse((self.store.directory/'sleep.json').exists())
        outcome=feed.ingest(self.path,self.store,True)
        self.assertEqual(outcome['total_records'],1)
        self.assertEqual(self.store.read('sleep.json')['mode'],'imported')

    def test_repeated_feed_is_idempotent_and_incremental_history_is_retained(self):
        feed.ingest(self.path,self.store,True)
        second=feed.ingest(self.path,self.store,True)
        self.assertEqual(second['added_records'],0)
        self.path.write_text(json.dumps(payload(startDate='2026-09-02 23:00:00 +0900',endDate='2026-09-03 00:00:00 +0900')))
        third=feed.ingest(self.path,self.store,True)
        self.assertEqual(third['added_records'],1);self.assertEqual(third['total_records'],2)
        self.assertTrue(self.store.read('feed-status.json')['last_success'])

    def test_failed_feed_preserves_existing_import_and_settings_bytes(self):
        self.store.write('settings.json',DEFAULT_SETTINGS)
        feed.ingest(self.path,self.store,True)
        before={name:(self.store.directory/name).read_bytes() for name in ('sleep.json','settings.json')}
        self.path.write_text(json.dumps(payload(source='')))
        with self.assertRaises(ImportError):feed.ingest(self.path,self.store,True)
        self.assertEqual(before,{name:(self.store.directory/name).read_bytes() for name in before})

    def test_newer_import_cannot_be_overwritten_and_live_store_refreshes(self):
        original=self.store.version('sleep.json')
        feed.ingest(self.path,self.store,True)
        with self.assertRaisesRegex(ValueError,'changed'):
            self.store.write('sleep.json',{'invalid':True},expected_version=original)
        self.store.refresh_dataset()
        self.assertEqual(len(self.store.dataset['records']),1)
        self.assertFalse(any(p.name.startswith('tmp') for p in self.store.directory.iterdir()))

    def test_json_size_guard(self):
        with patch.object(feed,'MAX_JSON',10):
            with self.assertRaisesRegex(ImportError,'8 MiB'):feed.load_feed(self.path)

    def test_watcher_waits_for_a_stable_file_and_ignores_symlinks(self):
        directory=self.root/'inbox';directory.mkdir()
        target=directory/'latest.json';target.write_bytes(self.path.read_bytes())
        (directory/'symlink.json').symlink_to(target)
        class Finished(Exception):pass
        with patch.object(feed.time,'sleep',side_effect=[None,Finished]):
            with self.assertRaises(Finished):feed.watch(directory,self.store,60)
        self.assertEqual(len(self.store.read('sleep.json')['records']),1)

    def test_watcher_rejects_oversize_without_losing_previous_success(self):
        directory=self.root/'inbox';directory.mkdir()
        (directory/'large.json').write_bytes(self.path.read_bytes())
        self.store.write('feed-status.json',{'last_success':'2026-09-01T00:00:00+00:00'})
        class Finished(Exception):pass
        with patch.object(feed,'MAX_JSON',10), patch.object(feed.time,'sleep',side_effect=[None,Finished]):
            with self.assertRaises(Finished):feed.watch(directory,self.store,60)
        status=self.store.read('feed-status.json')
        self.assertEqual(status['last_success'],'2026-09-01T00:00:00+00:00')
        self.assertIn('file-size guard',status['last_error'])
        self.assertFalse((self.store.directory/'sleep.json').exists())

    def test_watcher_retries_rejected_input_only_after_it_changes(self):
        directory=self.root/'inbox';directory.mkdir()
        (directory/'bad.json').write_text(json.dumps(payload(source='')))
        (directory/'good.json').write_bytes(self.path.read_bytes())
        class Finished(Exception):pass
        with patch.object(feed,'ingest',wraps=feed.ingest) as importer, patch.object(feed.time,'sleep',side_effect=[None,None,Finished]):
            with self.assertRaises(Finished):feed.watch(directory,self.store,60)
        self.assertEqual(importer.call_count,2)
        self.assertEqual(len(self.store.read('sleep.json')['records']),1)


if __name__=='__main__':unittest.main()
