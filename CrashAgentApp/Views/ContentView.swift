// ContentView.swift
//
// Main window. Visual redesign: a real design-token system (see
// DesignSystem.swift) replaces ad-hoc styling, and the agent transcript
// is now a vertical timeline -- a connecting line down the left edge
// with a node per step -- rather than disconnected stacked cards. This
// is the signature layout choice: it visually encodes that a diagnosis
// is a sequential investigation, the same way a git log or a build
// pipeline view does, rather than reading like a chat thread.
//
// Functionally unchanged from the prior version: paste a crash log, hit
// Symbolicate (calls the Python backend's /symbolicate, which shells
// out to atos), then hit Diagnose to stream the agent's live
// investigation from /diagnose. All business logic lives in the Python
// backend (see /backend); this is a thin client over BackendClient.

import SwiftUI
import Combine
import UniformTypeIdentifiers

struct ContentView: View {
    @StateObject private var settings = AppSettings()
    @State private var crashLogText: String = ""
    @State private var symbolicatedCrash: SymbolicatedCrash?
    @State private var symbolicationError: String?
    @State private var showingSettings = false

    @State private var loadedFileName: String = ""
    @State private var fileLoadError: String?
    @State private var isDropTargeted = false

    @State private var agentSteps: [DisplayStep] = []
    @State private var isDiagnosing = false
    @State private var diagnoseTask: Task<Void, Never>?

    var body: some View {
        NavigationSplitView {
            sidebar
        } detail: {
            detail
        }
        .background(DS.Color.surfaceBase)
        .sheet(isPresented: $showingSettings) {
            SettingsView(settings: settings)
        }
        .toolbar {
            ToolbarItem {
                Button {
                    showingSettings = true
                } label: {
                    Label("Settings", systemImage: "gearshape")
                }
            }
        }
    }

    // MARK: - Sidebar: crash log input

