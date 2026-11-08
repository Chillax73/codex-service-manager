import AppKit
import SwiftUI
import Foundation
import Darwin

func argument(_ name: String) -> String? {
    guard let index = CommandLine.arguments.firstIndex(of: name), index + 1 < CommandLine.arguments.count else { return nil }
    return CommandLine.arguments[index + 1]
}
let statePath = argument("--root") ?? ProcessInfo.processInfo.environment["CSM_STATE_DIR"] ??
    NSHomeDirectory() + "/Library/Application Support/Codex Service Manager"
let enginePath = argument("--engine") ?? Bundle.main.resourcePath! + "/backend/engine.py"

func rpc(_ request: [String: Any]) throws -> Any {
    let process = Process()
    process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
    process.arguments = [enginePath, "--root", statePath, "rpc"]
    process.environment = ProcessInfo.processInfo.environment.merging(["PYTHONDONTWRITEBYTECODE": "1"]) { _, new in new }
    let input = Pipe(), output = Pipe()
    process.standardInput = input; process.standardOutput = output; process.standardError = FileHandle.nullDevice
    try process.run()
    try input.fileHandleForWriting.write(contentsOf: JSONSerialization.data(withJSONObject: request))
    try input.fileHandleForWriting.close()
    let data = output.fileHandleForReading.readDataToEndOfFile()
    process.waitUntilExit()
    guard let result = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
        throw NSError(domain: "CSM", code: 1, userInfo: [NSLocalizedDescriptionKey: "管理组件返回了无效数据"])
    }
    guard result["ok"] as? Bool == true else {
        var info = [NSLocalizedDescriptionKey: result["error"] as? String ?? "操作失败"]
        info["rpc_error_code"] = result["error_code"] as? String
        throw NSError(domain: "CSM", code: 1, userInfo: info)
    }
    return result["result"] ?? NSNull()
}

final class CachedSnapshotConnection {
    private let queue = DispatchQueue(label: "csm.snapshot.connection", qos: .utility)
    private var process: Process?
    private var input: FileHandle?
    private var output: FileHandle?
    private var buffered = Data()
    private var nextAttempt: TimeInterval = 0
    private var backoff: TimeInterval = 1

    func read(visible: Bool, priorityProject: String?) throws -> [String: Any] {
        try queue.sync {
            guard ProcessInfo.processInfo.systemUptime >= nextAttempt else { throw unavailable() }
            do {
                if let process = process, !process.isRunning { throw unavailable() }
                if process == nil { try start() }
                guard let input = input, let output = output else { throw unavailable() }
                var request: [String: Any] = ["action": "snapshot_cached", "visible": visible]
                request["priority_project"] = priorityProject
                try input.write(contentsOf: JSONSerialization.data(withJSONObject: request) + Data([10]))
                let line = try readLine(output)
                guard let response = try JSONSerialization.jsonObject(with: line) as? [String: Any],
                      response["ok"] as? Bool == true, let result = response["result"] as? [String: Any]
                else { throw unavailable() }
                backoff = 1
                nextAttempt = 0
                return result
            } catch {
                reset()
                nextAttempt = ProcessInfo.processInfo.systemUptime + backoff
                backoff = min(backoff * 2, 16)
                throw error
            }
        }
    }

    func close() {
        queue.async { self.reset(terminate: false) }
    }

    private func start() throws {
        let worker = Process()
        let requests = Pipe(), responses = Pipe()
        let writer = requests.fileHandleForWriting
        guard fcntl(writer.fileDescriptor, F_SETNOSIGPIPE, 1) != -1 else { throw unavailable() }
        worker.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        worker.arguments = [enginePath, "--root", statePath, "snapshot-worker"]
        worker.environment = ProcessInfo.processInfo.environment.merging(["PYTHONDONTWRITEBYTECODE": "1"]) { _, new in new }
        worker.standardInput = requests
        worker.standardOutput = responses
        worker.standardError = FileHandle.nullDevice
        try worker.run()
        process = worker
        input = writer
        output = responses.fileHandleForReading
    }

    private func readLine(_ handle: FileHandle) throws -> Data {
        let deadline = ProcessInfo.processInfo.systemUptime + 4
        var bytes = [UInt8](repeating: 0, count: 8192)
        while buffered.count <= 64 * 1024 * 1024 {
            if let newline = buffered.firstIndex(of: 10) {
                let line = buffered.prefix(upTo: newline)
                buffered.removeSubrange(...newline)
                return Data(line)
            }
            let left = deadline - ProcessInfo.processInfo.systemUptime
            guard left > 0 else { throw unavailable() }
            var descriptor = pollfd(fd: handle.fileDescriptor, events: Int16(POLLIN), revents: 0)
            let ready = poll(&descriptor, 1, Int32((left * 1000).rounded(.up)))
            if ready < 0 && errno == EINTR { continue }
            guard ready > 0 else { throw unavailable() }
            let count = bytes.withUnsafeMutableBytes { Darwin.read(descriptor.fd, $0.baseAddress, $0.count) }
            if count < 0 && errno == EINTR { continue }
            guard count > 0 else { throw unavailable() }
            buffered.append(contentsOf: bytes.prefix(count))
        }
        throw unavailable()
    }

    private func reset(terminate: Bool = true) {
        try? input?.close()
        try? output?.close()
        if terminate, let process = process, process.isRunning { process.terminate() }
        input = nil
        output = nil
        process = nil
        buffered.removeAll()
    }

