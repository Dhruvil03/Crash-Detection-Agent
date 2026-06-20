// AppSettings.swift
//
// Settings now mostly live server-side (Groq key is passed per-request
// to the backend, not used by Swift directly). What's left here is just
// what the *backend* needs to know per diagnosis: the binary path (for
// atos), the codebase root (for the agent's tools), and the Groq
// key/model to forward in the request.
//
// Security note: same caveat as before -- groqAPIKey is stored via
// @AppStorage (UserDefaults) for this prototype. Move to Keychain
// before sharing this app with anyone else.

import Foundation
import SwiftUI
import Combine

@MainActor
final class AppSettings: ObservableObject {
    @AppStorage("groqAPIKey") var groqAPIKey: String = ""
    @AppStorage("groqModel") var groqModel: String = "" // empty = let backend use its default
    @AppStorage("binaryPath") var binaryPath: String = ""
    @AppStorage("targetBinaryName") var targetBinaryName: String = "" // empty = derive from binaryPath
    @AppStorage("codebaseRootPath") var codebaseRootPath: String = ""
    @AppStorage("backendURLString") var backendURLString: String = "http://127.0.0.1:8000"

    var backendURL: URL {
        URL(string: backendURLString) ?? URL(string: "http://127.0.0.1:8000")!
    }

    var isConfigured: Bool {
        !groqAPIKey.isEmpty && !binaryPath.isEmpty && !codebaseRootPath.isEmpty
    }
}
