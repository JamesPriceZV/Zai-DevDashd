import Foundation
import FoundationModels

struct Request: Codable {
    var mode: String?
    var subject: String?
    var job_type: String?
    var outcome: String?
    var failing_step: String?
    var first_error: String?
    var lane: String?
    var branch: String?
    var pr: String?
    var brief: String?
    var actions: String?
    var clock: String?
    var bucket: String?
    var context: String?
}

@Generable
struct RunLabel: Sendable {
    @Guide(description: "At most 8 words. Plain name of what this run did. No hash, no secret.")
    var label: String
    @Guide(description: "One plain sentence about the outcome. Use only the given facts.")
    var summary: String
}

@Generable
struct TitleLabel: Sendable {
    @Guide(description: "At most 22 words. What the job is for. Form: lane or pull request, then the outcome being pursued. No hash, no secret. Never say a step is running.")
    var label: String
    @Guide(description: "One outcome sentence: what done looks like. Do not describe the current step. Do not say a step failed.")
    var goal: String
    @Guide(description: "One plain sentence about the current action. If a clock time is given, copy it exactly and call it a commit.")
    var summary: String
}

@Generable
struct Analysis: Sendable {
    @Guide(description: "The goal, using only the supplied text.")
    var goal: String
    @Guide(description: "Up to four next steps from the supplied numbers.", .maximumCount(4))
    var plan: [String]
    @Guide(description: "yes, at_risk, or off_track.", .anyOf(["yes", "at_risk", "off_track"]))
    var on_track: String
    @Guide(description: "The main risk, citing a supplied number, or none.")
    var risk: String
    @Guide(description: "One different next step, or none. Do not invent a cause.")
    var would_do_differently: String
    @Guide(description: "Notes that only restate the supplied findings.")
    var pattern_notes: String
}

struct Reply: Codable {
    var label: String
    var summary: String
    var goal: String
    var backend: String
}

struct AnalysisReply: Codable {
    var goal: String
    var plan: [String]
    var on_track: String
    var risk: String
    var would_do_differently: String
    var pattern_notes: String
    var backend: String
}

enum LabelError: Error, CustomStringConvertible {
    case unavailable(String)
    case badInput(String)
    case timedOut

    var description: String {
        switch self {
        case .unavailable(let why): return why
        case .badInput(let why): return why
        case .timedOut: return "timed out"
        }
    }
}

let labelInstructions = """
You write status-board names for software runs.
The label is at most 8 words and states the result, not only the subject.
The summary is one plain sentence.
Use only the facts in the input. Do not invent causes, files, or results.
Do not include secrets, tokens, or commit hashes.
"""

let titleInstructions = """
You name one engineering job for a status board.
The label is at most 22 words and says what the job is for, like: Example lane · Resolve the review findings for synthetic PR #1001.
The goal is one outcome sentence about the finished result. Do not describe the current step. Do not say a step failed.
The summary is one sentence about the current action.
Use only the facts in the input. Do not invent files, results, or times.
If the clock line gives a time, copy that time exactly and call it a commit, not a push.
Never use these titles: Engineering lane running, Mac build running, Pipeline running, The engineering lane is running.
Do not include secrets, tokens, or commit hashes.
"""

let analyzeInstructions = """
You judge one engineering job from the JSON numbers you are given.
Use only those numbers and names. If a figure is absent, say it is not in the input.
Do not invent causes, owners, or results.
on_track is yes, at_risk, or off_track.
The plan has at most four short steps.
"""

@main
struct FMLabel {
    static func main() async {
        do {
            let raw = FileHandle.standardInput.readDataToEndOfFile()
            let request = try JSONDecoder().decode(Request.self, from: raw)
            let mode = (request.mode ?? "label").lowercased()
            if mode == "analyze" {
                let (content, backend) = try await analyze(request.context ?? "")
                let reply = AnalysisReply(
                    goal: clip(content.goal, 400),
                    plan: Array(content.plan.prefix(4)).map { clip($0, 180) },
                    on_track: normalizeTrack(content.on_track),
                    risk: clip(content.risk, 300),
                    would_do_differently: clip(content.would_do_differently, 300),
                    pattern_notes: clip(content.pattern_notes, 400),
                    backend: backend
                )
                writeJSON(reply)
                return
            }
            if mode == "title" {
                let (session, backend) = try openSession(titleInstructions)
                let content = try await withTimeout(seconds: 18) {
                    let response = try await session.respond(to: titlePrompt(request), generating: TitleLabel.self)
                    return response.content
                }
                let reply = Reply(
                    label: clampWords(content.label, max: 22),
                    summary: oneSentence(content.summary),
                    goal: oneSentence(content.goal),
                    backend: backend
                )
                writeJSON(reply)
                return
            }
            let (session, backend) = try openSession(labelInstructions)
            let content = try await withTimeout(seconds: 18) {
                let response = try await session.respond(to: labelPrompt(request), generating: RunLabel.self)
                return response.content
            }
            let reply = Reply(
                label: clampWords(content.label, max: 8),
                summary: oneSentence(content.summary),
                goal: "",
                backend: backend
            )
            writeJSON(reply)
        } catch let error as LabelError {
            fputs(error.description + "\n", stderr)
            exit(error.isUnavailable ? 2 : 1)
        } catch {
            fputs(String(describing: error) + "\n", stderr)
            exit(1)
        }
    }
}

