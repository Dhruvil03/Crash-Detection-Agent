# CrashAgent

A macOS app that symbolicates Apple crash reports and runs an AI agent to diagnose them — reading your codebase, tracing the root cause, and proposing a code fix you can approve in one click.

---

## How it works

1. **Paste or drop** a `.ips` / `.crash` report into the sidebar.
2. Click **Symbolicate** — the app sends the log to a local Python backend, which shells out to `atos` to resolve raw addresses into human-readable symbol names.
3. Click **Diagnose** — the backend runs an LLM agent (via Groq) that searches your codebase, reads relevant files, and streams its investigation live.
4. The agent's work appears as a vertical **investigation timeline** — thinking steps, tool calls, results — and ends with a root-cause explanation and an optional **code fix**.
5. Review the unified diff, then **Approve** to write the fix to disk (a backup is saved automatically) or **Reject** to leave things untouched.

The Swift app is a thin client: all symbolication, LLM orchestration, and file I/O live in the Python backend. Swift handles input, display, and the approve/reject gate.

---

## Architecture

```
CrashAgentApp (macOS SwiftUI)
        │  HTTP (localhost:8000)
        ▼
  Python backend  ──►  atos (symbolication)
        │          ──►  Groq API (LLM agent)
        │          ──►  codebase files (read / search)
        ▼
  apply_fix endpoint  ──►  disk (with backup)
```

### Swift layers

| File | Purpose |
|------|---------|
| [CrashAgentApp.swift](CrashAgentApp/CrashAgentApp.swift) | App entry point |
| [Views/ContentView.swift](CrashAgentApp/Views/ContentView.swift) | Main window: sidebar input + investigation timeline |
| [Views/SettingsView.swift](CrashAgentApp/Views/SettingsView.swift) | Settings sheet (backend URL, Groq key, paths) |
| [Backend/BackendClient.swift](CrashAgentApp/Backend/BackendClient.swift) | HTTP client wrapping `/symbolicate`, `/diagnose`, `/apply_fix`, `/health` |
| [Models/AppSettings.swift](CrashAgentApp/Models/AppSettings.swift) | `@AppStorage`-backed settings model |
| [DesignSystem.swift](CrashAgentApp/DesignSystem.swift) | Design tokens (color, type, spacing) and reusable SwiftUI components |

### Backend endpoints

| Method | Path | What it does |
|--------|------|--------------|
| `POST` | `/symbolicate` | Runs `atos` to resolve crash addresses into symbols |
| `POST` | `/diagnose` | Streams NDJSON agent steps (thinking, tool calls, fix proposals) |
| `POST` | `/apply_fix` | Writes an approved fix to disk after a staleness check; returns backup path |
| `GET` | `/health` | Returns 200 when the service is up |

---

## Requirements

- **macOS** (filesystem access and `atos` are required; iOS sandbox cannot support this)
- **Xcode** to build the Swift app
- **Python 3.10+** for the backend
- A **[Groq](https://console.groq.com) API key**
- An **unstripped Debug build** of the app binary you're diagnosing (so `atos` has symbols to resolve)

---

## Setup

### 1. Start the Python backend

```bash
cd backend
pip install -r requirements.txt          # first time only
GROQ_API_KEY=sk-... python3 main.py
# Serves on http://127.0.0.1:8000
```

### 2. Build and run the macOS app

Open `CrashAgentApp.xcodeproj` in Xcode and run the `CrashAgentApp` scheme (⌘R).

### 3. Configure in Settings (gear icon)

| Setting | What to enter |
|---------|--------------|
| **Backend URL** | `http://127.0.0.1:8000` (default — change only if you moved the service) |
| **Groq API Key** | Your key from [console.groq.com](https://console.groq.com) |
| **Groq Model** | Leave blank to use the backend's default, or enter a specific model ID |
| **App Binary** | Path to the executable inside the crashed app's `.app` bundle, e.g. `MyApp.app/Contents/MacOS/MyApp` — must be an unstripped Debug build |
| **Binary Name in Crash Log** | Leave blank; only needed if the filename differs from the name in crash frame lines |
| **Codebase Root** | Root folder of your Xcode project source (the folder containing your `.swift` files, not `DerivedData`) |

---

## Usage

1. Click **Choose File…** or drag a `.ips` / `.crash` / `.log` file onto the text area, or paste the report text directly.
2. Click **Symbolicate**. The right panel shows the symbolicated stack trace with exception type and thread label.
3. Click **Diagnose**. The investigation timeline streams in real time — each step shows what the agent is thinking, which files it's reading, and what it found.
4. When the agent proposes a fix, a diff card appears with **Approve & Write to File** / **Reject** buttons. Approving writes the change to disk and saves a `.bak` backup alongside the original.

---

## Notes

- **API key security**: the Groq key is currently stored in `UserDefaults` via `@AppStorage`. This is acceptable for local/personal use; move it to Keychain before distributing the app to others.
- **Staleness check**: if you edit a file between when Diagnose runs and when you approve a fix, the backend will return HTTP 409 and the fix will not be applied. Re-run Diagnose to get a fresh proposal.
- **Debug builds only**: `atos` resolves symbols from DWARF debug info. A stripped Release build will produce unresolved addresses and the agent won't have useful symbols to work with.
