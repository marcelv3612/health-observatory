# Watch samples and native companion

The Watch panel uses strict individual-sample batches, a local SQLite store and source-aware daily summaries. It preserves sample UUIDs, types, original offset-aware timestamps, units, source identity and available timezone provenance. Sleep calculations reuse the Sleep interval engine and explicitly configured wearer exclusions.

## Local workflow

```sh
python3 server.py --port 8888 --data-dir /absolute/path/disposable-watch-store
```

The Watch panel starts empty. Import `fixtures/watch-demo.json` for invented samples. A batch declares `synthetic` or `device`, and a store cannot mix these modes. Watch values are separate from `sleep.json`.

The catalog covers Sleep stages, heart rate, resting heart rate, HRV SDNN, steps, active energy, exercise time, respiratory rate, oxygen saturation, sleeping wrist temperature and VO₂ max. Displayed SDNN remains SDNN, oxygen saturation is a fraction in the contract, and wrist temperature is absolute wrist temperature. Empty data, unknown read visibility, read errors, zero samples and excluded Sleep sessions retain separate meanings. Values describe recorded samples and do not provide a diagnosis.

## Contract and recovery

Version1 batches allow at most 1 MiB and 2,000 sample/deletion operations. Per-type scopes are fixed by stream/source/type. The initial baseline uses a null previous anchor. Incremental updates must match the stored anchor; unknown or failed reads cannot advance it. UUID corrections replace the same sample, deletion UUIDs remove it, and summaries are recomputed. Repeated batch IDs require identical content. Unsupported fields/types, inconsistent source/unit/timezone metadata and malformed timestamps are rejected before a transaction commits.

Original IANA timezone metadata is preserved when available. `timezone_provenance: utc_reference` explicitly marks a UTC reference clock when a historical local timezone is unavailable. Current location is never substituted for missing historical provenance. Empty query deltas do not establish read permission or full-day coverage.

The default retained sample window is90days. Expired raw values are purged when the store opens or is accessed; no cleanup daemon is installed. Query checkpoints, UUID provenance, replay receipts and value-free change manifests have separate caps. The pending outbox is minimized metadata, not a second raw archive. Retry records local intent and bounded backoff; it cannot contact the phone or upload. A destination adapter would need an explicit reconciliation and transport policy.

Watch storage is local plaintext under the data directory's `watch/` child, with private file/directory permissions. No network client, upload worker, pairing credential or phone-to-Mac transport is implemented.

## Companion

`ios-companion/` retains the unsigned iPhone app, HealthKit adapter and portable Swift core. The core commits protected local batches and anchors together. The iOS reader requests only explicitly selected read types after a device action, with an empty write set. Native source does not imply an installed or device-validated app. [Companion setup](ios-companion/README.md) and [device acceptance checks](ios-companion/DEVICE-QA.md) describe the remaining work.

The Mac loopback endpoint is inaccessible from another device. Selecting a phone transport, enabling capabilities, signing and granting Health permissions require separate user actions. No such setup happens when running this dashboard or its synthetic tests.
