// BackendClient.swift
//
// Thin HTTP client for the local Python backend (see /backend in the
// project root). This replaces the old C++/Objective-C++ symbolication
// bridge entirely -- Swift no longer does any binary parsing or LLM
// orchestration itself, it just calls out to the Python service and
// renders what comes back.
//
// The backend must be running locally before you use the app:
//   cd backend && GROQ_API_KEY=sk-... python3 main.py
// (serves on http://127.0.0.1:8000 by default)

import Foundation

// MARK: - Wire models (mirror the Pydantic models in backend/main.py)

struct SymbolicatedFrame: Identifiable, Codable {
    let id = UUID()
    let index: Int
    let address: UInt64
    let binaryName: String
    let symbolName: String?
    let filePath: String?
    let lineNumber: Int?
    let displayLine: String

    enum CodingKeys: String, CodingKey {
        case index, address
        case binaryName = "binary_name"
        case symbolName = "symbol_name"
        case filePath = "file_path"
        case lineNumber = "line_number"
        case displayLine = "display_line"
    }
}

struct SymbolicatedCrash: Codable {
    let threadLabel: String
    let exceptionType: String
    let exceptionSubtype: String
    let frames: [SymbolicatedFrame]

    enum CodingKeys: String, CodingKey {
        case threadLabel = "thread_label"
        case exceptionType = "exception_type"
        case exceptionSubtype = "exception_subtype"
        case frames
    }
}

/// One step of agent activity, mirroring backend/agent_loop.py's
/// AgentStep.to_dict() shape. Streamed as NDJSON from /diagnose.
struct AgentStepDTO: Codable {
    let type: String // "thinking" | "tool_call" | "tool_result" | "fix_proposed" | "final_answer" | "error"
    let text: String?
    let toolName: String?
    let toolArguments: String?
    let toolResult: String?
    // populated only when type == "fix_proposed"
    let fixPath: String?
    let fixDiff: String?
    let fixExplanation: String?
    let fixOldContent: String?  // needed to call applyFix (staleness check)
    let fixNewContent: String?  // needed to call applyFix

    enum CodingKeys: String, CodingKey {
        case type, text
        case toolName = "tool_name"
        case toolArguments = "tool_arguments"
        case toolResult = "tool_result"
        case fixPath = "fix_path"
        case fixDiff = "fix_diff"
        case fixExplanation = "fix_explanation"
        case fixOldContent = "fix_old_content"
        case fixNewContent = "fix_new_content"
    }
}

// MARK: - Errors

enum BackendError: LocalizedError {
    case notReachable(String)
    case httpError(status: Int, detail: String)
    case decodingFailed(String)

    var errorDescription: String? {
        switch self {
        case .notReachable(let detail):
            return "Could not reach the backend service: \(detail)\n\nMake sure it's running: cd backend && python3 main.py"
        case .httpError(let status, let detail):
            return "Backend error (HTTP \(status)): \(detail)"
        case .decodingFailed(let detail):
            return "Failed to decode backend response: \(detail)"
        }
    }
}

// MARK: - Client

final class BackendClient {
    private let baseURL: URL
    private let session: URLSession

    init(baseURL: URL = URL(string: "http://127.0.0.1:8000")!, session: URLSession = .shared) {
        self.baseURL = baseURL
        self.session = session
    }

    /// Calls POST /symbolicate.
    func symbolicate(crashLogText: String, binaryPath: String, targetBinaryName: String?) async throws -> SymbolicatedCrash {
        var body: [String: Any] = [
            "crash_log_text": crashLogText,
            "binary_path": binaryPath,
        ]
        if let targetBinaryName, !targetBinaryName.isEmpty {
            body["target_binary_name"] = targetBinaryName
        }

        let data = try await postJSON(path: "/symbolicate", body: body)

        do {
            return try JSONDecoder().decode(SymbolicatedCrash.self, from: data)
        } catch {
            throw BackendError.decodingFailed(error.localizedDescription)
        }
    }

