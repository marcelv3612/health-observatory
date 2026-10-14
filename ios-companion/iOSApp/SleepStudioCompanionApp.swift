#if os(iOS)
import SwiftUI
import Observation
import Foundation
import UIKit
import WatchSyncCore
import WatchHealthKit

@main struct SleepStudioCompanionApp: App {
    @State private var model = CompanionModel()
    @Environment(\.scenePhase) private var scenePhase
    var body: some Scene {
        WindowGroup {
            CompanionView(model: model)
                .task { await model.resumePriorOptIn() }
                .onChange(of: scenePhase) { _, phase in
                    if phase == .active { Task { await model.resumePriorOptIn() } }
                }
                .onReceive(NotificationCenter.default.publisher(for: UIApplication.protectedDataDidBecomeAvailableNotification)) { _ in
                    Task { await model.resumePriorOptIn() }
                }
        }
    }
}

@MainActor @Observable final class CompanionModel {
    var selected: Set<WatchMetric> = [.sleepAnalysis, .restingHeartRate, .hrvSDNN, .stepCount]
    var initialDays = 7
    var sources: [HealthSourceOption] = []
    var selectedBundle = ""
    var workflowFinished = false
    var optedIn = false
    var busy = false
    var message = "Choose the read types you want. Local capture begins only after your explicit action."
    var status: CaptureStatus?
    var checkpoints: [Checkpoint] = []
    private let reader = HealthKitReader()
    private let gate = DeviceProtectedDataGate()
    private let session = CaptureSessionControl()
    private var store: LocalCaptureStore?
    private var coordinator: CaptureCoordinator?
    private var observing = false
    private let optInKey = "sleepstudio.local-read-opt-in"
    private let retryKey = "sleepstudio.catch-up-needed" // Boolean only; no health value.

    private func reserveUserAction(requiresOptIn: Bool = false) -> CaptureOperationToken? {
        guard let token = session.reserveUserAction(requiresOptIn: requiresOptIn) else { return nil }
        busy = true; return token
    }
    private func finish(_ token: CaptureOperationToken) {
        session.release(token); busy = session.busy
    }
    private func isLive(_ token: CaptureOperationToken, requiresOptIn: Bool = false) -> Bool {
        session.isCurrent(token, requiresOptIn: requiresOptIn)
            && (!requiresOptIn || UserDefaults.standard.bool(forKey: optInKey))
    }
    private func continueOrStop(_ token: CaptureOperationToken, requiresOptIn: Bool = false) async -> Bool {
        guard isLive(token, requiresOptIn: requiresOptIn) else {
            // Pause can race an already-enqueued setLocalOptIn(true). Force it off
            // again before the stale operation releases its exclusive reservation.
            if let coordinator { await coordinator.setLocalOptIn(false) }
            reader.stopObserving(); observing = false
            return false
        }
        return true
    }
    private func showIfLive(_ error: any Error, token: CaptureOperationToken, requiresOptIn: Bool = false) async {
        if await continueOrStop(token, requiresOptIn: requiresOptIn) { showGeneric(error) }
    }

    private func prepareStore() throws {
        if store != nil { return }
        let support = try FileManager.default.url(for: .applicationSupportDirectory, in: .userDomainMask,
                                                  appropriateFor: nil, create: true)
        let directory = support.appendingPathComponent("WatchCapture", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false,
                                                attributes: [.posixPermissions: 0o700, .protectionKey: FileProtectionType.complete])
        try BackupExclusion.excludeFromBackup(directory)
        let store = try LocalCaptureStore(io: AtomicSnapshotFile(directory: directory), mode: .device, gate: gate)
        self.store = store
        coordinator = CaptureCoordinator(reader: reader, gate: gate, store: store, mode: .device)
    }

    /// Only this user button invokes Apple's permission workflow; never launch/foreground.
    func requestSelectedReads() async {
        guard !selected.isEmpty, !optedIn, let token = reserveUserAction() else { return }
        defer { finish(token) }
        do {
            try await reader.requestSelectedReadWorkflow(metrics: selected)
            guard await continueOrStop(token) else { return }
            workflowFinished = true
            message = "The permission workflow finished. Read visibility remains unknown. Choose the exact source to capture locally."
            let options = try await reader.visibleSourceOptions(metrics: selected)
            guard await continueOrStop(token) else { return }
            sources = options
            selectedBundle = "" // The user chooses; do not guess the Watch source.
            if sources.isEmpty {
                message = "No selected source is visible. This may mean no data or restricted reads; permission cannot be inferred."
            }
        } catch { await showIfLive(error, token: token) }
    }

