# Sleep Studio iPhone companion source

This adds a native **iPhone companion scaffold** to Sleep Studio's Python/browser app. It is source with a tested local core, not an installed or device-validated application. The browser cannot read HealthKit. The iPhone reads only selected HealthKit types after a future explicit device action and stages bounded local batches. **Delivery is disabled: there is no network client, endpoint, credential, receiver acknowledgement, export button or share sheet.** No real HealthKit records were used in development or tests.

## What is included

- `Sources/WatchSyncCore`: the strict v1 encoder, immutable query scopes, a serialized capture coordinator, and atomic protected snapshot/outbox storage. Foundation and Apple's bundled CryptoKit are used; there are no external package dependencies.
- `Sources/WatchHealthKit`: iOS-only individual-sample adapter using observer notifications and anchored queries. This code is guarded by `canImport(HealthKit) && os(iOS)` and is not typechecked by the Mac build.
- `iOSApp`: a minimal SwiftUI app, unsigned Xcode project and shared scheme. It presents selected read types, explicit source choice, local capture status, manual catch-up/retry and pause/resume. It requests no permission at launch.
- `Tests/WatchSyncCoreTests`: wholly invented records, failure injection and reader/protected-data mocks.
- `Fixtures/synthetic-interoperability.json`: a synthetic v1 batch emitted by the compiled Swift encoder for the strict Python consumer. `synthetic-watch-fixture` always emits hardcoded invented records; it cannot export a device's records.
- [DEVICE-QA.md](DEVICE-QA.md): exact setup decisions and the device validation checklist.

## Privacy and storage behavior

The device snapshot lives in the app container's **Application Support/WatchCapture**, with directory permissions `0700`, files `0600`, `.complete` iOS file protection and `isExcludedFromBackup`. It does not use Documents, CloudKit or iCloud. File protection is applied to the temporary file **before** writing any value. A successful local commit writes and `fsync`s the temporary snapshot, atomically renames it, then `fsync`s the directory. Checkpoints, UUID scope tombstones, replay receipts and the pending batch are in this same transaction. A post-rename error has an uncertain result; the next access reloads the durable receipt instead of assuming rollback.

The only stored raw values are in the bounded pending outbox. There is no additional raw database, mirror or raw journal. Default caps are 512 pending batches, 50,000 pending sample/deletion operations, 16 MiB pending wire bytes, 4,096 small replay receipts, 100,000 UUID scope tombstones and 64 immutable query scopes. Each wire batch has at most 2,000 operations and 1 MiB. Full queues and exhausted metadata limits stop checkpoint advancement; an explicit future migration/receiver policy is needed to release capacity.

Raw pending values expire after at most 90 days. Every protected open/access checks expiry and physically rewrites the active snapshot without expired batches. An app that is not running cannot guarantee a purge at the exact deadline. Expiry keeps original checkpoints/receipts, marks `reconciliation_required`, and stops new capture before querying. It does not silently continue a chain the Mac receiver could not consume. There is no reset/rebaseline or acknowledgement API yet. Deleting filesystem entries is not a claim of secure physical erasure of underlying storage blocks.

Protected data is checked before local snapshot access and before/after each read. A locked phone, inaccessible HealthKit database, malformed metadata, invalid unit/source, failed local write or full outbox preserves the original checkpoint. The coordinator stores only a generic retry intent; the app persists a separate Boolean catch-up-needed control flag. It does not read the health snapshot while locked or log values, original anchors, source errors or private paths.

Backup exclusion uses a shared Foundation helper with `URLResourceValues.isExcludedFromBackup` and `URL.setResourceValues`. Setter errors are thrown before health bytes are written. The portable API regression does not establish actual iOS backup exclusion; that remains a real-device QA requirement.

## Query and metric contract

Each scope fixes its original stream UUID, source bundle, individual HealthKit type and absolute start boundary. The initial user choice is 7, 14 or 90 elapsed days. `.strictStartDate` is used; the first night's coverage may be partial. The predicate never rolls forward when retention changes, the date changes, or the source software version changes. Scope replacement or rebaseline is intentionally unavailable.

One page is durably captured before advancing to another. Original HealthKit sample UUIDs, stream UUIDs and batch UUIDs are lower-case canonical UUID strings. The original secure-coded `HKQueryAnchor` archive is preserved and reused; it is never replaced by a timestamp, fabricated empty anchor or receiver checkpoint. Local capture commits independently of any Mac/network acknowledgement. Deletions, unknown UUID tombstones and same-UUID corrections are preserved in order, including resting-heart-rate replacements for today or yesterday. No `HKStatisticsQuery` aggregate is misrepresented as an original sample.

Supported identifiers match `watch_metrics.py`: sleep analysis (in-bed, awake, unspecified, core, deep, REM), heart rate, resting heart rate, **HRV SDNN in milliseconds**, steps, active energy, exercise minutes, respiratory rate, oxygen saturation as a fraction, absolute sleeping wrist temperature in °C, and estimated VO₂ max in mL/kg/min. Hardware, region, source and read visibility can leave optional types empty. Wrist temperature is not body temperature. Lab biomarkers and diagnoses are outside this adapter.

Native HealthKit dates are absolute instants. Only an actual valid `HKMetadataKeyTimeZone` is encoded as `timezone_provenance: recorded`, with the original IANA zone and correct endpoint UTC offsets, including DST. Missing metadata uses explicit UTC/Z timestamps and `timezone_provenance: utc_reference`; current phone timezone is never invented as the historical timezone. Invalid metadata fails the page and preserves its checkpoint. The dashboard can later render an explicitly chosen clock while indicating unknown historical timezone provenance.