    private var sidebar: some View {
        VStack(alignment: .leading, spacing: DS.Space.lg) {
            VStack(alignment: .leading, spacing: DS.Space.xs) {
                Text("Crash Log")
                    .font(DS.Font.pageTitle)
                Text("Paste a .crash or .ips report, or load one from disk")
                    .font(DS.Font.caption)
                    .foregroundStyle(DS.Color.textSecondary)
            }

            HStack(spacing: DS.Space.sm) {
                Button {
                    chooseCrashLogFile()
                } label: {
                    HStack(spacing: 5) {
                        Image(systemName: "doc.badge.plus")
                        Text("Choose File…")
                    }
                }
                .buttonStyle(.dsSecondary)

                if !loadedFileName.isEmpty {
                    Text(loadedFileName)
                        .font(DS.Font.caption)
                        .foregroundStyle(DS.Color.textSecondary)
                        .lineLimit(1)
                        .truncationMode(.middle)
                }
            }

            ZStack(alignment: .topLeading) {
                if crashLogText.isEmpty {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Paste crash report text here…")
                        Text("…or drop a .ips / .crash file")
                            .foregroundStyle(DS.Color.textTertiary.opacity(0.7))
                    }
                    .font(DS.Font.code)
                    .foregroundStyle(DS.Color.textTertiary)
                    .padding(.top, 8)
                    .padding(.leading, 5)
                    .allowsHitTesting(false)
                }
                TextEditor(text: $crashLogText)
                    .font(DS.Font.code)
                    .scrollContentBackground(.hidden)
                    .padding(4)
            }
            .frame(minHeight: 320)
            .dsCard(
                tint: DS.Color.surfaceSunken,
                border: isDropTargeted ? DS.Color.accent : DS.Color.hairline
            )
            .animation(.easeOut(duration: 0.15), value: isDropTargeted)
            .onDrop(of: [.fileURL], isTargeted: $isDropTargeted) { providers in
                handleDrop(providers: providers)
            }

            if let fileLoadError {
                HStack(alignment: .top, spacing: DS.Space.sm) {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .foregroundStyle(DS.Color.danger)
                        .font(.system(size: 12))
                    Text(fileLoadError)
                        .font(DS.Font.caption)
                        .foregroundStyle(DS.Color.danger)
                        .textSelection(.enabled)
                }
                .padding(DS.Space.sm)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(DS.Color.danger.opacity(0.08))
                .clipShape(RoundedRectangle(cornerRadius: DS.Radius.sm, style: .continuous))
            }

            if let symbolicationError {
                HStack(alignment: .top, spacing: DS.Space.sm) {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .foregroundStyle(DS.Color.danger)
                        .font(.system(size: 12))
                    Text(symbolicationError)
                        .font(DS.Font.caption)
                        .foregroundStyle(DS.Color.danger)
                        .textSelection(.enabled)
                }
                .padding(DS.Space.sm)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(DS.Color.danger.opacity(0.08))
                .clipShape(RoundedRectangle(cornerRadius: DS.Radius.sm, style: .continuous))
            }

            Button {
                runSymbolication()
            } label: {
                HStack(spacing: 6) {
                    Image(systemName: "wand.and.stars")
                    Text("Symbolicate")
                }
                .frame(maxWidth: .infinity)
            }
            .buttonStyle(.dsPrimary(disabled: crashLogText.isEmpty || !settings.isConfigured))
            .disabled(crashLogText.isEmpty || !settings.isConfigured)

            if !settings.isConfigured {
                Label("Configure your Groq key, binary path, and codebase root in Settings.", systemImage: "info.circle")
                    .font(DS.Font.caption)
                    .foregroundStyle(DS.Color.textSecondary)
            }

            Spacer()
        }
        .padding(DS.Space.xl)
        .frame(minWidth: 380, idealWidth: 440)
        .background(DS.Color.surfaceBase)
    }

    // MARK: - Detail: trace + agent timeline

    private var detail: some View {
        Group {
            if let crash = symbolicatedCrash {
                ScrollView {
                    VStack(alignment: .leading, spacing: DS.Space.xl) {
                        traceView(crash)
                        agentSection(crash)
                    }
                    .padding(DS.Space.xl)
                }
            } else {
                emptyStateView
            }
        }
        .frame(minWidth: 560)
        .background(DS.Color.surfaceBase)
    }

    private var emptyStateView: some View {
        VStack(spacing: DS.Space.md) {
            Image(systemName: "ladybug")
                .font(.system(size: 36, weight: .light))
                .foregroundStyle(DS.Color.textTertiary)
            Text("No Crash Symbolicated Yet")
                .font(DS.Font.sectionTitle)
                .foregroundStyle(DS.Color.textPrimary)
            Text("Paste a crash log on the left and click Symbolicate.")
                .font(DS.Font.body)
                .foregroundStyle(DS.Color.textSecondary)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    // MARK: - Trace panel

    private func traceView(_ crash: SymbolicatedCrash) -> some View {
        VStack(alignment: .leading, spacing: DS.Space.md) {
            HStack(spacing: DS.Space.sm) {
                StatusPill(text: crash.exceptionType, color: DS.Color.danger, icon: "bolt.fill")
                Text(crash.threadLabel)
                    .font(DS.Font.sectionTitle)
                    .foregroundStyle(DS.Color.textPrimary)
            }

            if !crash.exceptionSubtype.isEmpty {
                Text(crash.exceptionSubtype)
                    .font(DS.Font.body)
                    .foregroundStyle(DS.Color.textSecondary)
            }

            Divider().overlay(DS.Color.hairline)

            VStack(alignment: .leading, spacing: 3) {
                ForEach(crash.frames) { frame in
                    HStack(alignment: .top, spacing: DS.Space.sm) {
                        Text("\(frame.index)")
                            .font(DS.Font.codeSmall)
                            .foregroundStyle(DS.Color.textTertiary)
                            .frame(width: 18, alignment: .trailing)
                        Text(frame.displayLine)
                            .font(DS.Font.codeSmall)
                            .foregroundStyle(frame.symbolName == nil ? DS.Color.textTertiary : DS.Color.textPrimary)
                            .textSelection(.enabled)
                    }
                }
            }
        }
        .padding(DS.Space.lg)
        .frame(maxWidth: .infinity, alignment: .leading)
        .dsCard()
    }

    // MARK: - Agent timeline section

    private func agentSection(_ crash: SymbolicatedCrash) -> some View {
        VStack(alignment: .leading, spacing: DS.Space.md) {
            HStack {
                Text("Investigation")
                    .font(DS.Font.sectionTitle)
                    .foregroundStyle(DS.Color.textPrimary)
                Spacer()
                Button {
                    runDiagnosis(crash)
                } label: {
                    HStack(spacing: 6) {
                        if isDiagnosing {
                            ProgressView().controlSize(.small).tint(.white)
                        } else {
                            Image(systemName: "sparkle.magnifyingglass")
                        }
                        Text(isDiagnosing ? "Investigating…" : "Diagnose")
                    }
                }
                .buttonStyle(.dsPrimary(disabled: isDiagnosing))
                .disabled(isDiagnosing)
            }

            if agentSteps.isEmpty {
                Text("Run Diagnose to start the agent's investigation.")
                    .font(DS.Font.caption)
                    .foregroundStyle(DS.Color.textTertiary)
                    .padding(.vertical, DS.Space.md)
            } else {
                TimelineView(steps: agentSteps, settings: settings)
            }
        }
    }

    // MARK: - Actions

    /// Reads a file from disk into the crash log text box. Used by both
    /// the "Choose File…" button and drag-and-drop. .ips files are
    /// JSON, .crash files are plain text -- both are read as UTF-8
    /// either way, since the JSON format is itself UTF-8 text on disk.
    private func loadCrashLogFile(at url: URL) {
        fileLoadError = nil
        do {
            let data = try Data(contentsOf: url)
            guard let text = String(data: data, encoding: .utf8) else {
                fileLoadError = "Could not read '\(url.lastPathComponent)' as UTF-8 text. Crash reports should always be plain text or JSON -- this file may be corrupted or not actually a crash report."
                return
            }
            crashLogText = text
            loadedFileName = url.lastPathComponent
        } catch {
            fileLoadError = "Could not read '\(url.lastPathComponent)': \(error.localizedDescription)"
        }
    }

    private func chooseCrashLogFile() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        panel.title = "Choose a Crash Report"
        // .ips is the modern format; .crash is the legacy one (treated
        // as plain text by the system). Some tools export crash reports
        // under .txt/.log too, so include those rather than .item (which
        // would match nearly anything and defeat the point of filtering).
        var allowedTypes: [UTType] = [.plainText, .text]
        if let ipsType = UTType(filenameExtension: "ips") {
            allowedTypes.append(ipsType)
        }
        if let crashType = UTType(filenameExtension: "crash") {
            allowedTypes.append(crashType)
        }
        if let logType = UTType(filenameExtension: "log") {
            allowedTypes.append(logType)
        }
        panel.allowedContentTypes = allowedTypes

        if panel.runModal() == .OK, let url = panel.url {
            loadCrashLogFile(at: url)
        }
    }

    private func handleDrop(providers: [NSItemProvider]) -> Bool {
        guard let provider = providers.first else { return false }
        guard provider.hasItemConformingToTypeIdentifier(UTType.fileURL.identifier) else { return false }

        provider.loadItem(forTypeIdentifier: UTType.fileURL.identifier, options: nil) { item, error in
            guard error == nil else {
                Task { @MainActor in
                    fileLoadError = "Could not read dropped file: \(error!.localizedDescription)"
                }
                return
            }
            var resolvedURL: URL?
            if let data = item as? Data {
                resolvedURL = URL(dataRepresentation: data, relativeTo: nil)
            } else if let url = item as? URL {
                resolvedURL = url
            }
            guard let url = resolvedURL else { return }

            Task { @MainActor in
                loadCrashLogFile(at: url)
            }
        }
        return true
    }

    private func runSymbolication() {
        symbolicationError = nil
        symbolicatedCrash = nil
        agentSteps = []

        let client = BackendClient(baseURL: settings.backendURL)
        let binaryPath = settings.binaryPath
        let targetName = settings.targetBinaryName
        let logText = crashLogText

        Task {
            do {
                let result = try await client.symbolicate(
                    crashLogText: logText,
                    binaryPath: binaryPath,
                    targetBinaryName: targetName.isEmpty ? nil : targetName
                )
                await MainActor.run {
                    withAnimation(.easeOut(duration: 0.2)) {
                        self.symbolicatedCrash = result
                    }
                }
            } catch {
                await MainActor.run {
                    self.symbolicationError = error.localizedDescription
                }
            }
        }
    }

    private func runDiagnosis(_ crash: SymbolicatedCrash) {
        agentSteps = []
        isDiagnosing = true

        let client = BackendClient(baseURL: settings.backendURL)
        let codebaseRoot = settings.codebaseRootPath
        let apiKey = settings.groqAPIKey
        let model = settings.groqModel

        diagnoseTask?.cancel()
        diagnoseTask = Task {
            do {
                let stream = client.diagnose(
                    crash: crash,
                    codebaseRoot: codebaseRoot,
                    groqAPIKey: apiKey,
                    groqModel: model.isEmpty ? nil : model
                )
                for try await dto in stream {
                    await MainActor.run {
                        withAnimation(.easeOut(duration: 0.2)) {
                            agentSteps.append(DisplayStep(dto: dto))
                        }
                    }
                }
            } catch {
                await MainActor.run {
                    agentSteps.append(DisplayStep(errorText: error.localizedDescription))
                }
            }
            await MainActor.run {
                isDiagnosing = false
            }
        }
    }
}