    /// Freeze one stream/source/predicate only after the selected-source user action.
    func beginLocalCapture() async {
        guard workflowFinished, !selectedBundle.isEmpty, !optedIn, let token = reserveUserAction() else { return }
        defer { finish(token) }
        do {
            let available = await gate.isAvailable()
            guard await continueOrStop(token) else { return }
            guard available else { throw CaptureError.protectedDataUnavailable }
            try prepareStore()
            let existing = try await store!.checkpoints()
            guard await continueOrStop(token) else { return }
            guard existing.isEmpty else {
                // Existing checkpoints cannot be reset or replaced by a changed source/window.
                message = "A capture scope already exists. Resume it; changing the original source or baseline needs a recovery policy."
                return
            }
            let stream = UUID(), boundary = Date().addingTimeInterval(-Double(initialDays) * 86_400)
            let scopes = try selected.map { try QueryScope(streamID: stream, sourceBundleID: selectedBundle, metric: $0, startDate: boundary) }
            try await store!.register(scopes: scopes)
            guard await continueOrStop(token), session.activateOptIn(token) else { return }
            optedIn = true; UserDefaults.standard.set(true, forKey: optInKey)
            await coordinator!.setLocalOptIn(true)
            guard await continueOrStop(token, requiresOptIn: true) else { return }
            try await reader.startObserving(scopes: scopes, coordinator: coordinator!, shouldContinue: {
                self.isLive(token, requiresOptIn: true)
            })
            guard await continueOrStop(token, requiresOptIn: true) else { return }
            observing = true
            await catchUpLocal(token)
        } catch { await showIfLive(error, token: token) }
    }

    /// Restore only a prior local opt-in. No authorization request happens here.
    func resumePriorOptIn() async {
        // Synchronous reservation precedes ANY suspension. Competing lifecycle
        // notifications cannot all pass a pre-await busy check.
        guard let token = session.reservePriorOptInResume(persistedOptIn: UserDefaults.standard.bool(forKey: optInKey)) else { return }
        busy = true; optedIn = true; defer { finish(token) }
        await resumeReservedScope(token)
    }

    private func resumeReservedScope(_ token: CaptureOperationToken) async {
        do {
            guard await continueOrStop(token, requiresOptIn: true) else { return }
            let available = await gate.isAvailable()
            guard await continueOrStop(token, requiresOptIn: true) else { return }
            guard available else { throw CaptureError.protectedDataUnavailable }
            try prepareStore()
            await coordinator!.setLocalOptIn(true)
            guard await continueOrStop(token, requiresOptIn: true) else { return }
            let existing = try await store!.checkpoints()
            guard await continueOrStop(token, requiresOptIn: true) else { return }
            selected = Set(existing.map { $0.scope.metric })
            selectedBundle = existing.first?.scope.sourceBundleID ?? ""
            if !observing {
                try await reader.startObserving(scopes: existing.map(\.scope), coordinator: coordinator!, shouldContinue: {
                    self.isLive(token, requiresOptIn: true)
                })
                guard await continueOrStop(token, requiresOptIn: true) else { return }
                observing = true
            }
            await catchUpLocal(token)
        } catch { await showIfLive(error, token: token, requiresOptIn: true) }
    }

    func manualRetry() async {
        guard optedIn, let token = reserveUserAction(requiresOptIn: true) else { return }
        defer { finish(token) }; await catchUpLocal(token)
    }

    func resumeStoredScopeByUserAction() async {
        guard let token = reserveUserAction() else { return }
        defer { finish(token) }
        do {
            let available = await gate.isAvailable()
            guard await continueOrStop(token) else { return }
            guard available else { throw CaptureError.protectedDataUnavailable }
            try prepareStore()
            let existing = try await store!.checkpoints()
            guard await continueOrStop(token) else { return }
            guard !existing.isEmpty else {
                message = "Choose reads and a source to begin a new local scope."; return
            }
            guard session.activateOptIn(token) else { return }
            optedIn = true
            UserDefaults.standard.set(true, forKey: optInKey)
            // Reuse this reservation; do not deadlock against a nested busy guard.
            await resumeReservedScope(token)
        } catch { await showIfLive(error, token: token) }
    }

    private func catchUpLocal(_ token: CaptureOperationToken) async {
        guard let coordinator, let store else { return }
        guard await continueOrStop(token, requiresOptIn: true) else { return }
        let result = await coordinator.catchUp()
        guard await continueOrStop(token, requiresOptIn: true) else { return }
        let retry = await coordinator.retryIntent()
        guard await continueOrStop(token, requiresOptIn: true) else { return }
        do {
            let capturedStatus = try await store.status()
            guard await continueOrStop(token, requiresOptIn: true) else { return }
            let capturedCheckpoints = try await store.checkpoints()
            guard await continueOrStop(token, requiresOptIn: true) else { return }
            status = capturedStatus; checkpoints = capturedCheckpoints
            UserDefaults.standard.set(retry.needed, forKey: retryKey)
            switch result {
            case .captured:
                message = retry.needed ? "Local page captured. More data awaits a later catch-up." : "Local queries captured. Delivery is disabled; read coverage remains unknown."
            case .deferred(let code): message = Self.message(for: code)
            case .busy: message = "A local catch-up is already in progress."
            case .optInRequired: message = "Local read opt-in is required."
            }
        } catch { await showIfLive(error, token: token, requiresOptIn: true) }
    }

