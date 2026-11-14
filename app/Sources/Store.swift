import AppKit
import SwiftUI
import Foundation
import Darwin

final class Store: ObservableObject {
    @Published var projects: [ProjectItem] = []
    @Published private(set) var projectSortKey = "name"
    @Published private(set) var projectSortDirection = "asc"
    @Published var deletedProjects: [DeletedProjectItem] = []
    @Published var recentlyDeletedProject: String?
    @Published var services: [ServiceItem] = [] { didSet { rebuildServices() } }
    @Published var operations: [[String: Any]] = []
    @Published var selectedProject = "all" { didSet { rebuildFilter() } }
    @Published var selectedService: String? = nil
    @Published var search = "" { didSet { rebuildFilter() } }
    @Published var filter: ServiceListStatus? { didSet { rebuildFilter() } }
    @Published var busy = false
    var refreshing = false { didSet { if refreshState.loading != refreshing { refreshState.loading = refreshing } } }
    let refreshState = RefreshState()
    let checkTimes = CheckTimes()
    @Published private(set) var observationUnavailable = false
    @Published var error: String? = nil
    @Published var notice = ""
    @Published var logs = ""
    @Published var logRunID: String? = nil
    @Published var history: [[String: Any]] = []
    @Published var watcher: [String: Any] = [:]
    @Published var lastCodexExit: [String: Any] = [:]
    @Published var capture: [String: Any] = [:]
    @Published var captureDiagnostic: [String: Any] = [:]
    @Published var discovery: [String: Any] = [:]
    var timer: Timer?
    var onRefresh: (() -> Void)?
    var revision = 0
    private var queuedRefresh: String?
    private var queuedPriority = false
    private var refreshGeneration = 0
    private var manualRefreshInFlight = false
    private var lastRefreshStarted = -Double.infinity
    private weak var window: NSWindow?
    private let snapshotConnection = CachedSnapshotConnection()
    private var windowVisible: Bool {
        window?.isVisible == true && window?.isMiniaturized == false && !NSApp.isHidden
    }
    private var servicesByID: [String: ServiceItem] = [:]
    private var projectStatuses: [String: ProjectStatus] = [:]
    private var cachedStates = false
    private(set) var hasPausedServices = false
    private(set) var runningCount = 0
    private(set) var filtered: [ServiceItem] = []
    private(set) var displayedRunningCount = 0
    var hasCachedStates: Bool {
        observationUnavailable || cachedStates
    }
    private func rebuildServices() {
        servicesByID = Dictionary(services.map { ($0.id, $0) }, uniquingKeysWith: { _, new in new })
        projectStatuses = Dictionary(grouping: services, by: \.projectID).mapValues(ProjectStatus.init)
        hasPausedServices = services.contains { $0.observationPaused }
        cachedStates = services.contains { $0.cacheState != "recent" }
        runningCount = services.filter { $0.running }.count
        rebuildFilter()
    }
    private func rebuildFilter() {
        filtered = services.filter { item in
            (selectedProject == "all" || item.projectID == selectedProject) &&
            (search.isEmpty || item.name.localizedCaseInsensitiveContains(search) || item.typeName.localizedCaseInsensitiveContains(search)) &&
            (filter?.matches(item) ?? true)
        }
        displayedRunningCount = filtered.filter { $0.running }.count
    }
    func projectStatus(_ id: String) -> ProjectStatus { projectStatuses[id] ?? ProjectStatus([]) }
    var selected: ServiceItem? { selectedService.flatMap { servicesByID[$0] } }
    var currentProject: ProjectItem? { projects.first { $0.id == selectedProject } }
    func start(window: NSWindow) {
        self.window = window
        ensureWatcher()
        timer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in
            guard let self = self else { return }
            if self.windowVisible || ProcessInfo.processInfo.systemUptime - self.lastRefreshStarted >= 10 {
                self.refresh()
            }
        }
        timer?.tolerance = 0.2
    }
    func stop() {
        timer?.invalidate()
        snapshotConnection.close()
    }

    func cacheCaption(_ item: ServiceItem) -> String? {
        checkTimes.caption(item, unavailable: observationUnavailable)
    }
    func ensureWatcher() {
        let process = Process()
        process.executableURL = Bundle.main.executableURL
        process.arguments = ["--watch", "--root", statePath, "--engine", enginePath]
        process.standardInput = FileHandle.nullDevice; process.standardOutput = FileHandle.nullDevice; process.standardError = FileHandle.nullDevice
        try? process.run()
    }
    private func finishRefresh() {
        refreshing = false
        flushQueuedRefresh()
    }
    private func flushQueuedRefresh() {
        guard !refreshing, !busy else { return }
        if let scope = queuedRefresh {
            queuedRefresh = nil
            refresh(projectID: scope)
        } else if queuedPriority {
            queuedPriority = false
            refresh(prioritize: true)
        }
    }
    func refresh(projectID: String? = nil, prioritize: Bool = false) {
        let isManual = projectID != nil
        guard !busy, !refreshing || (isManual && !manualRefreshInFlight) else {
            if let projectID = projectID { queuedRefresh = projectID }
            if prioritize { queuedPriority = true }
            return
        }
        refreshGeneration += 1
        let generation = refreshGeneration
        refreshing = true
        manualRefreshInFlight = isManual
        lastRefreshStarted = ProcessInfo.processInfo.systemUptime
        let requestRevision = revision
        let visible = windowVisible
        let priorityProject = prioritize ? selectedProject : nil
        DispatchQueue.global(qos: .utility).async {
            do {
                let data: [String: Any]
                if let projectID = projectID {
                    data = try rpc(["action": "refresh", "project_id": projectID]) as? [String: Any] ?? [:]
                } else {
                    data = try self.snapshotConnection.read(visible: visible, priorityProject: priorityProject)
                }
                DispatchQueue.main.async {
                    guard generation == self.refreshGeneration else { return }
                    guard requestRevision == self.revision else { self.finishRefresh(); return }
                    let runChanged = self.applySnapshot(data, cached: projectID == nil)
                    if runChanged { self.loadDetails() }
                    self.finishRefresh()
                }
            } catch {
                DispatchQueue.main.async {
                    guard generation == self.refreshGeneration else { return }
                    if projectID == nil { if !self.observationUnavailable { self.observationUnavailable = true } }
                    else { self.error = error.localizedDescription }
                    self.finishRefresh()
                }
            }
        }
    }
    @discardableResult
    func applySnapshot(_ data: [String: Any], cached: Bool = true) -> Bool {
        if cached {
            let unavailable = data["observation_available"] as? Bool != true
            if unavailable != observationUnavailable { observationUnavailable = unavailable }
        }
        let previousRun = selected?.run
        applyProjectSidebar(data)
        let deleted = (data["deleted_projects"] as? [[String: Any]] ?? []).map(DeletedProjectItem.init)
        if deleted != deletedProjects { deletedProjects = deleted }
        if let id = recentlyDeletedProject, !deletedProjects.contains(where: { $0.id == id }) { recentlyDeletedProject = nil }
        let rows = data["services"] as? [[String: Any]] ?? []
        checkTimes.updateServices(rows)
        let updated = rows.map { ServiceItem(raw: $0) }
        if updated != services { services = updated }
        let updatedOperations = data["operations"] as? [[String: Any]] ?? []
        if !NSArray(array: operations).isEqual(to: updatedOperations) { operations = updatedOperations }
        var updatedWatcher = data["watcher"] as? [String: Any] ?? [:]
        updatedWatcher.removeValue(forKey: "updated")
        if !NSDictionary(dictionary: watcher).isEqual(to: updatedWatcher) { watcher = updatedWatcher }
        let exit = data["last_codex_exit"] as? [String: Any] ?? [:]
        if !NSDictionary(dictionary: lastCodexExit).isEqual(to: exit) { lastCodexExit = exit }
        var updatedCapture = data["capture"] as? [String: Any] ?? [:]
        updatedCapture.removeValue(forKey: "updated")
        if !NSDictionary(dictionary: capture).isEqual(to: updatedCapture) { capture = updatedCapture }
        let diagnostic = data["capture_diagnostic"] as? [String: Any] ?? [:]
        if !NSDictionary(dictionary: captureDiagnostic).isEqual(to: diagnostic) { captureDiagnostic = diagnostic }
        if selectedProject != "all", !projects.contains(where: { $0.id == selectedProject }) { selectedProject = "all" }
        if let id = selectedService, servicesByID[id] == nil {
            selectedService = nil; logs = ""; history = []; logRunID = nil
        }
        onRefresh?()
        guard let currentRun = selected?.run else { return false }
        return previousRun?["id"] as? String != currentRun["id"] as? String ||
            previousRun?["state"] as? String != currentRun["state"] as? String ||
            previousRun?["ended"] as? Double != currentRun["ended"] as? Double
    }

    func perform(_ request: [String: Any], refreshAfter: Bool = true, failure: ((String) -> Void)? = nil, completion: ((Any) -> Void)? = nil) {
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
                    completion?(result)
                    self.flushQueuedRefresh()
                    if refreshAfter && !self.refreshing {
                        self.refresh()
                    }
                    if refreshAfter, self.selected != nil { self.loadDetails() }
                }
            } catch {
                DispatchQueue.main.async {
                    self.busy = false
                    self.notice = "操作失败"
                    if let failure = failure { failure(error.localizedDescription) }
                    else { self.error = error.localizedDescription }
                    self.flushQueuedRefresh()
                    if refreshAfter && !self.refreshing { self.refresh() }
                }
            }
        }
    }
    private func applyProjectSidebar(_ data: [String: Any]) {
        checkTimes.updateProjects(data["projects"] as? [[String: Any]] ?? [])
        let updated = (data["projects"] as? [[String: Any]] ?? []).map(ProjectItem.init)
        if updated != projects { projects = updated }
        if let preferences = data["project_sort_preferences"] as? [String: String] {
            let key = preferences["sort_key"] ?? "name"
            let direction = preferences["direction"] ?? "asc"
            if key != projectSortKey { projectSortKey = key }
            if direction != projectSortDirection { projectSortDirection = direction }
        }
    }
    func updateProjectSidebar(_ request: [String: Any]) {
        perform(request, refreshAfter: false, completion: { result in
            if let data = result as? [String: Any] { self.applyProjectSidebar(data) }
        })
    }
    func togglePin(_ project: ProjectItem) {
        updateProjectSidebar(["action": "project_pin", "id": project.id, "pinned": !project.pinned])
    }
    func toggleScan(_ project: ProjectItem) {
        perform(["action": "project_scan_policy", "id": project.id, "enabled": !project.autoScan])
    }
    func updateService(_ result: Any) {
        if let data = result as? [String: Any], let id = data["id"] as? String,
           let index = services.firstIndex(where: { $0.id == id }) {
            services[index] = ServiceItem(raw: data)
        }
    }
    func removeService(_ id: String) {
        services.removeAll { $0.id == id }
        operations.removeAll { $0["target"] as? String == id }
        if selectedService == id {
            selectedService = nil
            logs = ""
            history = []
            logRunID = nil
        }
        onRefresh?()
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
