// SettingsView.swift
//
// Restyled onto the shared design system (DesignSystem.swift) -- the
// underlying layout (full-width path fields with the value echoed
// below so two paths never look identical when truncated) is unchanged
// from the version that fixed that exact bug; this pass only touches
// color, type, and spacing tokens.

import SwiftUI

struct SettingsView: View {
    @ObservedObject var settings: AppSettings
    @State private var backendReachable: Bool?
    @State private var isCheckingBackend = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: DS.Space.xl) {
                header
                backendSection
                groqSection
                symbolicationSection
            }
            .padding(DS.Space.xl)
        }
        .frame(width: 660, height: 660)
        .background(DS.Color.surfaceBase)
        .task {
            checkBackend()
        }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: DS.Space.xs) {
            Text("Settings")
                .font(DS.Font.pageTitle)
            Text("Configure the backend connection and crash analysis inputs.")
                .font(DS.Font.caption)
                .foregroundStyle(DS.Color.textSecondary)
        }
    }

    // MARK: - Sections

    private var backendSection: some View {
        sectionCard(title: "Backend Service", icon: "server.rack") {
            labeledField(label: "Backend URL") {
                TextField("http://127.0.0.1:8000", text: $settings.backendURLString)
                    .textFieldStyle(.plain)
                    .font(DS.Font.code)
                    .padding(8)
                    .background(DS.Color.surfaceSunken)
                    .clipShape(RoundedRectangle(cornerRadius: DS.Radius.sm, style: .continuous))
            }

            HStack(spacing: DS.Space.md) {
                backendStatusView
                Spacer()
                Button("Check Connection") {
                    checkBackend()
                }
                .buttonStyle(.dsSecondary)
            }

            helpText("Run the backend with: cd backend && GROQ_API_KEY=… python3 main.py")
        }
    }

    private var groqSection: some View {
        sectionCard(title: "Groq API", icon: "bolt.fill") {
            labeledField(label: "API Key") {
                SecureField("sk-...", text: $settings.groqAPIKey)
                    .textFieldStyle(.plain)
                    .font(DS.Font.code)
                    .padding(8)
                    .background(DS.Color.surfaceSunken)
                    .clipShape(RoundedRectangle(cornerRadius: DS.Radius.sm, style: .continuous))
            }
            labeledField(label: "Model (optional override)") {
                TextField("leave blank for backend default", text: $settings.groqModel)
                    .textFieldStyle(.plain)
                    .font(DS.Font.code)
                    .padding(8)
                    .background(DS.Color.surfaceSunken)
                    .clipShape(RoundedRectangle(cornerRadius: DS.Radius.sm, style: .continuous))
            }
            helpText("Get a key at console.groq.com. Leave model blank to use the backend's default (openai/gpt-oss-120b as of writing).")
        }
    }

    private var symbolicationSection: some View {
        sectionCard(title: "Crash Symbolication Inputs", icon: "ladybug.fill") {
            pathField(
                label: "App Binary",
                path: $settings.binaryPath,
                chooseFiles: true,
                help: "The executable inside the crashed app's .app bundle, e.g. MyApp.app/Contents/MacOS/MyApp. Use an unstripped Debug build, or atos won't have symbols to resolve."
            )

            labeledField(label: "Binary Name in Crash Log (optional)") {
                TextField("auto-detected from binary path if blank", text: $settings.targetBinaryName)
                    .textFieldStyle(.plain)
                    .font(DS.Font.code)
                    .padding(8)
                    .background(DS.Color.surfaceSunken)
                    .clipShape(RoundedRectangle(cornerRadius: DS.Radius.sm, style: .continuous))
            }
            helpText("Only needed if the binary's filename differs from the name shown in the crash log's frame lines.")

            Divider().overlay(DS.Color.hairline).padding(.vertical, DS.Space.xs)

            pathField(
                label: "Codebase Root",
                path: $settings.codebaseRootPath,
                chooseFiles: false,
                help: "Root folder the agent may read files from and search — your Xcode project's source folder (the one containing your .swift files), not DerivedData."
            )
        }
    }

    // MARK: - Building blocks

    @ViewBuilder
    private func sectionCard<Content: View>(title: String, icon: String, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: DS.Space.md) {
            HStack(spacing: 6) {
                Image(systemName: icon)
                    .font(.system(size: 12))
                    .foregroundStyle(DS.Color.accent)
                Text(title)
                    .font(DS.Font.sectionTitle)
            }
            content()
        }
        .padding(DS.Space.lg)
        .frame(maxWidth: .infinity, alignment: .leading)
        .dsCard()
    }

    @ViewBuilder
    private func labeledField<Content: View>(label: String, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: DS.Space.xs) {
            Text(label)
                .font(DS.Font.label)
                .foregroundStyle(DS.Color.textSecondary)
            content()
        }
    }

    /// A path row laid out vertically: label, then a full-width text
    /// field with a Choose… button beside it on its own row, then the
    /// full value echoed below (so two truncated paths never look
    /// identical), then help text.
    @ViewBuilder
    private func pathField(label: String, path: Binding<String>, chooseFiles: Bool, help: String) -> some View {
        VStack(alignment: .leading, spacing: DS.Space.xs) {
            Text(label)
                .font(DS.Font.label)
                .foregroundStyle(DS.Color.textSecondary)

            HStack(spacing: DS.Space.sm) {
                TextField("", text: path)
                    .textFieldStyle(.plain)
                    .font(DS.Font.code)
                    .lineLimit(1)
                    .truncationMode(.head)
                    .padding(8)
                    .background(DS.Color.surfaceSunken)
                    .clipShape(RoundedRectangle(cornerRadius: DS.Radius.sm, style: .continuous))

                Button("Choose…") {
                    choosePath(into: path, chooseFiles: chooseFiles)
                }
                .buttonStyle(.dsSecondary)
                .fixedSize()
            }

            if !path.wrappedValue.isEmpty {
                Text(path.wrappedValue)
                    .font(DS.Font.codeSmall)
                    .foregroundStyle(DS.Color.textTertiary)
                    .textSelection(.enabled)
                    .lineLimit(2)
                    .truncationMode(.middle)
            }

            helpText(help)
        }
    }

    @ViewBuilder
    private func helpText(_ text: String) -> some View {
        Text(text)
            .font(DS.Font.caption)
            .foregroundStyle(DS.Color.textTertiary)
            .fixedSize(horizontal: false, vertical: true)
    }

    @ViewBuilder
    private var backendStatusView: some View {
        if isCheckingBackend {
            HStack(spacing: 6) {
                ProgressView().controlSize(.small)
                Text("Checking…").font(DS.Font.caption).foregroundStyle(DS.Color.textSecondary)
            }
        } else if let backendReachable {
            StatusPill(
                text: backendReachable ? "Backend reachable" : "Backend not reachable",
                color: backendReachable ? DS.Color.success : DS.Color.danger,
                icon: backendReachable ? "checkmark" : "xmark"
            )
        } else {
            Text("Not checked yet").font(DS.Font.caption).foregroundStyle(DS.Color.textTertiary)
        }
    }

    // MARK: - Actions

    private func checkBackend() {
        isCheckingBackend = true
        Task {
            let client = BackendClient(baseURL: settings.backendURL)
            let reachable = await client.healthCheck()
            await MainActor.run {
                self.backendReachable = reachable
                self.isCheckingBackend = false
            }
        }
    }

    private func choosePath(into binding: Binding<String>, chooseFiles: Bool) {
        let panel = NSOpenPanel()
        panel.canChooseFiles = chooseFiles
        panel.canChooseDirectories = !chooseFiles
        panel.allowsMultipleSelection = false
        if panel.runModal() == .OK, let url = panel.url {
            binding.wrappedValue = url.path
        }
    }
}
