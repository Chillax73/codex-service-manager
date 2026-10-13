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

struct ProjectItem: Identifiable, Equatable {
    let id: String, name: String, cwd: String
    let manual: Bool, codexExit: Bool
    init(_ data: [String: Any]) {
        id = data["id"] as? String ?? ""; name = data["name"] as? String ?? ""
        cwd = data["cwd"] as? String ?? ""
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
func stateLabel(_ value: String) -> String {
    ["starting":"启动中", "running":"运行中", "stopping":"停止中", "stopped":"已停止", "failed":"失败",
     "unknown":"未知", "stop_failed":"停止失败", "exited":"已退出", "succeeded":"成功", "cancelled":"已取消",
     "never_started":"未运行", "available":"可用", "unavailable":"不可用", "not_configured":"未配置检查", "checking":"检查中"][value] ?? value
}
struct StatusAppearance {
    let color: NSColor
    let symbol: String

    init(_ state: String) {
        switch state {
        case "running": color = .systemGreen; symbol = "play.circle.fill"
        case "succeeded", "available": color = .systemGreen; symbol = "checkmark.circle.fill"
        case "stopped", "exited": color = .systemRed; symbol = "stop.circle.fill"
        case "failed", "stop_failed", "unavailable": color = .systemRed; symbol = "exclamationmark.circle.fill"
        case "starting", "stopping", "checking": color = .systemOrange; symbol = "clock.fill"
        case "unknown": color = .systemOrange; symbol = "questionmark.circle.fill"
        case "attention": color = .systemOrange; symbol = "exclamationmark.triangle.fill"
        case "cancelled": color = .secondaryLabelColor; symbol = "xmark.circle.fill"
        case "never_started", "not_configured": color = .secondaryLabelColor; symbol = "minus.circle.fill"
        default: color = .systemOrange; symbol = "questionmark.circle.fill"
        }
    }

    var menuImage: NSImage? {
        guard let source = NSImage(systemSymbolName:symbol, accessibilityDescription:nil) else { return nil }
        let image = NSImage(size:NSSize(width:14,height:14), flipped:false) { rect in
            source.draw(in:rect)
            color.setFill()
            rect.fill(using:.sourceIn)
            return true
        }
        image.isTemplate = false
        return image
    }
}

struct StatusBadge: View {
    let state: String
    var prefix = ""
    var title: String? = nil

    var body: some View {
        let appearance = StatusAppearance(state)
        let color = Color(nsColor:appearance.color)
        HStack(spacing:5) {
            Image(systemName:appearance.symbol).foregroundStyle(color)
            Text(prefix + (title ?? stateLabel(state))).foregroundStyle(Color(nsColor:.labelColor))
        }
        .font(.caption.weight(.semibold))
        .padding(.horizontal,8).padding(.vertical,4)
        .background(color.opacity(0.12),in:Capsule())
        .background(Color(nsColor:.controlBackgroundColor),in:Capsule())
        .overlay(Capsule().strokeBorder(color.opacity(0.5),lineWidth:1))
        .fixedSize()
        .accessibilityElement(children:.ignore)
        .accessibilityLabel(prefix + (title ?? stateLabel(state)))
    }
}
let serviceStates = ["running", "exited", "stopped", "failed", "stop_failed", "succeeded", "unknown",
                     "starting", "stopping", "cancelled", "never_started"]
func needsAttention(_ item: ServiceItem) -> Bool {
    item.problem != nil || item.health == "unavailable" || ["unknown", "failed", "stop_failed"].contains(item.state)
}
struct ProjectStatus: Equatable {
    let total: Int, running: Int, attention: Int
    let state: String, label: String

    init(_ services: [ServiceItem]) {
        total = services.count
        running = services.filter { $0.state == "running" }.count
        attention = services.filter(needsAttention).count
        if services.isEmpty { state = "never_started"; label = "无服务" }
        else if running > 0 { state = "running"; label = "运行中 \(running)" }
        else if let active = ["starting", "stopping", "stop_failed", "unknown", "failed"].first(where:{ value in services.contains { $0.state == value } }) {
            state = active; label = stateLabel(active)
        } else if Set(services.map(\.state)).count == 1 {
            state = services[0].state; label = stateLabel(state)
        } else { state = "stopped"; label = "无运行" }
    }
}
func dateLabel(_ number: Any?) -> String {
    guard let number = number as? Double else { return "—" }
    return Date(timeIntervalSince1970: number).formatted(date: .abbreviated, time: .standard)
}

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

struct ProjectRow: View, Equatable {
    let project: ProjectItem
    let status: ProjectStatus
    let busy: Bool
    let onRename: () -> Void
    let onDelete: () -> Void
    static func == (lhs: ProjectRow, rhs: ProjectRow) -> Bool { lhs.project == rhs.project && lhs.status == rhs.status && lhs.busy == rhs.busy }
    var body: some View {
        VStack(alignment:.leading,spacing:4) {
            Text(project.name).font(.headline)
            Text(project.cwd).font(.caption).foregroundStyle(.secondary).lineLimit(1).truncationMode(.middle)
            HStack(spacing:6) {
                StatusBadge(state:status.state,title:status.label)
                Text("\(status.total) 个服务").font(.caption).foregroundStyle(.secondary)
            }
            if status.attention > 0 { StatusBadge(state:"attention",title:"需关注 \(status.attention)") }
        }.padding(.vertical,4).contextMenu {
            Button(action:onRename) { Label("重命名项目…",systemImage:"pencil") }.disabled(busy)
            Button(role:.destructive,action:onDelete) { Label("删除项目…",systemImage:"trash") }.disabled(busy)
        }
    }
}

struct MainView: View {
    @ObservedObject var store: Store
    @State private var adding = false
    @State private var discovering = false
    @State private var settings = false
    @State private var tab = "details"
    @State private var forceItem: ServiceItem?
    @State private var configuringService: ServiceItem?
    @State private var renamingProject: ProjectItem?
    @State private var deletingProject: ProjectItem?
    var body: some View {
        NavigationSplitView {
            List(selection: $store.selectedProject) {
                Label("全部项目", systemImage:"square.grid.2x2").tag("all")
                Section("项目") {
                    ForEach(store.projects) { project in
                        ProjectRow(project:project,status:store.projectStatus(project.id),busy:store.busy,
                            onRename:{ renamingProject = project },onDelete:{ deletingProject = project })
                            .equatable().tag(project.id)
                    }
                }
            }.navigationTitle("项目").navigationSplitViewColumnWidth(min:210, ideal:240)
        } content: {
            VStack(spacing:0) {
                HStack {
                    VStack(alignment:.leading, spacing:4) {
                        Text(store.currentProject?.name ?? "全部服务").font(.title2.weight(.semibold))
                        Text("\(store.filtered.count) 个服务 · \(store.displayedRunningCount) 个运行中").font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button { adding = true } label: { Image(systemName:"plus") }.help("添加服务")
                    Menu {
                        Button("发现本机服务") { store.perform(["action":"discover"],completion:{ store.discovery = $0 as? [String:Any] ?? [:]; discovering = true }) }
                        Button("刷新") { store.refresh() }
                        Button("管理设置") { settings = true }
                        if let project = store.currentProject {
                            Divider()
                            projectActions(project)
                            Divider()
                            Button("结束工作") { store.perform(["action":"end_project", "project_id":project.id]) }
                            Button("停止项目全部服务") { store.perform(["action":"batch", "project_id":project.id, "operation":"stop"]) }
                            Button("重启项目全部服务") { store.perform(["action":"batch", "project_id":project.id, "operation":"restart"]) }
                        }
                    } label: { Image(systemName:"ellipsis.circle") }.menuStyle(.borderlessButton).fixedSize()
                }.padding(20)
                HStack {
                    TextField("搜索服务", text:$store.search).textFieldStyle(.roundedBorder)
                    Picker("筛选", selection:$store.filter) {
                        Text("全部状态").tag("all")
                        ForEach(serviceStates,id:\.self) { state in Text(stateLabel(state)).tag(state) }
                        Text("需关注").tag("attention")
                    }.labelsHidden().frame(width:125).help("按运行状态筛选，与项目和搜索同时生效")
                }.padding(.horizontal,20).padding(.bottom,14)
                Divider()
                if store.filtered.isEmpty {
                    VStack(spacing:12) {
                        Image(systemName:"terminal").font(.system(size:38)).foregroundStyle(.secondary)
                        Text(store.services.isEmpty ? "还没有登记的服务" : "没有符合条件的服务").font(.headline)
                        Text(store.services.isEmpty ? "启用全局自动观察后，任意本机聊天启动的服务会出现在这里。" : "试试其他状态，或清空搜索词。").font(.caption).foregroundStyle(.secondary)
                        Button("添加服务") { adding = true }
                    }.frame(maxWidth:.infinity,maxHeight:.infinity)
                } else {
                    List(selection:$store.selectedService) {
                        ForEach(store.filtered) { item in
                            HStack(alignment:.top,spacing:12) {
                                Image(systemName:item.icon).font(.title3).frame(width:24).foregroundStyle(.secondary)
                                VStack(alignment:.leading,spacing:5) {
                                    HStack { Text(item.name).font(.headline); if item.keep { Image(systemName:"pin.fill").font(.caption).foregroundStyle(.secondary) } }
                                    Text(item.typeName + (item.observed ? " · 自动观察" : "")).font(.caption).foregroundStyle(.secondary)
                                    ViewThatFits(in:.horizontal) {
                                        HStack(spacing:6) {
                                            StatusBadge(state:item.state)
                                            if item.health != "not_configured" { StatusBadge(state:item.health,prefix:"健康 · ") }
                                        }
                                        VStack(alignment:.leading,spacing:6) {
                                            StatusBadge(state:item.state)
                                            if item.health != "not_configured" { StatusBadge(state:item.health,prefix:"健康 · ") }
                                        }
                                    }
                                    if let url = item.url { Text(url).font(.caption.monospaced()).foregroundStyle(.secondary).lineLimit(1) }
                                }
                                Spacer()
                            }.padding(.vertical,9).tag(item.id)
                        }
                    }.listStyle(.inset)
                }
                Divider()
                HStack {
                    if store.busy { ProgressView().controlSize(.small) }
                    Text(store.notice.isEmpty ? (store.refreshing ? "正在更新状态…" : "每 2 秒核对状态") : store.notice)
                    Spacer()
                    if let id = store.recentlyDeletedProject { Button("撤销删除") { store.restoreProject(id) }.disabled(store.busy) }
                    Image(systemName:store.watcher["alive"] as? Bool == true ? "eye" : "eye.slash")
                        .help(store.watcher["alive"] as? Bool == true ? "退出观察器运行中" : "退出观察器不可用")
                }.font(.caption).foregroundStyle(.secondary).padding(12)
            }.navigationSplitViewColumnWidth(min:330,ideal:420)
        } detail: {
            if let item = store.selected {
                VStack(alignment:.leading,spacing:16) {
                    HStack { Image(systemName:item.icon).font(.title2); Text(item.name).font(.title2.weight(.semibold)); Spacer() }
                    HStack {
                        StatusBadge(state:item.state)
                        StatusBadge(state:item.health,prefix:"健康 · ")
                    }.font(.subheadline)
                    HStack {
                        Button(item.kind == "task" ? "重新执行" : (item.canStart ? "启动" : "重启")) { store.action(item.canStart ? "start" : "restart", item) }.disabled(store.busy || (!item.canStart && !item.canRestart))
                        Button(item.kind == "task" ? "取消" : "停止") { store.action("stop", item) }.disabled(store.busy || !item.canStop)
                        if item.state == "stop_failed" { Button("强制停止") { forceItem = item }.disabled(store.busy || !item.canForce) }
                        if item.observed && ["process", "task"].contains(item.kind) {
                            Button("配置启动方式…") { configuringService = item }.disabled(store.busy)
                        }
                        if let url = item.url, let address = URL(string:url) {
                            Button { NSWorkspace.shared.open(address) } label: { Image(systemName:"arrow.up.right.square") }.help("打开地址")
                            Button { NSPasteboard.general.clearContents(); NSPasteboard.general.setString(url,forType:.string) } label: { Image(systemName:"doc.on.doc") }.help("复制地址")
                        }
                    }
                    if let problem = item.problem { Text(problem).font(.caption).foregroundStyle(.orange).textSelection(.enabled) }
                    if let reason = item.capabilityReason { Text(reason).font(.caption).foregroundStyle(.secondary) }
                    if item.observed { Text("原运行不参与项目结束策略。通过管理器再次启动后，将按项目策略管理；此前输出和退出码无法补回。").font(.caption).foregroundStyle(.secondary) }
                    Picker("详情",selection:$tab) { Text("配置").tag("details"); Text("日志").tag("logs"); Text("历史").tag("history"); Text("操作").tag("operations") }.pickerStyle(.segmented).labelsHidden()
                    ScrollView {
                        VStack(alignment:.leading,spacing:14) {
                            if tab == "details" {
                                field("类型",item.typeName)
                                field("管理能力",item.managementSummary)
                                if item.observed { field("归属依据",item.spec["association"] as? String ?? "—") }
                                field("工作目录",item.spec["cwd"] as? String ?? "—")
                                field("运行对象",item.runtimeIdentity)
                                field("端口",item.portSummary)
                                field("启动命令",(item.spec["argv"] as? [String])?.joined(separator:" ") ?? item.spec["container_id"] as? String ?? item.spec["file"] as? String ?? "—")
                                field("开始时间",dateLabel(item.run["started"]))
                                field("运行 ID",item.run["id"] as? String ?? "—")
                                Toggle("项目结束后持续运行",isOn:Binding(get:{ item.keep },set:{ store.perform(["action":"service_keep","id":item.id,"keep":$0]) })).disabled(store.busy || item.observed)
                                if let group = item.groupID {
                                    Divider(); Text("Compose 服务组").font(.headline)
                                    HStack {
                                        Button("停止整组") { store.perform(["action":"batch","group_id":group,"operation":"stop"]) }.disabled(store.busy || !item.canStop)
                                        Button("重启整组") { store.perform(["action":"batch","group_id":group,"operation":"restart"]) }.disabled(store.busy || !item.canRestart)
                                    }
                                }
                                if let project = store.projects.first(where:{$0.id == item.projectID}) {
                                    Divider(); Text("项目结束策略 · " + project.name).font(.headline)
                                    Toggle("手动结束工作时停止",isOn:Binding(get:{project.manual},set:{store.perform(["action":"project_policy","id":project.id,"manual":$0,"codex_exit":project.codexExit])})).disabled(store.busy)
                                    Toggle("退出 Codex 时停止",isOn:Binding(get:{project.codexExit},set:{store.perform(["action":"project_policy","id":project.id,"manual":project.manual,"codex_exit":$0])})).disabled(store.busy)
                                    Text("仅应用进程终止触发；关闭窗口、聊天结束不会触发。强制退出同样产生系统终止事件。").font(.caption).foregroundStyle(.secondary)
                                }
                            } else if tab == "logs" {
                                HStack {
                                    Text(store.logRunID.map { "运行 " + String($0.prefix(8)) } ?? ((item.kind == "container" || item.kind == "compose") ? "容器最近日志" : "当前运行日志")).font(.caption).foregroundStyle(.secondary)
                                    Spacer()
                                    Button("刷新日志") { if let runID = store.logRunID { store.loadRunLogs(runID) } else { store.loadDetails() } }
                                    if store.logRunID != nil { Button("当前日志") { store.logRunID = nil; store.loadDetails() } }
                                }
                                Text(store.logs.isEmpty ? (item.observed && item.kind != "container" ? "本机自动观察未接管原进程的输出，暂无采集日志。" : "尚无日志输出") : store.logs).font(.system(.caption,design:.monospaced)).textSelection(.enabled).frame(maxWidth:.infinity,alignment:.leading)
                            } else if tab == "history" {
                                Button("刷新历史") { store.loadDetails() }
                                ForEach(Array(store.history.enumerated()),id:\.offset) { _, run in
                                    VStack(alignment:.leading,spacing:5) {
                                        StatusBadge(state:run["state"] as? String ?? "unknown")
                                        Text(dateLabel(run["started"]) + " → " + dateLabel(run["ended"])).font(.caption).foregroundStyle(.secondary)
                                        if let error = run["error"] as? String { Text(error).font(.caption).foregroundStyle(.orange) }
                                        if let code = run["exit_code"] as? Int { Text("退出码 \(code)").font(.caption).foregroundStyle(.secondary) }
                                        Text(run["id"] as? String ?? "").font(.caption.monospaced()).foregroundStyle(.secondary)
                                        if (item.kind == "process" || item.kind == "task"), let runID = run["id"] as? String {
                                            Button("查看本次日志") { store.loadRunLogs(runID); tab = "logs" }
                                        }
                                    }; Divider()
                                }
                            } else {
                                ForEach(Array(store.operations.filter {$0["target"] as? String == item.id}.enumerated()),id:\.offset) { _, op in
                                    VStack(alignment:.leading,spacing:5) {
                                        Text((op["action"] as? String ?? "") + " · " + stateLabel(op["result"] as? String ?? "执行中")).font(.headline)
                                        Text(dateLabel(op["started"])).font(.caption).foregroundStyle(.secondary)
                                        if let error = op["error"] as? String { Text(error).font(.caption).foregroundStyle(.orange) }
                                    }; Divider()
                                }
                            }
                        }.frame(maxWidth:.infinity,alignment:.leading)
                    }
                }.padding(24).frame(minWidth:320)
            } else {
                VStack(spacing:10) { Image(systemName:"sidebar.right").font(.largeTitle).foregroundStyle(.secondary); Text("选择服务查看详情").foregroundStyle(.secondary) }.frame(maxWidth:.infinity,maxHeight:.infinity)
            }
        }
        .frame(minWidth:1000,minHeight:620)
        .onChange(of:store.selectedService) { _ in store.logRunID = nil; store.logs = ""; store.history = []; store.loadDetails() }
        .onChange(of:store.selectedProject) { scope in
            if scope != "all", store.selected?.projectID != scope { store.selectedService = nil }
        }
        .sheet(item:$renamingProject) { RenameProjectView(project:$0,store:store) }
        .sheet(item:$deletingProject) { DeleteProjectView(project:$0,store:store) }
        .sheet(item:$configuringService) { ObservedConfigView(item:$0,store:store) }
        .sheet(isPresented:$adding) { AddView(store:store) }
        .sheet(isPresented:$discovering) { DiscoveryView(store:store) }
        .sheet(isPresented:$settings) { SettingsView(store:store) }
        .alert("操作未完成",isPresented:Binding(get:{store.error != nil},set:{if !$0 {store.error = nil}})) { Button("知道了",role:.cancel) {store.error = nil} } message: {Text(store.error ?? "")}
        .confirmationDialog("强制停止会立即终止已确认属于该服务的进程",isPresented:Binding(get:{forceItem != nil},set:{if !$0 {forceItem = nil}}),titleVisibility:.visible) {
            Button("强制停止",role:.destructive) { if let item = forceItem { store.action("force",item) }; forceItem = nil }
            Button("取消",role:.cancel) {forceItem = nil}
        }
    }
    @ViewBuilder func projectActions(_ project: ProjectItem) -> some View {
        Button { renamingProject = project } label: { Label("重命名项目…", systemImage:"pencil") }.disabled(store.busy)
        Button(role:.destructive) { deletingProject = project } label: { Label("删除项目…", systemImage:"trash") }.disabled(store.busy)
    }
    func field(_ label:String,_ value:String) -> some View {
        VStack(alignment:.leading,spacing:5) { Text(label).font(.caption).foregroundStyle(.secondary); Text(value).font(.system(.body,design:.monospaced)).textSelection(.enabled) }
    }
}

struct RenameProjectView: View {
    let project: ProjectItem
    @ObservedObject var store: Store
    @Environment(\.dismiss) var dismiss
    @State private var name: String
    @State private var failure = ""
    init(project: ProjectItem, store: Store) {
        self.project = project; self.store = store; _name = State(initialValue:project.name)
    }
    var cleanName: String { name.trimmingCharacters(in:.whitespacesAndNewlines) }
    var validName: Bool {
        !cleanName.isEmpty && cleanName.unicodeScalars.count <= 100 &&
        !cleanName.unicodeScalars.contains { CharacterSet.controlCharacters.union(.newlines).contains($0) }
    }
    var body: some View {
        VStack(alignment:.leading,spacing:16) {
            Text("重命名项目").font(.title2.weight(.semibold))
            Text(project.cwd).font(.caption.monospaced()).foregroundStyle(.secondary).textSelection(.enabled)
            TextField("项目名称",text:$name).textFieldStyle(.roundedBorder).accessibilityIdentifier("project-name-input").onSubmit(save)
            Text("名称为 1–100 个字符。只修改显示名称，目录、服务归属和结束策略保持不变。").font(.caption).foregroundStyle(.secondary)
            if !failure.isEmpty { Text(failure).font(.caption).foregroundStyle(.red) }
            HStack {
                Spacer()
                Button("取消") { dismiss() }.keyboardShortcut(.cancelAction).disabled(store.busy)
                Button("保存名称",action:save).keyboardShortcut(.defaultAction).buttonStyle(.borderedProminent).disabled(!validName || store.busy)
            }
        }.padding(24).frame(width:500)
    }
    func save() {
        guard validName, !store.busy else { return }
        failure = ""
        store.perform(["action":"project_rename","id":project.id,"name":cleanName],failure:{ failure = $0 },completion:{ _ in dismiss() })
    }
}

struct DeleteProjectView: View {
    let project: ProjectItem
    @ObservedObject var store: Store
    @Environment(\.dismiss) var dismiss
    @State private var preview: [String: Any]?
    @State private var loading = true
    @State private var failure = ""
    var blockers: [[String: Any]] { preview?["blockers"] as? [[String: Any]] ?? [] }
    var body: some View {
        VStack(alignment:.leading,spacing:16) {
            Text("删除项目登记").font(.title2.weight(.semibold))
            Text(project.name).font(.headline)
            Text(project.cwd).font(.caption.monospaced()).foregroundStyle(.secondary).textSelection(.enabled)
            Text("项目、服务登记及运行历史将移出当前列表，之后可撤销或在管理设置中恢复。源码目录、容器、数据卷和本机日志文件保留。").font(.callout)
            if loading { ProgressView("正在核对服务状态…") }
            if let data = preview {
                Text("将移除 \(data["service_count"] as? Int ?? 0) 个服务登记、\(data["run_count"] as? Int ?? 0) 条运行历史和 \(data["operation_count"] as? Int ?? 0) 条操作记录。").font(.caption).foregroundStyle(.secondary)
                if !blockers.isEmpty {
                    Text("有服务尚未安全结束，当前不能删除").font(.headline).foregroundStyle(.orange)
                    ScrollView {
                        VStack(alignment:.leading,spacing:10) {
                            ForEach(Array(blockers.enumerated()),id:\.offset) { _, item in
                                VStack(alignment:.leading,spacing:3) {
                                    Text(item["name"] as? String ?? "服务").font(.subheadline.weight(.semibold))
                                    Text(item["reason"] as? String ?? stateLabel(item["state"] as? String ?? "unknown")).font(.caption).foregroundStyle(.secondary)
                                }
                            }
                        }.frame(maxWidth:.infinity,alignment:.leading)
                    }.frame(maxHeight:180)
                    Text("请先手动停止运行中的服务，或处理身份未知/停止失败的问题，再重新检查。").font(.caption).foregroundStyle(.secondary)
                }
            }
            if !failure.isEmpty { Text(failure).font(.caption).foregroundStyle(.red) }
            HStack {
                Button("重新检查",action:reload).disabled(loading || store.busy)
                Spacer()
                Button("取消") { dismiss() }.keyboardShortcut(.cancelAction).disabled(store.busy)
                Button("删除项目",role:.destructive,action:remove).buttonStyle(.borderedProminent)
                    .disabled(loading || store.busy || preview?["can_delete"] as? Bool != true)
            }
        }.padding(24).frame(width:590).onAppear(perform:reload)
    }
    func reload() {
        loading = true; failure = ""; preview = nil
        DispatchQueue.global(qos:.utility).async {
            do {
                let data = try rpc(["action":"project_delete_preview","id":project.id]) as? [String: Any]
                DispatchQueue.main.async { preview = data; loading = false }
            } catch { DispatchQueue.main.async { failure = error.localizedDescription; loading = false } }
        }
    }
    func remove() {
        guard preview?["can_delete"] as? Bool == true, !store.busy else { return }
        store.perform(["action":"project_delete","id":project.id],failure:{ failure = $0; preview = nil },completion:{ result in
            if let data = result as? [String:Any] { store.recentlyDeletedProject = data["trash_id"] as? String; store.notice = "项目已移除，可撤销删除或稍后恢复" }
            if store.selectedProject == project.id { store.selectedProject = "all" }
            if store.selected?.projectID == project.id {
                store.selectedService = nil; store.logs = ""; store.history = []; store.logRunID = nil
            }
            dismiss()
        })
    }
}

struct AddView: View {
    @ObservedObject var store:Store
    @Environment(\.dismiss) var dismiss
    @State var kind = "process"
    @State var name = ""
    @State var cwd = NSHomeDirectory()
    @State var command = ""
    @State var url = ""
    @State var port = ""
    @State var container = ""
    @State var file = ""
    @State var composeProject = ""
    @State var envText = ""
    @State var envError = ""
    var body: some View {
        VStack(alignment:.leading,spacing:18) {
            Text("添加服务").font(.title2.weight(.semibold))
            Picker("类型",selection:$kind) { Text("本机服务").tag("process"); Text("后台任务").tag("task"); Text("Docker 容器").tag("container"); Text("Compose").tag("compose") }
            Form {
                TextField("名称",text:$name)
                HStack { TextField("工作目录",text:$cwd); Button("选择…") { let panel = NSOpenPanel(); panel.canChooseDirectories = true; panel.canChooseFiles = false; if panel.runModal() == .OK { cwd = panel.url!.path } } }
                if kind == "process" || kind == "task" { TextField("命令与参数",text:$command); TextField("监听端口（可选）",text:$port) }
                if kind == "container" { TextField("已有容器 ID / 名称",text:$container) }
                if kind == "compose" { TextField("Compose 文件绝对路径",text:$file); TextField("Compose 项目名（可选）",text:$composeProject) }
                if kind != "compose" { TextField("健康检查 URL（可选）",text:$url) }
                if kind != "container" {
                    VStack(alignment:.leading) {
                        Text("必要环境覆盖（每行 KEY=VALUE）").font(.caption).foregroundStyle(.secondary)
                        TextEditor(text:$envText).font(.system(.caption,design:.monospaced)).frame(height:70)
                    }
                }
            }
            if !envError.isEmpty { Text(envError).font(.caption).foregroundStyle(.orange) }
            Text("命令按参数解析；需要管道或重定向时使用 /bin/sh -lc '…'。已有本机进程先查看发现列表；请先结束旧运行再登记启动。").font(.caption).foregroundStyle(.secondary)
            HStack { Spacer(); Button("取消") {dismiss()}; Button(kind == "container" ? "登记" : "登记并启动") {
                var req:[String:Any] = ["action":"launch", "name":name, "cwd":cwd, "kind":kind, "command":command]
                var env:[String:String] = [:]
                for line in envText.split(separator:"\n") where !line.trimmingCharacters(in:.whitespaces).isEmpty {
                    let parts = line.split(separator:"=",maxSplits:1,omittingEmptySubsequences:false)
                    guard parts.count == 2, !parts[0].isEmpty else {envError = "环境配置需要 KEY=VALUE 格式";return}
                    env[String(parts[0])] = String(parts[1])
                }
                req["env"] = env
                if !url.isEmpty {req["url"] = url}
                if let number = Int(port) {req["ports"] = [number]}
                if kind == "container" {req["action"] = "register_container";req["container_id"] = container}
                if kind == "compose" {req["action"] = "register_compose";req["file"] = file; if !composeProject.isEmpty {req["compose_project"] = composeProject}}
                store.perform(req,completion:{_ in dismiss()})
            }.buttonStyle(.borderedProminent).disabled(store.busy || (kind != "compose" && name.isEmpty)) }
        }.padding(26).frame(width:590)
    }
}
struct ObservedConfigView: View {
    let item: ServiceItem
    @ObservedObject var store: Store
    @Environment(\.dismiss) var dismiss
    @State private var command = ""
    @State private var cwd = ""
    @State private var envText = ""
    @State private var inputError = ""
    var body: some View {
        VStack(alignment:.leading,spacing:18) {
            Text("配置启动方式 · " + item.name).font(.title2.weight(.semibold))
            Text("填写仅启动这一项服务的命令。保存不会停止或启动服务；重启会先停止旧运行，再创建新的运行记录。多服务脚本请拆成单项命令。").font(.caption).foregroundStyle(.secondary)
            Form {
                TextField("工作目录",text:$cwd)
                TextField("命令与参数",text:$command)
                VStack(alignment:.leading) {
                    Text("必要环境覆盖（每行 KEY=VALUE；已有值保留）").font(.caption)
                    TextEditor(text:$envText).font(.system(.caption,design:.monospaced)).frame(height:80)
                }
            }
            if !inputError.isEmpty { Text(inputError).font(.caption).foregroundStyle(.orange) }
            HStack {
                Spacer(); Button("取消") { dismiss() }
                Button("保存启动配置") {
                    var env: [String:String] = [:]
                    for line in envText.split(separator:"\n") where !line.trimmingCharacters(in:.whitespaces).isEmpty {
                        let parts = line.split(separator:"=",maxSplits:1,omittingEmptySubsequences:false)
                        guard parts.count == 2, !parts[0].isEmpty else { inputError = "环境配置需要 KEY=VALUE 格式"; return }
                        env[String(parts[0])] = String(parts[1])
                    }
                    store.perform(["action":"configure_observed","id":item.id,"cwd":cwd,"command":command,"env":env],completion:{ _ in dismiss() })
                }.buttonStyle(.borderedProminent).disabled(store.busy || command.trimmingCharacters(in:.whitespaces).isEmpty || cwd.isEmpty)
            }
        }.padding(26).frame(width:610)
        .onAppear {
            cwd = item.spec["cwd"] as? String ?? ""
            command = (item.spec["argv"] as? [String] ?? []).map { "'" + $0.replacingOccurrences(of:"'",with:"'\"'\"'") + "'" }.joined(separator:" ")
        }
    }
}
struct DiscoveryView: View {
    @ObservedObject var store:Store
    @Environment(\.dismiss) var dismiss
    var body:some View {
        VStack(alignment:.leading,spacing:14) {
            Text("发现本机服务").font(.title2.weight(.semibold))
            Text("发现结果不自动接管。容器需明确选择归属项目；本机进程请先核对配置再登记启动。").font(.caption).foregroundStyle(.secondary)
            List {
                Section("未登记的容器") {
                    ForEach(Array((store.discovery["containers"] as? [[String:Any]] ?? []).enumerated()),id:\.offset) { _, item in
                        HStack { Text(item["name"] as? String ?? ""); Spacer(); Text(item["state"] as? String ?? "").foregroundStyle(.secondary)
                            Button("接管到当前项目") {
                                guard let project = store.currentProject else {return}
                                store.perform(["action":"register_container","container_id":item["id"] ?? "","cwd":project.cwd],completion:{_ in dismiss()})
                            }.disabled(store.currentProject == nil || store.busy)
                        }
                    }
                }
                Section("监听进程（仅查看）") {
                    ForEach(Array((store.discovery["processes"] as? [[String:Any]] ?? []).enumerated()),id:\.offset) { _, item in
                        VStack(alignment:.leading) { Text("\(item["name"] as? String ?? "") · PID \(item["pid"] as? Int ?? 0)"); Text((item["ports"] as? [String] ?? []).joined(separator:", ")).font(.caption.monospaced()).foregroundStyle(.secondary) }
                    }
                }
                ForEach(store.discovery["errors"] as? [String] ?? [],id:\.self) {Text($0).font(.caption).foregroundStyle(.orange)}
            }
            HStack { Spacer(); Button("完成") {dismiss()} }
        }.padding(24).frame(width:700,height:520)
    }
}
struct SettingsView: View {
    @ObservedObject var store:Store
    @Environment(\.dismiss) var dismiss
    @State var days = "30"
    var body:some View {
        VStack(alignment:.leading,spacing:18) {
            Text("管理设置").font(.title2.weight(.semibold))
            Text("退出观察器：" + (store.watcher["alive"] as? Bool == true ? "运行中" : "不可用"))
            Text("目标应用：" + (store.watcher["bundle_ids"] as? [String] ?? []).joined(separator:", ")).font(.caption.monospaced())
            if let error = store.watcher["error"] as? String, !error.isEmpty {Text(error).foregroundStyle(.orange)}
            Button("恢复退出观察器") {store.ensureWatcher();store.refresh()}
            Divider()
            Text("服务自动采集：" + (store.capture["alive"] as? Bool == true ? "运行中" : "未运行"))
            Text("已记录 \(store.capture["event_count"] as? Int ?? 0) 次启动上下文。首次启用需安装并审核全局 hooks；采集组件运行不代表所有聊天已加载 hooks。").font(.caption).foregroundStyle(.secondary)
            if let error = store.capture["error"] as? String, !error.isEmpty {Text(error).font(.caption).foregroundStyle(.orange)}
            Button("启动或恢复自动采集") {store.perform(["action":"capture_start"])}.disabled(store.busy)
            Divider()
            Text("每次运行最多 3 MiB 滚动日志；历史默认保留。清理只删除已结束记录。").font(.caption).foregroundStyle(.secondary)
            HStack {TextField("保留天数",text:$days).frame(width:100);Button("清理旧历史") {if let number = Int(days), number > 0 {store.perform(["action":"cleanup","days":number])}}.disabled(store.busy)}
            if !store.deletedProjects.isEmpty {
                Divider()
                Text("已删除项目 · 可恢复").font(.headline)
                ScrollView {
                    VStack(alignment:.leading,spacing:12) {
                        ForEach(store.deletedProjects) { project in
                            HStack {
                                VStack(alignment:.leading,spacing:3) {
                                    Text(project.name).font(.subheadline.weight(.semibold))
                                    Text(project.cwd).font(.caption).foregroundStyle(.secondary).lineLimit(1).truncationMode(.middle)
                                }
                                Spacer()
                                Button("恢复项目") { store.restoreProject(project.id) }.disabled(store.busy)
                            }
                        }
                    }
                }.frame(maxHeight:140)
                Text("恢复登记和历史，不启动服务；同目录已有项目时会拒绝覆盖。").font(.caption).foregroundStyle(.secondary)
            }
            Button("打开数据目录") {NSWorkspace.shared.open(URL(fileURLWithPath:statePath))}
            Text("关闭主窗口继续管理。退出本应用保留服务，退出观察器继续运行。系统关机时不保证停止完成。").font(.caption).foregroundStyle(.secondary)
            HStack {Spacer();Button("完成") {dismiss()}}
        }.padding(26).frame(width:520)
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    let store = Store()
    var window:NSWindow!
    var status:NSStatusItem!
    func applicationDidFinishLaunching(_ notification:Notification) {
        NSApp.setActivationPolicy(.regular)
        installMainMenu()
        let view = MainView(store:store)
        window = NSWindow(contentRect:NSRect(x:0,y:0,width:1200,height:740),styleMask:[.titled,.closable,.miniaturizable,.resizable],backing:.buffered,defer:false)
        window.title = "Codex Service Manager"
        window.isReleasedWhenClosed = false
        window.contentView = NSHostingView(rootView:view)
        window.center(); showWindow()
        status = NSStatusBar.system.statusItem(withLength:NSStatusItem.variableLength)
        status.button?.image = NSImage(systemSymbolName:"terminal",accessibilityDescription:"服务管理")
        let menu = NSMenu(); menu.delegate = self; status.menu = menu
        store.onRefresh = {[weak self] in self?.status.button?.title = " \(self?.store.runningCount ?? 0)"
            self?.status.button?.contentTintColor = self?.store.services.contains(where: { $0.problem != nil || $0.health == "unavailable" || $0.state == "unknown" }) == true ? .systemOrange : nil}
        store.start()
    }
    @objc func showWindow() { window?.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps:true) }
    func installMainMenu() {
        let main = NSMenu()
        let applicationItem = NSMenuItem(title:"Codex Service Manager",action:nil,keyEquivalent:"")
        let applicationMenu = NSMenu(title:"Codex Service Manager")
        let about = NSMenuItem(title:"关于 Codex Service Manager",action:#selector(NSApplication.orderFrontStandardAboutPanel(_:)),keyEquivalent:"")
        about.target = NSApp
        applicationMenu.addItem(about)
        applicationMenu.addItem(.separator())
        let hide = NSMenuItem(title:"隐藏 Codex Service Manager",action:#selector(NSApplication.hide(_:)),keyEquivalent:"h")
        hide.target = NSApp; applicationMenu.addItem(hide)
        let show = NSMenuItem(title:"显示主窗口",action:#selector(showWindow),keyEquivalent:"1")
        show.target = self; applicationMenu.addItem(show)
        let services = NSMenuItem(title:"服务菜单",action:#selector(openServiceMenu),keyEquivalent:"m")
        services.keyEquivalentModifierMask = [.command, .shift]
        services.target = self; applicationMenu.addItem(services)
        applicationMenu.addItem(.separator())
        let quit = NSMenuItem(title:"退出 Codex Service Manager（保留服务）",action:#selector(quitApp),keyEquivalent:"q")
        quit.target = self; applicationMenu.addItem(quit)
        applicationItem.submenu = applicationMenu; main.addItem(applicationItem)
        let editItem = NSMenuItem(title:"编辑",action:nil,keyEquivalent:"")
        let editMenu = NSMenu(title:"编辑")
        for (title,selector,key) in [("撤销","undo:","z"),("重做","redo:","Z"),
                                    ("剪切","cut:","x"),("复制","copy:","c"),("粘贴","paste:","v"),("全选","selectAll:","a")] {
            let item = NSMenuItem(title:title,action:Selector(selector),keyEquivalent:key)
            if selector == "redo:" { item.keyEquivalentModifierMask = [.command,.shift] }
            editMenu.addItem(item)
            if selector == "redo:" { editMenu.addItem(.separator()) }
        }
        editItem.submenu = editMenu; main.addItem(editItem)
        let windowItem = NSMenuItem(title:"窗口",action:nil,keyEquivalent:"")
        let windowMenu = NSMenu(title:"窗口")
        let close = NSMenuItem(title:"关闭窗口",action:#selector(closeWindow),keyEquivalent:"w")
        close.target = self; windowMenu.addItem(close)
        let reopen = NSMenuItem(title:"显示主窗口",action:#selector(showWindow),keyEquivalent:"1")
        reopen.target = self; windowMenu.addItem(reopen)
        windowItem.submenu = windowMenu; main.addItem(windowItem)
        NSApp.mainMenu = main
    }
    @objc func closeWindow() { window?.performClose(nil) }
    @objc func openServiceMenu() {
        DispatchQueue.main.async { [weak self] in self?.status.button?.performClick(nil) }
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender:NSApplication)->Bool {false}
    func applicationShouldHandleReopen(_ sender:NSApplication,hasVisibleWindows flag:Bool)->Bool {
        if !flag { showWindow() }
        return true
    }
    func menuWillOpen(_ menu:NSMenu) {
        menu.removeAllItems()
        let heading = NSMenuItem(title:"\(store.runningCount) 个服务运行中",action:nil,keyEquivalent:"");menu.addItem(heading)
        menu.addItem(.separator())
        for service in store.services.prefix(16) {
            let project = store.projects.first { $0.id == service.projectID }
            let row = NSMenuItem(title:(project?.name ?? "项目") + " / " + service.name + " · " + stateLabel(service.state) + (service.health == "not_configured" ? "" : " / " + stateLabel(service.health)),action:nil,keyEquivalent:"")
            row.toolTip = project?.cwd
            row.image = StatusAppearance(service.state).menuImage
            let actions = NSMenu()
            for (label,action,enabled) in [("查看详情","detail",true),("停止","stop",service.canStop),(service.canStart ? "启动":"重启",service.canStart ? "start":"restart",service.canStart || service.canRestart)] {
                let item = NSMenuItem(title:label,action:#selector(menuAction(_:)),keyEquivalent:"")
                item.target = self;item.representedObject = ["id":service.id,"action":action];item.isEnabled = enabled && !store.busy;actions.addItem(item)
            }
            actions.autoenablesItems = false
            row.submenu = actions; menu.addItem(row)
        }
        menu.addItem(.separator())
        let open = NSMenuItem(title:"打开主窗口",action:#selector(showWindow),keyEquivalent:"");open.target = self;menu.addItem(open)
        let quit = NSMenuItem(title:"退出服务管理（保留服务）",action:#selector(quitApp),keyEquivalent:"q");quit.target = self;menu.addItem(quit)
    }
    @objc func menuAction(_ sender:NSMenuItem) {
        guard let req = sender.representedObject as? [String:String],let id = req["id"],let action = req["action"] else {return}
        if action == "detail" {store.selectedService = id;store.selectedProject = "all";store.loadDetails();showWindow()}
        else {store.perform(["action":action,"id":id])}
    }
    @objc func quitApp() {NSApp.terminate(nil)}
}

if CommandLine.arguments.contains("--watch") {
    ExitWatcher().start()
} else {
    let app = NSApplication.shared
    let delegate = AppDelegate()
    app.delegate = delegate
    app.run()
}
