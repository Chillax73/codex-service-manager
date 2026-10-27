import AppKit
import SwiftUI
import Foundation
import Darwin

final class Store: ObservableObject {
    @Published var projects: [ProjectItem] = []
    @Published var deletedProjects: [DeletedProjectItem] = []
    @Published var recentlyDeletedProject: String?
    @Published var services: [ServiceItem] = []
    @Published var operations: [[String: Any]] = []
    @Published var selectedProject = "all"
    @Published var selectedService: String? = nil
    @Published var search = ""
    @Published var filter = "all"
    @Published var busy = false
    @Published var refreshing = false
    @Published var error: String? = nil
    @Published var notice = ""
    @Published var logs = ""
    @Published var logRunID: String? = nil
    @Published var history: [[String: Any]] = []
    @Published var watcher: [String: Any] = [:]
    @Published var capture: [String: Any] = [:]
    @Published var captureDiagnostic: [String: Any] = [:]
    @Published var discovery: [String: Any] = [:]
    var timer: Timer?
    var onRefresh: (() -> Void)?
    var revision = 0
    var runningCount: Int { services.filter { $0.running }.count }
    var filtered: [ServiceItem] {
        services.filter { item in
            (selectedProject == "all" || item.projectID == selectedProject) &&
            (search.isEmpty || item.name.localizedCaseInsensitiveContains(search) || item.typeName.localizedCaseInsensitiveContains(search)) &&
            (filter == "all" || (filter == "attention" ? needsAttention(item) : item.state == filter))
        }
    }
    var displayedRunningCount: Int { filtered.filter { $0.running }.count }
    func projectStatus(_ id: String) -> ProjectStatus { ProjectStatus(services.filter { $0.projectID == id }) }
    var selected: ServiceItem? { services.first { $0.id == selectedService } }
    var currentProject: ProjectItem? { projects.first { $0.id == selectedProject } }
    func start() {
        ensureWatcher()
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in self?.refresh() }
    }
    func ensureWatcher() {
        let process = Process()
        process.executableURL = Bundle.main.executableURL
        process.arguments = ["--watch", "--root", statePath, "--engine", enginePath]
        process.standardInput = FileHandle.nullDevice; process.standardOutput = FileHandle.nullDevice; process.standardError = FileHandle.nullDevice
        try? process.run()
    }
    func refresh() {
        guard !refreshing else { return }
        refreshing = true
        let requestRevision = revision
        DispatchQueue.global(qos: .utility).async {
            do {
                let data = try rpc(["action": "snapshot"]) as? [String: Any] ?? [:]
                DispatchQueue.main.async {
                    guard requestRevision == self.revision else { self.refreshing = false; self.refresh(); return }
                    let previousRun = self.selected?.run
                    let projects = (data["projects"] as? [[String: Any]] ?? []).map(ProjectItem.init)
                    if projects != self.projects { self.projects = projects }
                    self.deletedProjects = (data["deleted_projects"] as? [[String: Any]] ?? []).map(DeletedProjectItem.init)
                    if let id = self.recentlyDeletedProject, !self.deletedProjects.contains(where: { $0.id == id }) { self.recentlyDeletedProject = nil }
                    self.services = (data["services"] as? [[String: Any]] ?? []).map { ServiceItem(raw: $0) }
                    self.operations = data["operations"] as? [[String: Any]] ?? []
                    self.watcher = data["watcher"] as? [String: Any] ?? [:]
                    self.capture = data["capture"] as? [String: Any] ?? [:]
                    self.captureDiagnostic = data["capture_diagnostic"] as? [String: Any] ?? [:]
                    if self.selectedProject != "all", !self.projects.contains(where: { $0.id == self.selectedProject }) { self.selectedProject = "all" }
                    if let id = self.selectedService, !self.services.contains(where: { $0.id == id }) {
                        self.selectedService = nil; self.logs = ""; self.history = []; self.logRunID = nil
                    }
                    self.refreshing = false; self.onRefresh?()
                    if let currentRun = self.selected?.run,
                       (previousRun?["id"] as? String != currentRun["id"] as? String ||
                        previousRun?["state"] as? String != currentRun["state"] as? String ||
                        previousRun?["ended"] as? Double != currentRun["ended"] as? Double) {
                        self.loadDetails()
                    }
                }
            } catch { DispatchQueue.main.async { self.error = error.localizedDescription; self.refreshing = false } }
        }
    }
    func perform(_ request: [String: Any], failure: ((String) -> Void)? = nil, completion: ((Any) -> Void)? = nil) {
        guard !busy else { return }
        busy = true; notice = "正在执行…"
        DispatchQueue.global(qos: .userInitiated).async {
            do {
                let result = try rpc(request)
                DispatchQueue.main.async {
                    self.busy = false; self.revision += 1
                    if let results = (result as? [[String: Any]]) ?? ((result as? [String: Any])?["results"] as? [[String: Any]]), results.contains(where: { $0["result"] as? String == "failed" }) {
                        self.notice = "部分操作失败，请查看操作记录"
                        self.error = results.filter { $0["result"] as? String == "failed" }.map { $0["error"] as? String ?? "失败" }.joined(separator: "\n")
                    } else { self.notice = "操作已完成" }
                    completion?(result); self.refresh()
                    if self.selected != nil { self.loadDetails() }
                }
            } catch {
                DispatchQueue.main.async { self.busy = false; self.notice = "操作失败"; if let failure = failure { failure(error.localizedDescription) } else { self.error = error.localizedDescription }; self.refresh() }
            }
        }
    }
    func restoreProject(_ id: String) {
        perform(["action":"project_restore","id":id],completion:{ result in
            if let data = result as? [String:Any], let project = data["project"] as? [String:Any], let projectID = project["id"] as? String {
                self.selectedProject = projectID; self.recentlyDeletedProject = nil; self.notice = "项目登记和历史已恢复，未启动服务"
            }
        })
    }
    func action(_ action: String, _ item: ServiceItem) { perform(["action": action, "id": item.id]) }
    func loadDetails() {
        guard let id = selectedService else { return }
        DispatchQueue.global(qos: .utility).async {
            let history = (try? rpc(["action":"history", "id":id])) as? [[String: Any]] ?? []
            let logs: String
            do { logs = try rpc(["action":"logs", "id":id]) as? String ?? "" }
            catch { logs = error.localizedDescription }
            DispatchQueue.main.async {
                if self.selectedService == id { self.history = history; if self.logRunID == nil { self.logs = logs } }
            }
        }
    }
    func loadRunLogs(_ runID: String) {
        guard let id = selectedService else { return }
        logRunID = runID
        logs = "正在读取日志…"
        DispatchQueue.global(qos: .utility).async {
            let output: String
            do { output = try rpc(["action":"logs", "id":id, "run_id":runID]) as? String ?? "" }
            catch { output = error.localizedDescription }
            DispatchQueue.main.async {
                if self.selectedService == id && self.logRunID == runID { self.logs = output }
            }
        }
    }
}
