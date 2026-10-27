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
    let input = Pipe(), output = Pipe(), errors = Pipe()
    process.standardInput = input; process.standardOutput = output; process.standardError = errors
    try process.run()
    try input.fileHandleForWriting.write(contentsOf: JSONSerialization.data(withJSONObject: request))
    try input.fileHandleForWriting.close()
    let data = output.fileHandleForReading.readDataToEndOfFile()
    process.waitUntilExit()
    guard let result = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
        throw NSError(domain: "CSM", code: 1, userInfo: [NSLocalizedDescriptionKey: "管理组件返回了无效数据"])
    }
    guard result["ok"] as? Bool == true else {
        throw NSError(domain: "CSM", code: 1, userInfo: [NSLocalizedDescriptionKey: result["error"] as? String ?? "操作失败"])
    }
    return result["result"] ?? NSNull()
}

final class ExitWatcher {
    var targetBundles = ["com.openai.codex"]
    var instances: Set<pid_t> = []
    var tokens: [NSObjectProtocol] = []
    var timer: Timer?
    let work = DispatchQueue(label: "csm.exit-watcher")
    var lockFD: Int32 = -1
    var lastEvent = "观察器启动"
    var lastError = ""

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
            self.work.async {
                var failure: String? = nil
                // Mutating UI/launch requests can hold the lock. Retry this explicit lifecycle event.
                for _ in 0..<60 {
                    do { _ = try rpc(["action": "codex_exit"]); failure = nil; break }
                    catch {
                        failure = error.localizedDescription
                        if !error.localizedDescription.contains("另一个操作") { break }
                        Thread.sleep(forTimeInterval: 1)
                    }
                }
                DispatchQueue.main.async { self.lastError = failure ?? ""; self.heartbeat() }
            }
            self.heartbeat()
        })
        timer = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in self?.heartbeat() }
        heartbeat()
        RunLoop.main.run()
    }
    func heartbeat() {
        let bundles = targetBundles, pids = instances.map(Int.init), event = lastEvent, error = lastError
        work.async {
            let ps = Process(); let pipe = Pipe()
            ps.executableURL = URL(fileURLWithPath: "/bin/ps")
            ps.arguments = ["-p", String(getpid()), "-o", "lstart="]
            ps.standardOutput = pipe
            try? ps.run()
            let birth = String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8)?.split(whereSeparator: { $0.isWhitespace }).joined(separator: " ") ?? ""
            ps.waitUntilExit()
            _ = try? rpc(["action": "watcher_event", "watcher": ["pid": Int(getpid()), "birth": birth,
                "bundle_ids": bundles, "instances": pids, "updated": Date().timeIntervalSince1970,
                "last_event": event, "error": error]])
        }
    }
}