An authorization completion only means Apple's workflow completed. It does not prove a read grant. The adapter never uses write `authorizationStatus` as read authorization. No visible source or a nil next anchor is unknown, not an empty success. An anchored empty result with a real next anchor remains empty with unknown coverage; neither empty nor a measured zero is labelled healthy.

Wire timestamps use strict original-clock calendar components, not lenient ISO formatter parsing: impossible dates, `24:00`, invalid minutes/seconds, offset-minute overflow, offsets beyond ±14 hours, and encoded years outside 2000–2100 are rejected before any anchor advances, including `generated_at`.

## Morning and background behavior

After a prior explicit local opt-in, the app restores observers and catches up on launch, foreground and protected-data unlock. Sleep data arriving can trigger best-effort observer delivery; there is no reliable physiological waking event or guaranteed precise morning time. Each observer handles at most one saved scope/page and has a 20-second completion-once deadline, independent of networking or a reader that ignores cancellation. Cancelled late results cannot commit. Adapter queries also have a 12-second timeout. Remaining work records retry intent and can be caught up in foreground/manual retry; the scaffold does not promise an exact background retry schedule.

UI operations reserve their busy token before the first suspension. Pause invalidates the generation, turns durable local opt-in off, and stops observers immediately. Resumes, source setup and retry recheck the token/consent after each awaited stage; observer installation also checks between metric setup stages. A stale operation cannot re-enable capture, overwrite the paused message or release a later operation's reservation.

The core also carries an opaque, non-persistent consent permit through each catch-up. Its generation is checked around reader/store awaits, so pausing and then resuming does not authorize an older in-flight read. After the store's awaited protected-data gate, a shared synchronous lock checks that original permit and stays held through checkpoint/outbox mutation and durable replacement. Opt-out waits for an already-entered synchronous save to finish; once opt-out returns, an earlier permit cannot save later. Device-mode commits require a permit. Standalone synthetic harness commits remain supported without one. Retention maintenance and generic error status can still update local metadata without capturing a sample.

The visible timestamp means the **last successful local batch**. It does not claim all types are readable, that the full historical baseline is complete, or that any data reached the Mac. No destination has been selected or activated.

## Build and validate

macOS 14+ and Swift 6+ can build/test the core without Xcode, HealthKit access, an account, networking or an iPhone:

```sh
sh ios-companion/scripts/test-synthetic.sh
```

The script confines scratch/config/security/compiler caches to `ios-companion`, disables SwiftPM keychain/netrc lookups, and uses only bundled tools. `--disable-sandbox` avoids an unsupported nested SwiftPM subprocess sandbox inside the managed workspace sandbox; the outer workspace permissions stay in force. Some Command Line Tools releases need the bundled TestingMacros plugin loaded explicitly; the script discovers only its known toolchain locations.

The portable Mac core tests do not typecheck the guarded HealthKit/UIKit adapter. Full Xcode and its iPhone/iPhoneSimulator SDK are required for an iOS target build. Plist/XML/syntax checks also do not establish iOS API/type correctness, simulator behavior or real-device background/file-protection behavior.

When full Xcode and its iOS SDK are available, open `iOSApp/SleepStudioCompanion.xcodeproj`. The project references the local package, targets iOS 17+, uses the example bundle ID, and has `CODE_SIGNING_ALLOWED=NO`. No development team, entitlements file, provisioning profile, signing identity or archive/publish action has been configured. A future unsigned SDK compile can use:

```sh
xcodebuild -project ios-companion/iOSApp/SleepStudioCompanion.xcodeproj \
  -scheme SleepStudioCompanion -configuration Debug \
  -sdk iphonesimulator -destination 'generic/platform=iOS Simulator' \
  -derivedDataPath ios-companion/.cache/DerivedData \
  CODE_SIGNING_ALLOWED=NO build
```

This command has not been run successfully here. Device installation, capabilities/provisioning and actual read permission are separate approved setup steps in [DEVICE-QA.md](DEVICE-QA.md). The companion is not connected to the Python app and does not complete daily device-to-Mac synchronization or any upload yet.

## Primary Apple references

- [HKAnchoredObjectQuery](https://developer.apple.com/documentation/healthkit/hkanchoredobjectquery): original anchors plus added/deleted objects.
- [Background delivery](https://developer.apple.com/documentation/healthkit/hkhealthstore/enablebackgrounddelivery(for:frequency:withcompletion:)): notifications and background-delivery requirements.
- [Protected database inaccessible error](https://developer.apple.com/documentation/healthkit/hkerror/errordatabaseinaccessible): locked-device handling.
- [Authorization status](https://developer.apple.com/documentation/healthkit/hkauthorizationstatus) and [Protecting user privacy](https://developer.apple.com/documentation/healthkit/protecting-user-privacy): read visibility cannot be inferred from write status.
- [Sleep categories](https://developer.apple.com/documentation/healthkit/hkcategoryvaluesleepanalysis): overlapping in-bed/staged intervals must not be added blindly by the consumer.
- [Resting heart rate](https://developer.apple.com/documentation/healthkit/hkquantitytypeidentifier/restingheartrate): current/recent sample updates and replacements.
- [Original timezone metadata](https://developer.apple.com/documentation/healthkit/hkmetadatakeytimezone) and [VO₂ max units](https://developer.apple.com/documentation/healthkit/hkquantitytypeidentifier/vo2max).