func writeJSON<T: Encodable>(_ value: T) {
    do {
        let data = try JSONEncoder().encode(value)
        FileHandle.standardOutput.write(data)
        FileHandle.standardOutput.write(Data("\n".utf8))
    } catch {
        fputs(String(describing: error) + "\n", stderr)
        exit(1)
    }
}

func analyze(_ context: String) async throws -> (Analysis, String) {
    let prompt = "Use only these numbers and names.\n" + clip(context, 8000)
    let onDevice = SystemLanguageModel.default
    if case .available = onDevice.availability {
        do {
            let session = LanguageModelSession(model: onDevice, instructions: analyzeInstructions)
            let content = try await withTimeout(seconds: 14) {
                let response = try await session.respond(to: prompt, generating: Analysis.self)
                return response.content
            }
            return (content, "on-device")
        } catch {
            fputs("on-device analyze failed: \(error)\n", stderr)
        }
    } else if case .unavailable(let reason) = onDevice.availability {
        fputs("on-device unavailable: \(onDeviceReason(reason))\n", stderr)
    }
    let cloud = PrivateCloudComputeLanguageModel()
    switch cloud.availability {
    case .available:
        do {
            let session = LanguageModelSession(model: cloud, instructions: analyzeInstructions)
            let content = try await withTimeout(seconds: 14) {
                let response = try await session.respond(to: prompt, generating: Analysis.self)
                return response.content
            }
            return (content, "private-cloud")
        } catch {
            throw LabelError.badInput("private cloud analyze failed: \(error)")
        }
    case .unavailable(let reason):
        throw LabelError.unavailable("analyze unavailable: \(cloudReason(reason))")
    }
}

func normalizeTrack(_ value: String) -> String {
    let token = value.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
    if token == "yes" || token == "at_risk" || token == "off_track" {
        return token
    }
    return "at_risk"
}

extension LabelError {
    var isUnavailable: Bool {
        if case .unavailable = self { return true }
        return false
    }
}

func openSession(_ text: String) throws -> (LanguageModelSession, String) {
    let onDevice = SystemLanguageModel.default
    switch onDevice.availability {
    case .available:
        return (LanguageModelSession(model: onDevice, instructions: text), "on-device")
    case .unavailable(let reason):
        fputs("on-device unavailable: \(onDeviceReason(reason))\n", stderr)
    }
    let cloud = PrivateCloudComputeLanguageModel()
    switch cloud.availability {
    case .available:
        return (LanguageModelSession(model: cloud, instructions: text), "private-cloud")
    case .unavailable(let reason):
        throw LabelError.unavailable(
            "on-device unavailable and private cloud compute unavailable: \(cloudReason(reason))"
        )
    }
}

func onDeviceReason(_ reason: SystemLanguageModel.Availability.UnavailableReason) -> String {
    switch reason {
    case .deviceNotEligible: return "deviceNotEligible"
    case .appleIntelligenceNotEnabled: return "appleIntelligenceNotEnabled"
    case .modelNotReady: return "modelNotReady"
    @unknown default: return "unknown"
    }
}

func cloudReason(_ reason: PrivateCloudComputeLanguageModel.Availability.UnavailableReason) -> String {
    switch reason {
    case .deviceNotEligible: return "deviceNotEligible"
    case .systemNotReady: return "systemNotReady"
    @unknown default: return "unknown"
    }
}

func labelPrompt(_ request: Request) -> String {
    var lines = [
        "Subject: \(clip(request.subject, 200))",
        "Job type: \(clip(request.job_type, 80))",
        "Outcome: \(clip(request.outcome, 160))",
    ]
    let step = clip(request.failing_step, 80)
    if !step.isEmpty {
        lines.append("Failing step: \(step)")
    }
    let error = clip(request.first_error, 240)
    if !error.isEmpty {
        lines.append("First error: \(error)")
    }
    return lines.joined(separator: "\n")
}

func titlePrompt(_ request: Request) -> String {
    [
        "Lane: \(clip(request.lane, 40))",
        "Branch: \(clip(request.branch, 120))",
        "Pull request: \(clip(request.pr, 16))",
        "Action bucket: \(clip(request.bucket, 40))",
        "Clock: \(clip(request.clock, 80))",
        "Brief:",
        clip(request.brief, 1200),
        "Recent actions:",
        clip(request.actions, 800),
    ].joined(separator: "\n")
}

func clip(_ value: String?, _ max: Int) -> String {
    let text = (value ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
    if text.count <= max { return text }
    return String(text.prefix(max))
}

func clampWords(_ text: String, max: Int) -> String {
    let words = text.split(whereSeparator: { $0.isWhitespace })
    return words.prefix(max).joined(separator: " ")
}

func oneSentence(_ text: String) -> String {
    let flat = text.split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")
    guard let match = flat.range(of: #"^.*?[.!?](?:\s|$)"#, options: .regularExpression) else {
        return flat
    }
    return String(flat[match]).trimmingCharacters(in: .whitespaces)
}

func withTimeout<T: Sendable>(seconds: Double, _ operation: @escaping @Sendable () async throws -> T) async throws -> T {
    try await withThrowingTaskGroup(of: T.self) { group in
        group.addTask {
            try await operation()
        }
        group.addTask {
            try await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000))
            throw LabelError.timedOut
        }
        guard let value = try await group.next() else {
            throw LabelError.timedOut
        }
        group.cancelAll()
        return value
    }
}