// MARK: - DisplayStep view model

/// Swift-side view model for one streamed AgentStepDTO, with a stable
/// identity for SwiftUI's ForEach (the raw DTO has no id of its own).
final class DisplayStep: Identifiable, ObservableObject {
    let id = UUID()
    let dto: AgentStepDTO?
    let localErrorText: String?
    @Published var fixStatus: FixStatus = .pending

    enum FixStatus {
        case pending
        case applying
        case applied(backupPath: String)
        case rejected
        case failed(String)
    }

    init(dto: AgentStepDTO) {
        self.dto = dto
        self.localErrorText = nil
    }
    init(errorText: String) {
        self.dto = nil
        self.localErrorText = errorText
    }

    /// Drives the timeline node's icon/color -- one place that maps a
    /// step's semantic type to its visual identity, shared by the node
    /// and the card header so they never disagree.
    var kind: StepKind {
        if localErrorText != nil { return .error }
        switch dto?.type {
        case "thinking": return .thinking
        case "tool_call": return .toolCall
        case "tool_result": return .toolResult
        case "fix_proposed": return .fixProposed
        case "final_answer": return .finalAnswer
        case "error": return .error
        default: return .unknown
        }
    }
}

enum StepKind {
    case thinking, toolCall, toolResult, fixProposed, finalAnswer, error, unknown

