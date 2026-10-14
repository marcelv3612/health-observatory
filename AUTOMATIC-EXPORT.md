# Optional local incremental Sleep feed

`feed.py` validates or applies a bounded local Sleep feed. It does not read a phone, install an app, configure a Shortcut, enable a cloud connection or start automatically at login. Delivery of a real feed remains a separate user-configured step.

## Commands

Validate a local file without changing history:

```sh
python3 feed.py --file /absolute/path/latest-sleep.json
```

Apply a validated local feed to an explicitly selected store:

```sh
python3 feed.py --file /absolute/path/latest-sleep.json --data-dir /absolute/path/local-data --apply
```

Opt in to watching a dedicated local folder:

```sh
python3 feed.py --watch-folder /absolute/path/SleepFeed --data-dir /absolute/path/local-data --apply --interval 60
```

These are foreground commands; Ctrl+C stops them. They do not create login jobs. The running dashboard refreshes local changes while visible and outside an active edit.

## Input and reconciliation

The importer accepts the existing Sleep XML/ZIP subset or unaggregated Health Auto Export JSON with individual start/end, stage and source fields. It preserves original timestamp offsets and available metadata; absent source identity or historical timezone is not manufactured. Daily totals and CSV cannot establish stage chronology and are unsupported.

The full input is validated before an atomic write. Source/stage/UTC intervals merge additively and idempotently. Concurrent manual imports are detected so a feed cannot overwrite newer history. Preferences and annotations remain separate. This path cannot reconcile deletions or corrections to old intervals; periodic full Sleep exports are the reconciliation path. The individual UUID/anchor Watch contract is separate.

Guards include8MiB JSON,250,000 merged intervals,1,000 folder entries,10files per poll, a60second minimum interval, nonrecursive scanning and no symlinks. Files must be unchanged for one full poll before reading. Existing XML/ZIP bounds also apply. Failed reads preserve prior data and success status.

Any phone/cloud/LAN delivery route must be chosen and configured independently. Running this importer does not authorize a recipient or make the loopback server reachable from a phone.
