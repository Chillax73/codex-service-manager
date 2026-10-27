import AppKit
import SwiftUI
import Foundation
import Darwin

struct ProjectItem: Identifiable, Equatable {
    let id: String, name: String, cwd: String
    let manual: Bool, codexExit: Bool, captureEnabled: Bool
    init(_ data: [String: Any]) {
        id = data["id"] as? String ?? ""; name = data["name"] as? String ?? ""
        cwd = data["cwd"] as? String ?? ""
        captureEnabled = data["capture_enabled"] as? Bool ?? true
        manual = (data["manual"] as? NSNumber)?.boolValue ?? true
        codexExit = (data["codex_exit"] as? NSNumber)?.boolValue ?? false
    }
}
struct DeletedProjectItem: Identifiable {
    let id: String, name: String, cwd: String
    let deletedAt: Double
    init(_ data: [String: Any]) {
        id = data["id"] as? String ?? ""; name = data["name"] as? String ?? ""; cwd = data["cwd"] as? String ?? ""
        deletedAt = data["deleted_at"] as? Double ?? 0
    }
}
struct ServiceItem: Identifiable {
    let raw: [String: Any]
    var id: String { raw["id"] as? String ?? "" }
    var name: String { raw["name"] as? String ?? "" }
    var projectID: String { raw["project_id"] as? String ?? "" }
    var kind: String { raw["kind"] as? String ?? "" }
    var groupID: String? { raw["group_id"] as? String }
    var state: String { raw["state"] as? String ?? "unknown" }
    var health: String { raw["health"] as? String ?? "not_configured" }
    var problem: String? { raw["problem"] as? String }
    var canStop: Bool { raw["can_stop"] as? Bool ?? false }
    var canStart: Bool { raw["can_start"] as? Bool ?? false }
    var canRestart: Bool { raw["can_restart"] as? Bool ?? false }
    var canForce: Bool { raw["can_force"] as? Bool ?? false }
    var observed: Bool { spec["management"] as? String == "observed" }
    var capabilityReason: String? { raw["capability_reason"] as? String }
    var managementSummary: String {
        if !observed { return "已接管 · 支持启停和重启" }
        if canRestart || canStart { return "已关联 · 支持启停和重启" }
        return canStop ? "已关联 · 可停止，待补充启动配置" : "自动观察 · 暂不可控制"
    }
    var running: Bool { ["running", "stop_failed", "stopping"].contains(state) }
    var keep: Bool { (raw["keep"] as? NSNumber)?.boolValue ?? false }
    var spec: [String: Any] { raw["spec"] as? [String: Any] ?? [:] }
    var run: [String: Any] { raw["run"] as? [String: Any] ?? [:] }
    var runtime: [String: Any] { run["runtime"] as? [String: Any] ?? [:] }
    var runtimeIdentity: String {
        if let ids = runtime["container_ids"] as? [String], !ids.isEmpty { return ids.joined(separator:"\n") }
        if let pid = runtime["child_pid"] as? Int { return "PID \(pid) · 监督 PID \(runtime["supervisor_pid"] as? Int ?? 0)" }
        if let pid = runtime["pid"] as? Int { return "PID \(pid) · 已关联的原运行" }
        return "尚无已确认的运行对象"
    }
    var portSummary: String {
        if let ports = runtime["ports"] as? [[String: Any]] {
            let bindings = ports.flatMap { mapping -> [String] in
                mapping.keys.sorted().flatMap { port -> [String] in
                    guard let published = mapping[port] as? [[String:String]] else { return [port + "（未发布到本机）"] }
                    return published.map { ($0["HostIp"] ?? "") + ":" + ($0["HostPort"] ?? "") + " → " + port }
                }
            }
            return bindings.isEmpty ? "未发布端口" : bindings.joined(separator:"\n")
        }
        let ports = spec["ports"] as? [Int] ?? []
        return ports.isEmpty ? "未配置" : ports.map(String.init).joined(separator:", ") + (observed ? "（实际监听）" : "（启动配置）")
    }
    var url: String? { spec["url"] as? String }
    var icon: String { ["process": "terminal", "task": "checklist", "container": "shippingbox", "compose": "square.stack.3d.up"][kind] ?? "terminal" }
    var typeName: String { ["process": "本机服务", "task": "后台任务", "container": "Docker 容器", "compose": "Compose 成员"][kind] ?? kind }
}