    var icon: String {
        switch self {
        case .thinking: return "brain"
        case .toolCall: return "wrench.and.screwdriver.fill"
        case .toolResult: return "doc.text.magnifyingglass"
        case .fixProposed: return "sparkles"
        case .finalAnswer: return "checkmark.seal.fill"
        case .error: return "exclamationmark.triangle.fill"
        case .unknown: return "questionmark.circle"
        }
    }

    var color: Color {
        switch self {
        case .thinking: return DS.Color.accent
        case .toolCall, .toolResult: return DS.Color.inProgress
        case .fixProposed: return DS.Color.proposal
        case .finalAnswer: return DS.Color.success
        case .error: return DS.Color.danger
        case .unknown: return DS.Color.textTertiary
        }
    }

    var title: String {
        switch self {
        case .thinking: return "Thinking"
        case .toolCall: return "Tool Call"
        case .toolResult: return "Tool Result"
        case .fixProposed: return "Proposed Fix"
        case .finalAnswer: return "Diagnosis"
        case .error: return "Error"
        case .unknown: return "Step"
        }
    }
}

// MARK: - Timeline (the signature layout element)

/// Renders the agent's steps as a vertical timeline: a connecting line
/// down the left edge with a colored node per step, each paired with a
/// card describing what happened. This is deliberately closer to a git
/// log / CI pipeline view than a chat transcript -- it's a sequential
/// investigation, and the layout says so.
private struct TimelineView: View {
    let steps: [DisplayStep]
    let settings: AppSettings

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            ForEach(Array(steps.enumerated()), id: \.element.id) { index, step in
                TimelineRow(
                    step: step,
                    settings: settings,
                    isLast: index == steps.count - 1
                )
            }
        }
    }
}

private struct TimelineRow: View {
    @ObservedObject var step: DisplayStep
    let settings: AppSettings
    let isLast: Bool

    private let nodeSize: CGFloat = 22
    private let railWidth: CGFloat = 2