    func pauseCapture() async {
        session.pause()
        optedIn = false; UserDefaults.standard.set(false, forKey: optInKey)
        reader.stopObserving(); observing = false
        message = "Local capture is paused. Existing protected batches and checkpoints remain until retention expiry."
        if let coordinator { await coordinator.setLocalOptIn(false) }
    }

    private func showGeneric(_ error: any Error) {
        let code = (error as? CaptureError) ?? .readerUnavailable
        message = Self.message(for: code)
        UserDefaults.standard.set(true, forKey: retryKey)
        if code == .protectedDataUnavailable { status = nil; checkpoints = [] }
        // Never log interpolated errors, paths, sample contents or query anchors.
    }
    private static func message(for code: CaptureError) -> String {
        switch code {
        case .protectedDataUnavailable: "Waiting for protected data after unlock. Checkpoints are preserved."
        case .readVisibilityUnknown: "No visible read result. This may mean no data or restricted reads; permission cannot be inferred."
        case .resourceLimit: "The local queue limit was reached. Capture stopped without advancing the checkpoint."
        case .reconciliationRequired: "An unsent batch expired. Capture is stopped until an explicit delivery recovery policy is approved."
        default: "Local capture needs retry or setup review. Checkpoints are preserved."
        }
    }
}

private struct CompanionView: View {
    @Bindable var model: CompanionModel
    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Text("Delivery disabled · no approved recipient").font(.headline)
                    Text("This source has no upload or export feature. Pending values stay in protected local storage, excluded from backups.")
                    Text("Morning capture follows data availability. Apple does not guarantee an exact time after physiological waking.")
                }
                Section("Local capture status") {
                    Text(model.message)
                    if let status = model.status {
                        LabeledContent("Last successful local batch", value: status.lastSuccessfulLocalCapture?.formatted() ?? "Never")
                        LabeledContent("Pending delivery batches", value: String(status.pendingBatches))
                        LabeledContent("Read coverage", value: "Unknown")
                        if status.deliveryGap { Text("Delivery recovery required").foregroundStyle(.orange) }
                    }
                    ForEach(model.checkpoints, id: \.scope.key) { checkpoint in
                        LabeledContent(checkpoint.scope.metric.label, value: checkpoint.lastReadState.rawValue)
                    }
                    Text("An empty result is not a healthy value and does not prove read permission. HRV is SDNN; wrist temperature is not body temperature. Lab biomarkers are outside this module.").font(.footnote)
                }
                if !model.optedIn {
                    Section("Choose reads") {
                        ForEach(WatchMetric.allCases, id: \.self) { metric in
                            Toggle(metric.label, isOn: Binding(get: { model.selected.contains(metric) }, set: { enabled in
                                if enabled { model.selected.insert(metric) } else { model.selected.remove(metric) }
                                model.workflowFinished = false; model.sources = []; model.selectedBundle = ""
                            }))
                        }
                        Picker("Initial lookback", selection: $model.initialDays) {
                            ForEach([7, 14, 90], id: \.self) { Text("\($0) days").tag($0) }
                        }
                        Text("The chosen absolute start boundary is permanent for this stream. The first night may be partial. Raw pending values expire after 90 days.").font(.footnote)
                        Button("Choose read access on this iPhone") { Task { await model.requestSelectedReads() } }
                            .disabled(model.busy || model.selected.isEmpty)
                        if model.workflowFinished {
                            Picker("Capture source", selection: $model.selectedBundle) {
                                Text("Choose a source").tag("")
                                ForEach(model.sources) { source in Text("\(source.name) · \(source.bundleID)").tag(source.bundleID) }
                            }
                            Button("Begin protected local capture") { Task { await model.beginLocalCapture() } }
                                .disabled(model.busy || model.selectedBundle.isEmpty)
                        }
                        Button("Resume existing capture scope") { Task { await model.resumeStoredScopeByUserAction() } }
                            .disabled(model.busy)
                    }.disabled(model.busy)
                }
                Section {
                    Button("Catch up / retry locally") { Task { await model.manualRetry() } }
                        .disabled(model.busy || !model.optedIn)
                    if model.optedIn { Button("Pause local capture") { Task { await model.pauseCapture() } } }
                }
            }.navigationTitle("Sleep Studio Watch")
        }
    }
}
#endif