    private func unavailable() -> NSError {
        NSError(domain: "CSMSnapshot", code: 1, userInfo: [NSLocalizedDescriptionKey: "后台状态检查暂不可用"])
    }
}

enum ExitRequestOutcome {
    case acknowledged, busy, unconfirmed
}

func executeExitRequest(_ event: [String: Any], attempts: Int = 60,
                        request: ([String: Any]) throws -> Any = rpc,
                        wait: () -> Void = { Thread.sleep(forTimeInterval: 1) }) -> ExitRequestOutcome {
    var payload = event
    payload["action"] = "codex_exit"
    for attempt in 0..<attempts {
        do {
            _ = try request(payload)
            return .acknowledged
        } catch {
            let failure = error as NSError
            guard failure.domain == "CSM", failure.userInfo["rpc_error_code"] as? String == "mutation_busy" else {
                return .unconfirmed
            }
            if attempt + 1 < attempts { wait() }
        }
    }
    return .busy
}

final class ExitWatcher {
    var targetBundles = ["com.openai.codex"]
    var instances: Set<pid_t> = []
    var tokens: [NSObjectProtocol] = []
    var timer: Timer?
    let exitWork = DispatchQueue(label: "csm.exit-watcher.policy")
    let heartbeatWork = DispatchQueue(label: "csm.exit-watcher.heartbeat")
    var lockFD: Int32 = -1
    var lastEvent = "观察器启动"
    private var pendingExitReport: [String: Any]?
    private var heartbeatInFlight = false

    func start() {
        try? FileManager.default.createDirectory(atPath: statePath, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        lockFD = open(statePath + "/watcher.lock", O_CREAT | O_RDWR, 0o600)
        guard lockFD >= 0, flock(lockFD, LOCK_EX | LOCK_NB) == 0 else { exit(0) }
        if let data = try? Data(contentsOf: URL(fileURLWithPath: statePath + "/watcher-config.json")),
           let config = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let bundles = config["bundle_ids"] as? [String], !bundles.isEmpty { targetBundles = bundles }
        instances = Set(NSWorkspace.shared.runningApplications.filter { targetBundles.contains($0.bundleIdentifier ?? "") }.map(\.processIdentifier))
        let center = NSWorkspace.shared.notificationCenter
        tokens.append(center.addObserver(forName: NSWorkspace.didLaunchApplicationNotification, object: nil, queue: .main) { [weak self] event in
            guard let self = self, let app = event.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication,
                  self.targetBundles.contains(app.bundleIdentifier ?? "") else { return }
            self.instances.insert(app.processIdentifier)
            self.lastEvent = "目标应用启动"
            self.heartbeat()
        })
        tokens.append(center.addObserver(forName: NSWorkspace.didTerminateApplicationNotification, object: nil, queue: .main) { [weak self] event in
            guard let self = self, let app = event.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication,
                  self.targetBundles.contains(app.bundleIdentifier ?? "") else { return }
            let wasKnown = self.instances.contains(app.processIdentifier)
            self.instances.remove(app.processIdentifier)
            let live = NSWorkspace.shared.runningApplications.filter {
                self.targetBundles.contains($0.bundleIdentifier ?? "") && !$0.isTerminated && $0.processIdentifier != app.processIdentifier
            }
            self.instances.formUnion(live.map(\.processIdentifier))
            guard wasKnown, self.instances.isEmpty else { self.heartbeat(); return }
            self.lastEvent = "最后一个目标应用实例已终止"
            let event: [String: Any] = [
                "event_id": UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased(),
                "event_at": Date().timeIntervalSince1970
            ]
            self.exitWork.async {
                let outcome = executeExitRequest(event)
                guard outcome != .acknowledged else { return }
                var report = event
                report["action"] = "codex_exit_unconfirmed"
                report["busy"] = outcome == .busy
                let pending = report
                DispatchQueue.main.async {
                    self.pendingExitReport = pending
                    self.heartbeat()
                }
            }
            self.heartbeat()
        })
        timer = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in self?.heartbeat() }
        heartbeat()
        RunLoop.main.run()
    }
    func heartbeat() {
        guard !heartbeatInFlight else { return }
        heartbeatInFlight = true
        let bundles = targetBundles, pids = instances.map(Int.init), event = lastEvent, report = pendingExitReport
        heartbeatWork.async {
            defer { DispatchQueue.main.async { self.heartbeatInFlight = false } }
            let ps = Process(); let pipe = Pipe()
            ps.executableURL = URL(fileURLWithPath: "/bin/ps")
            ps.arguments = ["-p", String(getpid()), "-o", "lstart="]
            ps.standardOutput = pipe
            guard (try? ps.run()) != nil else { return }
            let birth = String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8)?.split(whereSeparator: { $0.isWhitespace }).joined(separator: " ") ?? ""
            ps.waitUntilExit()
            _ = try? rpc(["action": "watcher_event", "watcher": ["pid": Int(getpid()), "birth": birth,
                "bundle_ids": bundles, "instances": pids, "updated": Date().timeIntervalSince1970,
                "last_event": event]])
            if let report = report, (try? rpc(report)) != nil {
                DispatchQueue.main.async {
                    if self.pendingExitReport?["event_id"] as? String == report["event_id"] as? String {
                        self.pendingExitReport = nil
                    }
                }
            }
        }
    }
}
