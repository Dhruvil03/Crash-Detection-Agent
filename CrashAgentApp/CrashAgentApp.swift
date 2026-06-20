// CrashAgentApp.swift
//
// App entry point. macOS-first per the project design (filesystem
// access + process-level work needed for symbolication and codebase
// search isn't available in the iOS sandbox -- see design notes in the
// project README).

import SwiftUI

@main
struct CrashAgentApp: App {
    var body: some Scene {
        WindowGroup {
            ContentView()
        }
        .windowResizability(.contentSize)
    }
}
