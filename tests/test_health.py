from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import health
from health import ImportError, dashboard, load_export, normalize, parse_xml, resolve, summarize
from server import DEFAULT_SETTINGS, validate_settings


def record(start="2026-09-01 23:00:00 +0900", end="2026-09-02 00:00:00 +0900", stage="AsleepCore", source="Apple Watch"):
    return normalize({"type":health.SLEEP_TYPE,"value":"HKCategoryValueSleepAnalysis"+stage,
                      "startDate":start,"endDate":end,"sourceName":source,"device":source})


def xml_record(**overrides):
    from xml.sax.saxutils import quoteattr
    attrs={"type":health.SLEEP_TYPE,"value":"HKCategoryValueSleepAnalysisAsleepCore",
           "startDate":"2026-09-01 23:00:00 +0900","endDate":"2026-09-02 00:00:00 +0900",
           "sourceName":"Apple Watch","device":"Watch"}
    attrs.update(overrides)
    return '<Record '+ ' '.join(f'{k}={quoteattr(v)}' for k,v in attrs.items())+'><MetadataEntry key="HKTimeZone" value="Asia/Tokyo"/></Record>'


class ImportTests(unittest.TestCase):
    def test_export_streams_sleep_only_preserves_zone_and_deduplicates(self):
        xml=('<HealthData>'+xml_record()+xml_record()+xml_record(type="HKQuantityTypeIdentifierHeartRate",value="65")+'</HealthData>').encode()
        records,stats=parse_xml(io.BytesIO(xml))
        self.assertEqual(len(records),1)
        self.assertEqual(stats["exact_duplicates"],1)
        self.assertEqual(stats["sleep_records"],2)
        self.assertEqual(records[0]["timezone"],"Asia/Tokyo")
        self.assertNotIn("65",json.dumps(records))

    def test_zip_import_does_not_extract_other_members(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/"export.zip"
            with zipfile.ZipFile(path,"w") as z:
                z.writestr("apple_health_export/export.xml",'<HealthData>'+xml_record()+'</HealthData>')
                z.writestr("../../outside.txt","not extracted")
                z.writestr("apple_health_export/workout-routes/route.gpx","ignored private data")
            result=load_export(path)
            self.assertEqual(len(result["records"]),1)
            self.assertEqual(list(Path(temp).iterdir()),[path])

    def test_entity_expansion_is_rejected(self):
        xml=b'<!DOCTYPE HealthData [<!ENTITY secret "EXPAND">]><HealthData>&secret;</HealthData>'
        with self.assertRaisesRegex(ImportError,"entities"):
            parse_xml(io.BytesIO(xml))

    def test_external_entity_is_rejected(self):
        xml=b'<!DOCTYPE HealthData [<!ENTITY secret SYSTEM "file:///etc/passwd">]><HealthData>&secret;</HealthData>'
        with self.assertRaisesRegex(ImportError,"entities"):
            parse_xml(io.BytesIO(xml))

    def test_apple_internal_dtd_without_entities_is_supported(self):
        records,_=parse_xml(io.BytesIO(('<!DOCTYPE HealthData [<!ELEMENT HealthData ANY>]><HealthData>'+xml_record()+'</HealthData>').encode()))
        self.assertEqual(len(records),1)

    def test_no_offset_is_never_guessed(self):
        with self.assertRaisesRegex(ImportError,"UTC offset"):
            record(start="2026-09-01 23:00:00")

    def test_negative_and_excessive_durations_are_rejected(self):
        for end in ("2026-09-01 22:00:00 +0900","2026-09-10 01:00:00 +0900"):
            with self.assertRaises(ImportError): record(end=end)

    def test_resource_limits_and_wrong_root(self):
        xml=('<HealthData>'+xml_record()+'</HealthData>').encode()
        with patch.object(health,"MAX_XML",20):
            with self.assertRaisesRegex(ImportError,"512 MiB"):parse_xml(io.BytesIO(xml))
        with patch.object(health,"MAX_SLEEP_RECORDS",0):
            with self.assertRaisesRegex(ImportError,"Too many"):parse_xml(io.BytesIO(xml))
        with self.assertRaisesRegex(ImportError,"root"):
            parse_xml(io.BytesIO(b'<Other/>'))

    def test_unsupported_values_are_counted(self):
        records,stats=parse_xml(io.BytesIO(('<HealthData>'+xml_record()+xml_record(value="FutureStage")+'</HealthData>').encode()))
        self.assertEqual(len(records),1)
        self.assertEqual(stats["unsupported_sleep_values"],1)


class SleepMathTests(unittest.TestCase):
    def test_source_identity_survives_export_pointers_and_software_updates(self):
        attrs={"type":health.SLEEP_TYPE,"value":"HKCategoryValueSleepAnalysisAsleepCore",
               "startDate":"2026-09-01 23:00:00 +0900","endDate":"2026-09-02 00:00:00 +0900",
               "sourceName":"Apple Watch"}
        a=normalize({**attrs,"device":"<HKDevice: 0x123, name:Apple Watch, manufacturer:Apple Inc., model:Watch, hardware:Watch8,1, software:9.1, localIdentifier:stable-id>"})
        b=normalize({**attrs,"device":"<HKDevice: 0x999, name:Apple Watch, manufacturer:Apple Inc., model:Watch, hardware:Watch8,1, software:10.0, localIdentifier:stable-id>"})
        c=normalize({**attrs,"device":"<HKDevice: 0x999, name:Apple Watch, manufacturer:Apple Inc., model:Watch, hardware:Watch8,1, software:10.0, localIdentifier:other-id>"})
        self.assertEqual(a['source_id'],b['source_id']);self.assertNotEqual(a['source_id'],c['source_id'])

    def test_overlap_is_a_union_and_priority_is_effective(self):
        core=record(end="2026-09-02 01:00:00 +0900")
        rem=record(start="2026-09-02 00:00:00 +0900",end="2026-09-02 02:00:00 +0900",stage="AsleepREM",source="Second watch")
        segs,overlap=resolve([core,rem],[core["source_id"],rem["source_id"]])
        self.assertEqual(overlap,60)
        self.assertEqual(sum((s['end']-s['start'])/60 for s in segs),180)
        self.assertEqual(sum((s['end']-s['start'])/60 for s in segs if s['stage']=='core'),120)
        segs,_=resolve([core,rem],[rem["source_id"],core["source_id"]])
        self.assertEqual(sum((s['end']-s['start'])/60 for s in segs if s['stage']=='core'),60)

    def test_inbed_is_not_sleep_and_gaps_are_unknown(self):
        a=record()
        b=record(start="2026-09-02 01:00:00 +0900",end="2026-09-02 02:00:00 +0900",stage="AsleepREM")
        bed=record(start="2026-09-01 22:30:00 +0900",end="2026-09-02 02:30:00 +0900",stage="InBed")
        n=dashboard({"mode":"demo","records":[a,b,bed]},DEFAULT_SETTINGS)["nights"][0]
        self.assertEqual(n["total_minutes"],120)
        self.assertEqual(n["gap_minutes"],120)
        self.assertEqual(n["minutes"]["awake"],0)
        self.assertEqual(n["minutes"]["deep"],0)

    def test_specific_stages_replace_unspecified_from_same_source(self):
        unspecified=record(end="2026-09-02 01:00:00 +0900",stage="AsleepUnspecified")
        core=record();awake=record(start="2026-09-02 00:00:00 +0900",end="2026-09-02 00:10:00 +0900",stage="Awake")
        n=dashboard({"mode":"demo","records":[unspecified,core,awake]},DEFAULT_SETTINGS)["nights"][0]
        self.assertEqual(n["total_minutes"],110)
        self.assertEqual(n["minutes"]["core"],60)
        self.assertEqual(n["minutes"]["unspecified"],50)
        self.assertEqual(n["minutes"]["awake"],10)

    def test_elapsed_duration_handles_dst_and_timezone_change(self):
        r=record(start="2026-11-01 01:30:00 -0400",end="2026-11-01 02:30:00 -0500")
        n=dashboard({"mode":"imported","records":[r]},DEFAULT_SETTINGS)["nights"][0]
        self.assertEqual(n["total_minutes"],120)
        self.assertTrue(n["travel"])
        self.assertIn('02:30',n["wake_local"])
        self.assertEqual(n["wake_timezone"],"UTC−05:00")
        fixed={**DEFAULT_SETTINGS,"timezone_mode":"fixed","timezone":"America/New_York"}
        n=dashboard({"mode":"imported","records":[r]},fixed)["nights"][0]
        self.assertIn('02:30',n["wake_local"])
        self.assertEqual(n["timezone"],"America/New_York")

    def test_bedtime_spread_wraps_midnight(self):
        nights=[{"excluded":False,"total_minutes":420,"bedtime_local":"2026-09-01T23:50:00+09:00","minutes":{"deep":0,"core":420,"rem":0,"unspecified":0},"gap_minutes":0,"timezone":"Asia/Tokyo"},
                {"excluded":False,"total_minutes":420,"bedtime_local":"2026-09-02T00:10:00+09:00","minutes":{"deep":0,"core":420,"rem":0,"unspecified":0},"gap_minutes":0,"timezone":"Asia/Tokyo"}]
        s=summarize(nights,480)
        self.assertAlmostEqual(s["bedtime_spread"],10)
        self.assertTrue(min(s["mean_bedtime"],1440-s["mean_bedtime"])<.01)

    def test_unspecified_only_nights_do_not_become_zero_stage_observations(self):
        staged=record(stage='AsleepDeep')
        unspecified=record(start='2026-09-02 23:00:00 +0900',end='2026-09-03 00:00:00 +0900',stage='AsleepUnspecified')
        s=dashboard({'mode':'demo','records':[staged,unspecified]},DEFAULT_SETTINGS)['summary']
        self.assertEqual(s['nights'],2)
        self.assertEqual(s['stage_sessions'],1)
        self.assertEqual(s['stage_averages']['deep'],60)
        self.assertEqual(s['stage_averages']['unspecified'],30)
        unknown=dashboard({'mode':'demo','records':[unspecified]},DEFAULT_SETTINGS)['summary']
        self.assertIsNone(unknown['stage_averages']['deep'])
        self.assertIsNone(unknown['stage_averages']['core'])
        self.assertEqual(unknown['stage_sessions'],0)

    def test_other_wearers_and_annotation_windows_remain_excluded(self):
        a=record(start="2031-06-11 22:10:00 +0000",end="2031-06-11 23:20:00 +0000")
        b=record(start="2031-06-12 02:40:00 +0000",end="2031-06-12 03:15:00 +0000")
        data={"mode":"demo","records":[a,b]}
        narrow={**DEFAULT_SETTINGS,"session_gap_hours":2}
        n=dashboard(data,narrow)["nights"][-1]
        annotation={"owner":"Guest","excluded":False,"start":n["start"],"end":n["end"],"note":"Invented example"}
        wide={**DEFAULT_SETTINGS,"session_gap_hours":6,"annotations":{n['id']:annotation}}
        x=dashboard(data,wide)
        self.assertTrue(x["nights"][0]["excluded"])
        self.assertEqual(x["summary"]["nights"],0)

    def test_fixed_timezone_does_not_change_duration_or_annotation_identity(self):
        data={"mode":"demo","records":[record()]}
        a=dashboard(data,DEFAULT_SETTINGS)['nights'][0]
        b=dashboard(data,{**DEFAULT_SETTINGS,"timezone_mode":"fixed","timezone":"America/New_York"})['nights'][0]
        self.assertEqual(a['total_minutes'],b['total_minutes']);self.assertEqual(a['id'],b['id'])

    def test_validation_rejects_nonfinite_and_malformed_settings(self):
        for change in ({"target_hours":float('nan')},{"timezone":"invalid/zone"},{"source_order":["a","a"]},{"widgets":{}},{"annotations":{"x":{}}}):
            with self.assertRaises(ValueError):validate_settings({**DEFAULT_SETTINGS,**change})


if __name__ == '__main__':unittest.main()