    var body: some View {
        HStack(alignment: .top, spacing: DS.Space.md) {
            // Rail + node
            VStack(spacing: 0) {
                ZStack {
                    Circle()
                        .fill(step.kind.color.opacity(0.15))
                        .frame(width: nodeSize, height: nodeSize)
                    Image(systemName: step.kind.icon)
                        .font(.system(size: 10, weight: .semibold))
                        .foregroundStyle(step.kind.color)
                }
                if !isLast {
                    Rectangle()
                        .fill(DS.Color.hairlineStrong)
                        .frame(width: railWidth)
                        .frame(maxHeight: .infinity)
                }
            }
            .frame(width: nodeSize)

            // Content card
            StepCard(step: step, settings: settings)
                .padding(.bottom, DS.Space.lg)
        }
    }
}

private struct StepCard: View {
    @ObservedObject var step: DisplayStep
    let settings: AppSettings

    var body: some View {
        VStack(alignment: .leading, spacing: DS.Space.xs) {
            Text(step.kind.title)
                .font(DS.Font.captionEmphasis)
                .foregroundStyle(step.kind.color)

            content
        }
        .padding(DS.Space.md)
        .frame(maxWidth: .infinity, alignment: .leading)
        .dsCard(tint: DS.Color.surfaceRaised, border: step.kind == .fixProposed ? step.kind.color.opacity(0.3) : DS.Color.hairline)
    }

    @ViewBuilder
    private var content: some View {
        if let localErrorText = step.localErrorText {
            bodyText(localErrorText)
        } else if let dto = step.dto {
            switch dto.type {
            case "thinking":
                bodyText(dto.text ?? "")
            case "tool_call":
                VStack(alignment: .leading, spacing: 4) {
                    Text(dto.toolName ?? "tool")
                        .font(DS.Font.codeEmphasis)
                        .foregroundStyle(DS.Color.textPrimary)
                    if let args = dto.toolArguments, !args.isEmpty {
                        Text(args)
                            .font(DS.Font.codeSmall)
                            .foregroundStyle(DS.Color.textSecondary)
                            .textSelection(.enabled)
                    }
                }
            case "tool_result":
                ScrollView {
                    bodyText(dto.toolResult ?? "")
                }
                .frame(maxHeight: 220)
            case "fix_proposed":
                FixProposalView(step: step, dto: dto, settings: settings)
            case "final_answer":
                bodyText(dto.text ?? "")
            case "error":
                bodyText(dto.text ?? "")
            default:
                bodyText(dto.text ?? "")
            }
        }
    }

    private func bodyText(_ text: String) -> some View {
        Text(text)
            .font(DS.Font.code)
            .foregroundStyle(DS.Color.textPrimary)
            .textSelection(.enabled)
            .fixedSize(horizontal: false, vertical: true)
    }
}

// MARK: - Fix proposal: diff + approve/reject

private struct FixProposalView: View {
    @ObservedObject var step: DisplayStep
    let dto: AgentStepDTO
    let settings: AppSettings

    var body: some View {
        VStack(alignment: .leading, spacing: DS.Space.sm) {
            HStack(spacing: 6) {
                Image(systemName: "doc.text")
                    .font(.system(size: 11))
                    .foregroundStyle(DS.Color.proposal)
                Text(dto.fixPath ?? "?")
                    .font(DS.Font.codeEmphasis)
                    .foregroundStyle(DS.Color.textPrimary)
            }

            if let explanation = dto.fixExplanation, !explanation.isEmpty {
                Text(explanation)
                    .font(DS.Font.body)
                    .foregroundStyle(DS.Color.textSecondary)
            }

            ScrollView {
                DiffTextView(diffText: dto.fixDiff ?? "")
            }
            .frame(maxHeight: 260)
            .dsCard(tint: DS.Color.surfaceSunken)

            actionRow
        }
    }

