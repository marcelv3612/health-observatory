# Decisions and device checks before live use

This checklist contains no device records. The current deliverable is source plus synthetic core tests. No HealthKit permission, signing identity, new account grant, developer capability or recipient was activated.

## Missing dependencies

1. A full supported Xcode installation and iOS/iPhoneSimulator SDK. Command Line Tools alone cannot typecheck the guarded HealthKit/UIKit source or compile the app. Obtain Xcode through an approved user setup step; this task does not install it or switch global developer settings.
2. SDK compile and UI inspection of the unsigned example Xcode project. Resolve any Swift 6/HealthKit API differences against the actual SDK, including the secure-coded anchor archive, source predicates, callback concurrency, quantity conversions and availability.
3. An iPhone running iOS 17+ and a compatible Apple Watch/source with the intended metrics. Simulator validation is useful for invented samples but does not replace locked-phone and background-delivery checks on a real device.

## User decisions needed

- **Device build setup:** choose the app bundle identity/developer team and approve HealthKit capability, `com.apple.developer.healthkit.background-delivery` entitlement/provisioning, signing and installation. None are in this unsigned project. App Store submission and iCloud/CloudKit are outside scope.
- **Read scope on the phone:** press the explicit read-selection button, choose only the required types in Apple's device workflow, then select the actual visible Watch source bundle and a 7/14/90-day initial window. The fixed start window cannot be silently reset later. Read denial cannot be identified from authorization completion; no visible result remains unknown.
- **Delivery recipient and transport:** decide whether an approved local Mac receiver or an explicitly approved service should receive which minimized fields, on what retention policy. Current delivery stays disabled, with no endpoint or credential. Recipient authentication, acknowledgement ordering, encryption, disconnect/retry behavior and data minimization need review before implementing a transport. Do not assume permission to send records to an AI/TTS service or iCloud.
- **Expired/full queue recovery:** approve an explicit policy for a missed baseline/delivery chain before allowing rebaseline, dropping receipts, clearing capacity or restarting capture. The scaffold hard-stops on reconciliation-required and has no reset control. A phone-local checkpoint is not a receiver acknowledgement.

## QA after the approved setup

- [ ] Build the actual iOS target with the selected SDK. Inspect labels, type selection, source selection, status, manual retry and pause/resume. Launch must not show an automatic permission dialog.
- [ ] Use a dedicated synthetic test store first. Confirm synthetic/device mode separation and that no fixture generator can read HealthKit or import arbitrary files.
- [ ] Confirm `.complete` protection on snapshot and temporary files, `0700`/`0600` modes, Application Support placement and backup exclusion. Verify that a locked phone neither opens the health snapshot nor advances an anchor.
- [ ] Allow a minimal selected read scope through the explicit device workflow. Check that the app never claims read grants from workflow completion or write authorization status. Denied, unavailable, empty, zero and failed states must remain distinct.
- [ ] Verify original sample UUID preservation and exact bundle/type/start predicate across app restarts, phone timezone changes and source software updates. Check secure-coded anchor restoration without replacing it by a timestamp.
- [ ] Compare synthetic quantity conversions with SDK `HKQuantity` expectations: count/min, HRV SDNN ms, count, kcal, min, SpO₂ fraction, °C, mL/kg/min. Check optional metric absence on unsupported hardware/region without fabricated values.
- [ ] Exercise sleep stages, overlapping in-bed intervals, DST transitions, travel metadata and naps. Only actual valid original timezone metadata may be recorded provenance; absent metadata is UTC reference, malformed metadata fails without checkpoint movement.
- [ ] Exercise today/yesterday resting-heart-rate delete/replacement, same-UUID correction and deletion for an unknown sample UUID. Verify ordered deltas reach only a future explicitly approved receiver and summaries are recomputed there.
- [ ] Test a background notification with the phone unlocked, locked and after unlock. Observe completion once, bounded work, generic retry intent and foreground catch-up. Test force-quit/relaunch and stale/empty observer notifications. Precise physiological wake timing is not guaranteed.
- [ ] Trigger overlapping launch/foreground/unlock resume events and Pause during protected-data availability, checkpoint loading and observer setup. Exactly one UI operation may own the reservation; a paused operation must not re-enable local opt-in/observers or replace the paused status after its await returns.
- [ ] Reproduce the synthetic suspended-commit gate race: await core opt-out, then release the store's gate. No sample, receipt, outbox or anchor may be committed by the old permit. Pause/resume during a pending read must reject that old generation. A synchronous save already holding the consent fence may complete before opt-out returns; it must never begin later from a stale permit.
- [ ] Interrupt immediately before and after rename, inject failed directory `fsync`, replay the original batch and run concurrent callbacks. Outcomes must recover through durable receipts without duplicate operations or stale anchors.
- [ ] Fill sample/byte/batch/receipt limits; checkpoints must stay unchanged for rejected work. Advance the synthetic clock beyond retention on reopen/status/read/retry: expired raw values must disappear from the active snapshot, anchors/receipts must survive, and further capture must stop with reconciliation required.
- [ ] Keep all raw health values and underlying errors out of logs, screenshots, crash attachments and external handoffs. Record QA outcomes as generic pass/fail and counts rather than private sample payloads.

Until these checks pass and the transport is separately approved and implemented, the honest status is **native source scaffold, local capture tested with synthetic data, device integration and delivery pending**.