    /// Calls POST /diagnose and streams back AgentStepDTO events as they
    /// arrive, one per line of NDJSON. The returned AsyncThrowingStream
    /// lets the caller `for try await step in client.diagnose(...)`.
    func diagnose(
        crash: SymbolicatedCrash,
        codebaseRoot: String,
        groqAPIKey: String,
        groqModel: String?
    ) -> AsyncThrowingStream<AgentStepDTO, Error> {
        AsyncThrowingStream { continuation in
            Task {
                do {
                    let crashDict = try crash.asDictionary()
                    var body: [String: Any] = [
                        "crash": crashDict,
                        "codebase_root": codebaseRoot,
                        "groq_api_key": groqAPIKey,
                    ]
                    if let groqModel, !groqModel.isEmpty {
                        body["groq_model"] = groqModel
                    }

                    var request = URLRequest(url: baseURL.appendingPathComponent("diagnose"))
                    request.httpMethod = "POST"
                    request.setValue("application/json", forHTTPHeaderField: "Content-Type")
                    request.httpBody = try JSONSerialization.data(withJSONObject: body)

                    let (bytes, response) = try await session.bytes(for: request)

                    guard let httpResponse = response as? HTTPURLResponse else {
                        throw BackendError.notReachable("response was not an HTTP response")
                    }
                    guard (200...299).contains(httpResponse.statusCode) else {
                        // Error responses aren't streamed NDJSON, they're
                        // a single JSON body -- read it fully for the message.
                        var collected = Data()
                        for try await byte in bytes { collected.append(byte) }
                        let detail = Self.extractErrorDetail(from: collected)
                        throw BackendError.httpError(status: httpResponse.statusCode, detail: detail)
                    }

                    for try await line in bytes.lines {
                        guard !line.isEmpty else { continue }
                        guard let lineData = line.data(using: .utf8) else { continue }
                        let step = try JSONDecoder().decode(AgentStepDTO.self, from: lineData)
                        continuation.yield(step)
                    }
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
        }
    }

    /// Calls POST /apply_fix to write an approved fix to disk. Only call
    /// this after the user has reviewed the diff and explicitly
    /// approved it. Throws BackendError.httpError(status: 409, ...) if
    /// the file changed on disk since the fix was proposed -- the
    /// caller should surface that distinctly (e.g. "re-run Diagnose")
    /// rather than treating it like a generic failure.
    func applyFix(
        codebaseRoot: String,
        path: String,
        expectedOldContent: String,
        newContent: String
    ) async throws -> String {
        let body: [String: Any] = [
            "codebase_root": codebaseRoot,
            "path": path,
            "expected_old_content": expectedOldContent,
            "new_content": newContent,
        ]
        let data = try await postJSON(path: "/apply_fix", body: body)

        struct ApplyFixResponse: Codable {
            let backupPath: String
            enum CodingKeys: String, CodingKey { case backupPath = "backup_path" }
        }
        do {
            let decoded = try JSONDecoder().decode(ApplyFixResponse.self, from: data)
            return decoded.backupPath
        } catch {
            throw BackendError.decodingFailed(error.localizedDescription)
        }
    }

    /// Calls GET /health -- useful for a "backend reachable?" check the
    /// UI can show before the user pastes a crash log.
    func healthCheck() async -> Bool {
        guard let url = URL(string: "health", relativeTo: baseURL) else { return false }
        guard let (_, response) = try? await session.data(from: url) else { return false }
        return (response as? HTTPURLResponse)?.statusCode == 200
    }

    // MARK: - Internal

    private func postJSON(path: String, body: [String: Any]) async throws -> Data {
        var request = URLRequest(url: baseURL.appendingPathComponent(String(path.dropFirst())))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: body)

        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
        } catch {
            throw BackendError.notReachable(error.localizedDescription)
        }

        guard let httpResponse = response as? HTTPURLResponse else {
            throw BackendError.notReachable("response was not an HTTP response")
        }
        guard (200...299).contains(httpResponse.statusCode) else {
            let detail = Self.extractErrorDetail(from: data)
            throw BackendError.httpError(status: httpResponse.statusCode, detail: detail)
        }
        return data
    }

    /// FastAPI error bodies come in two shapes:
    /// 1. `{"detail": "some string"}` -- from our own `HTTPException(...)` calls
    /// 2. `{"detail": [{"type": "missing", "loc": [...], "msg": "...", ...}, ...]}`
    ///    -- Pydantic's automatic request-validation errors (422s), where
    ///    `detail` is an ARRAY of error objects, not a string.
    /// A naive `["detail"] as? String` cast silently fails on shape 2 and
    /// masks the real reason behind a generic "unknown error" -- which is
    /// exactly what happened here. This handles both shapes properly.
    private static func extractErrorDetail(from data: Data) -> String {
        guard let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return String(data: data, encoding: .utf8) ?? "unknown error (and response body wasn't valid JSON or UTF-8)"
        }
        guard let detail = json["detail"] else {
            return "unknown error (no 'detail' field in response)"
        }
        if let detailString = detail as? String {
            return detailString
        }
        if let detailArray = detail as? [[String: Any]] {
            let messages = detailArray.map { entry -> String in
                let loc = (entry["loc"] as? [Any])?.map { "\($0)" }.joined(separator: ".") ?? "?"
                let msg = entry["msg"] as? String ?? "invalid"
                return "\(loc): \(msg)"
            }
            return messages.joined(separator: "; ")
        }
        // Last resort: just stringify whatever shape it actually was.
        return "\(detail)"
    }
}

// MARK: - Helpers

private extension Encodable {
    /// Round-trips through JSONEncoder/JSONSerialization to get a
    /// [String: Any] dictionary suitable for embedding inside another
    /// JSONSerialization-built request body (used for the `crash` field
    /// of the /diagnose request, which the client received as a typed
    /// SymbolicatedCrash from a prior /symbolicate call).
    func asDictionary() throws -> [String: Any] {
        let data = try JSONEncoder().encode(self)
        guard let dict = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw BackendError.decodingFailed("could not convert encoded crash back to a dictionary")
        }
        return dict
    }
}