    @ViewBuilder
    private var actionRow: some View {
        switch step.fixStatus {
        case .pending:
            HStack(spacing: DS.Space.sm) {
                Button {
                    applyFix()
                } label: {
                    HStack(spacing: 5) {
                        Image(systemName: "checkmark")
                        Text("Approve & Write to File")
                    }
                }
                .buttonStyle(.dsPrimary(tint: DS.Color.success))

                Button("Reject") {
                    step.fixStatus = .rejected
                }
                .buttonStyle(.dsSecondary)
            }
        case .applying:
            HStack(spacing: 6) {
                ProgressView().controlSize(.small)
                Text("Writing to file…")
                    .font(DS.Font.caption)
                    .foregroundStyle(DS.Color.textSecondary)
            }
        case .applied(let backupPath):
            VStack(alignment: .leading, spacing: 3) {
                StatusPill(text: "Applied", color: DS.Color.success, icon: "checkmark")
                Text("Backup saved at \(backupPath)")
                    .font(DS.Font.caption)
                    .foregroundStyle(DS.Color.textTertiary)
                    .textSelection(.enabled)
            }
        case .rejected:
            StatusPill(text: "Rejected — file not modified", color: DS.Color.textSecondary, icon: "xmark")
        case .failed(let message):
            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 4) {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .foregroundStyle(DS.Color.danger)
                    Text("Failed to apply: \(message)")
                        .font(DS.Font.caption)
                        .foregroundStyle(DS.Color.danger)
                }
                Button("Retry") {
                    step.fixStatus = .pending
                }
                .buttonStyle(.dsSecondary)
            }
        }
    }

    private func applyFix() {
        guard let path = dto.fixPath,
              let oldContent = dto.fixOldContent,
              let newContent = dto.fixNewContent else {
            step.fixStatus = .failed("missing fix data (this shouldn't happen -- please report)")
            return
        }

        step.fixStatus = .applying
        let client = BackendClient(baseURL: settings.backendURL)
        let codebaseRoot = settings.codebaseRootPath

        Task {
            do {
                let backupPath = try await client.applyFix(
                    codebaseRoot: codebaseRoot,
                    path: path,
                    expectedOldContent: oldContent,
                    newContent: newContent
                )
                await MainActor.run {
                    withAnimation(.easeOut(duration: 0.2)) {
                        step.fixStatus = .applied(backupPath: backupPath)
                    }
                }
            } catch {
                await MainActor.run {
                    step.fixStatus = .failed(error.localizedDescription)
                }
            }
        }
    }
}

/// Renders unified diff text with per-line coloring: subdued red
/// background for removed lines, subdued green for added lines, hunk
/// headers in the accent color, context lines plain.
private struct DiffTextView: View {
    let diffText: String

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            ForEach(Array(diffText.components(separatedBy: "\n").enumerated()), id: \.offset) { _, line in
                HStack(spacing: 0) {
                    Text(marker(for: line))
                        .font(DS.Font.codeSmall)
                        .foregroundStyle(textColor(for: line))
                        .frame(width: 14, alignment: .center)
                    Text(displayText(for: line))
                        .font(DS.Font.codeSmall)
                        .foregroundStyle(textColor(for: line))
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, 6)
                .padding(.vertical, 1)
                .background(backgroundColor(for: line))
            }
        }
    }

    private func marker(for line: String) -> String {
        if line.hasPrefix("+") && !line.hasPrefix("+++") { return "+" }
        if line.hasPrefix("-") && !line.hasPrefix("---") { return "−" }
        return ""
    }

    private func displayText(for line: String) -> String {
        if (line.hasPrefix("+") && !line.hasPrefix("+++")) || (line.hasPrefix("-") && !line.hasPrefix("---")) {
            return String(line.dropFirst())
        }
        return line.isEmpty ? " " : line
    }

    private func backgroundColor(for line: String) -> Color {
        if line.hasPrefix("+++") || line.hasPrefix("---") {
            return .clear
        } else if line.hasPrefix("@@") {
            return DS.Color.diffHunk
        } else if line.hasPrefix("+") {
            return DS.Color.diffAdded
        } else if line.hasPrefix("-") {
            return DS.Color.diffRemoved
        }
        return .clear
    }

    private func textColor(for line: String) -> Color {
        if line.hasPrefix("@@") { return DS.Color.accent }
        if line.hasPrefix("+++") || line.hasPrefix("---") { return DS.Color.textTertiary }
        if line.hasPrefix("+") { return DS.Color.diffAddedText }
        if line.hasPrefix("-") { return DS.Color.diffRemovedText }
        return DS.Color.textSecondary
    }
}

#Preview {
    ContentView()
}
