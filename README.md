# Health Observatory

A local sleep and Watch dashboard with the Porcelain interface: a light workspace, an interactive five-layer map, source-aware charts and an explicit local storage boundary. Python serves the app on loopback; browser assets and Three.js are bundled locally.

## Start

Requires Python 3.11 or newer, Node.js 20 or newer for JavaScript tests, and the Python timezone database. On macOS, Python can use the system timezone database. No Python or JavaScript package installation is required.

```sh
python3 server.py --port 8888
```

Open **http://127.0.0.1:8888/**. Stop the foreground server with Ctrl+C. The included `Start Sleep Studio.command` starts the same app from its own folder. Check the selected port before starting another server.

Local files live in `.private/` by default; choose another directory with `--data-dir /absolute/path/local-data`. Directories use `0700` and stored files use `0600`. The storage is plaintext, so account access also grants access to its contents. The server binds only `127.0.0.1`, validates Host and Origin, protects writes with a per-run local token, and serves responses with `Cache-Control: no-store`. It has no enabled upload destination, remote analytics or phone transport.

## Demo and imports

The initial Sleep view uses entirely invented records dated in 2031. The Watch store starts empty. Import `fixtures/watch-demo.json` in the Watch panel to load invented Watch samples into a disposable store. Synthetic and device Watch modes cannot mix.

For a full isolated preview:

```sh
python3 tools/make_ui_preview.py --data-dir /absolute/path/new-invented-preview
python3 server.py --port 8888 --data-dir /absolute/path/new-invented-preview
```

The generator refuses to replace an existing directory. Its records, device labels, wearer example, dates, durations and gap evidence are invented. Demo-only protected windows are ignored when real sleep exports replace the demo. Explicit user-configured policies remain local.

The Sleep import accepts an Apple Health XML export or ZIP containing `export.xml`. It reads Sleep intervals only and replaces the current Sleep dataset after full validation. Preferences and annotations survive replacement. The importer streams ZIP members without extracting them and rejects external XML entities, invalid intervals and resource-limit violations. Failed imports preserve prior history. Temporary incoming raw files are removed on normal completion and graceful shutdown; a forced termination can leave a temporary `.incoming` file.

## Recorded evidence and wearer protection

Sleep duration is the union of recorded asleep intervals. In-bed time does not add sleep. Source priority resolves overlapping records; explicit awake and specific stages take precedence over unspecified sleep within one source. Missing recording spans remain unknown. Stage averages include only sessions with specific stage records, with their denominator shown. Daily charts and bedtime comparisons use the chosen clock; elapsed duration uses UTC.

Customize controls the primary wearer, personal target, session grouping, clock, visible cards and source order. Other wearers and excluded annotations stay out of summary statistics. Notes are escaped and stored locally. Annotation intervals use UTC anchors so exclusions survive clock, source and grouping changes.

For a borrowed device, configure `protected_windows` in the local `settings.json` while the server is stopped. Every grouped session overlapping a protected UTC interval is excluded regardless of source identity or ordinary annotation edits. Back up the local settings first; invalid policies are rejected when the server loads. The settings object may contain this entirely invented example:

```json
{
  "primary_owner": "You",
  "protected_windows": [{
    "start": 1932595200,
    "end": 1932681600,
    "owner": "Invented guest",
    "note": "Invented borrowed-device example",
    "reported_gaps": [{
      "start": 1932620400,
      "end": 1932621900,
      "label": "Invented recording gap; no sleep inferred"
    }]
  }]
}
```

Times are finite numeric UTC epoch seconds, with end greater than start. Gap intervals must lie within their protected window. Optional reported gaps are displayed as separate evidence and never increase sleep or stage totals. Each gap is attached to the nearest protected grouped session by UTC interval distance; actual overlap wins a distance tie, then the earlier session. This display association uses the full grouping before date filters, so changing the visible period does not move a gap to another night. Windows may overlap; distinct protected wearers remain excluded. Normal preference saves preserve these policies.

## Explore and navigate

The layer map represents application navigation, not medical hierarchy or causality. Sessions opens history; Measurements opens Watch metrics; Context expands read visibility; Preferences opens the existing settings dialog; Local boundary opens the storage explanation. Select a shell or its native button to inspect, then use Open to navigate. Keyboard selection, explicit focus, Pause, Rotate and Separate controls are available. A functional SVG fallback supports browsers without WebGL. Motion pauses when hidden or offscreen and respects reduced-motion preferences.

The map reads a limited set of already formatted dashboard labels. It does not inspect notes, wearer names, history rows, raw records or credentials.

## Useful modules

- `health.py`: bounded Sleep import, interval calculations, sources, clock handling, annotations and exclusions.
- `feed.py`: optional local additive feed validation and application; see [AUTOMATIC-EXPORT.md](AUTOMATIC-EXPORT.md).
- `watch_metrics.py` and `watch_sync.py`: validated individual samples, daily metrics, deletion/checkpoint reconciliation, retention and a disabled destination; see [WATCH-SYNC.md](WATCH-SYNC.md).
- `ios-companion/`: unsigned native iPhone source and a portable tested capture core. Device setup and transport remain separate; see [its README](ios-companion/README.md).

## Validate with synthetic inputs

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
node --test tests/*.test.cjs tests/*.test.mjs
sh ios-companion/scripts/test-synthetic.sh
```

The Swift command requires macOS Command Line Tools. Python server tests bind temporary loopback ports. JavaScript tests exercise actual modules and a minimal DOM harness; they do not establish physical touch, OS motion settings or real-device HealthKit behavior. The iOS-only adapter requires a separate full Xcode SDK build and device QA.

This publication starts from a sanitized independent initial history. Local original data, notes, source history, handovers and review packets are excluded. Bundled dependency licenses and provenance are retained in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
