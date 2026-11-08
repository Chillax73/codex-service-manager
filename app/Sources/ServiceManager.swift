import AppKit
import SwiftUI
import Foundation
import Darwin

struct ProjectRow: View, Equatable {
    let project: ProjectItem
    let status: ProjectStatus
    let busy: Bool
    let onRename: () -> Void
    let onPin: () -> Void
    let onScan: () -> Void
    let onDelete: () -> Void
    static func == (lhs: ProjectRow, rhs: ProjectRow) -> Bool { lhs.project == rhs.project && lhs.status == rhs.status && lhs.busy == rhs.busy }
    var body: some View {
        VStack(alignment:.leading,spacing:4) {
            HStack {
                Text(project.name).font(.headline)
                if project.pinned { Image(systemName: "pin.fill").accessibilityLabel("已置顶") }
            }
            Text(project.cwd).font(.caption).foregroundStyle(.secondary).lineLimit(1).truncationMode(.middle)
            HStack(spacing:6) {
                StatusBadge(state:status.state,title:status.label)
                Text("\(status.total) 个服务").font(.caption).foregroundStyle(.secondary)
            }
            if !project.autoScan {
                Text("已暂停自动扫描 · " + checkedLabel(project.lastCheckedAt))
                    .font(.caption).foregroundStyle(.secondary)
            }
            if status.attention > 0 { StatusBadge(state:"attention",title:"需关注 \(status.attention)") }
        }.padding(.vertical,4).contextMenu {
            Button(action: onPin) { Label(project.pinned ? "取消置顶" : "置顶项目", systemImage: project.pinned ? "pin.slash" : "pin") }.disabled(busy)
            Button(action: onScan) { Label(project.autoScan ? "暂停自动扫描" : "恢复自动扫描", systemImage: project.autoScan ? "pause.circle" : "play.circle") }.disabled(busy)
            Button(action:onRename) { Label("重命名项目…",systemImage:"pencil") }.disabled(busy)
            Button(role:.destructive,action:onDelete) { Label("删除项目…",systemImage:"trash") }.disabled(busy)
        }
    }
}

private struct RemoveAutomaticSidebarToggle: ViewModifier {
    @ViewBuilder
    func body(content: Content) -> some View {
        if #available(macOS 14.0, *) {
            content.toolbar(removing: .sidebarToggle)
        } else {
            content
        }
    }
}

private struct SidebarToggleToolbar: ViewModifier {
    @Binding var columnVisibility: NavigationSplitViewVisibility

    @ViewBuilder
    func body(content: Content) -> some View {
        if #available(macOS 14.0, *) {
            content.toolbar {
                // Keep the toggle on the window toolbar even when the sidebar is hidden.
                ToolbarItem(placement: .navigation) {
                    Button {
                        withAnimation {
                            columnVisibility = columnVisibility == .all ? .doubleColumn : .all
                        }
                    } label: {
                        Label("切换项目侧栏", systemImage: "sidebar.left")
                    }
                    .labelStyle(.iconOnly)
                    .help(columnVisibility == .all ? "收起项目侧栏" : "展开项目侧栏")
                }
            }
        } else {
            content
        }
    }
}

private struct SidebarWidthBridge: NSViewRepresentable {
    let preferred: Double
    let save: (Double) -> Void

    final class Marker: NSView {
        var attached: ((NSView) -> Void)?
        var measured: ((Double) -> Void)?
        override func setFrameSize(_ size: NSSize) {
            super.setFrameSize(size)
            DispatchQueue.main.async { [weak self] in self?.measured?(Double(size.width)) }
        }
        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            DispatchQueue.main.async { [weak self] in
                if let self = self { self.attached?(self) }
            }
        }
    }

    final class Coordinator {
        weak var split: NSSplitView?
        weak var sidebar: NSView?
        weak var marker: NSView?
        var preferred: Double
        var save: (Double) -> Void
        private var notification: NSObjectProtocol?
        private var monitor: Any?
        private var draggingDivider = false
        private var lastDividerDown = Date.distantPast
        private var wasCollapsed = false

        init(_ view: SidebarWidthBridge) {
            preferred = view.preferred
            save = view.save
        }

        deinit {
            if let notification = notification { NotificationCenter.default.removeObserver(notification) }
            if let monitor = monitor { NSEvent.removeMonitor(monitor) }
        }

        func attach(_ marker: NSView) {
            guard marker.window != nil else { return }
            self.marker = marker
            if monitor == nil {
                monitor = NSEvent.addLocalMonitorForEvents(matching: [.leftMouseDown, .leftMouseUp]) { [weak self] event in
                    if event.type == .leftMouseDown {
                        self?.beginDrag(event)
                    } else if let self = self, self.draggingDivider, let marker = self.marker {
                        self.measure(Double(marker.frame.width))
                    }
                    return event
                }
            }
            var child: NSView = marker
            while let parent = child.superview {
                if let view = parent as? NSSplitView, view.isVertical, view.arrangedSubviews.first === child {
                    guard split !== view else { return }
                    if let notification = notification { NotificationCenter.default.removeObserver(notification) }
                    split = view
                    sidebar = child
                    notification = NotificationCenter.default.addObserver(forName: NSSplitView.didResizeSubviewsNotification,
                                                                          object: view, queue: .main) { [weak self] _ in
                        self?.didResize()
                    }
                    DispatchQueue.main.async { [weak self] in self?.restore() }
                    return
                }
                child = parent
            }
        }

        private func beginDrag(_ event: NSEvent) {
            draggingDivider = false
            guard let marker = marker, event.window === marker.window else { return }
            let bounds = marker.convert(marker.bounds, to: nil)
            let point = event.locationInWindow
            draggingDivider = abs(point.x - bounds.maxX) <= 7 && point.y >= bounds.minY && point.y <= bounds.maxY
            if draggingDivider { lastDividerDown = Date() }
        }

        func measure(_ width: Double) {
            guard draggingDivider, Date().timeIntervalSince(lastDividerDown) < 5,
                  marker?.window?.inLiveResize == false,
                  width >= 260, abs(width - preferred) >= 1 else { return }
            preferred = width
            save(width)
        }

        private func didResize() {
            guard let split = split, let sidebar = sidebar else { return }
            if split.isSubviewCollapsed(sidebar) || sidebar.frame.width < 100 {
                wasCollapsed = true
                return
            }
            if wasCollapsed {
                wasCollapsed = false
                DispatchQueue.main.async { [weak self] in self?.restore() }
                return
            }
            measure(Double(sidebar.frame.width))
        }

        private func restore() {
            guard let split = split, let sidebar = sidebar, !split.isSubviewCollapsed(sidebar),
                  split.bounds.width >= 910, split.arrangedSubviews.count > 1 else { return }
            draggingDivider = false
            let available = Double(split.bounds.width - 650 - 2 * split.dividerThickness)
            split.setPosition(CGFloat(min(preferred, max(260, available))), ofDividerAt: 0)
        }
    }

    func makeCoordinator() -> Coordinator { Coordinator(self) }
    func makeNSView(context: Context) -> Marker {
        let marker = Marker()
        marker.attached = { [weak coordinator = context.coordinator] in coordinator?.attach($0) }
        marker.measured = { [weak coordinator = context.coordinator] in coordinator?.measure($0) }
        return marker
    }
    func updateNSView(_ marker: Marker, context: Context) {
        context.coordinator.preferred = preferred
        context.coordinator.save = save
        context.coordinator.attach(marker)
    }
}

struct MainView: View {
    @ObservedObject var store: Store
    @State private var adding = false
    @State private var discovering = false
    @State private var managingScan = false
    @State private var settings = false
    @State private var tab = "details"
    @State private var forceItem: ServiceItem?
    @State private var columnVisibility: NavigationSplitViewVisibility = .all
    @State private var configuringService: ServiceItem?
    @State private var renamingService: ServiceItem?
    @State private var deletingService: ServiceItem?
    @AppStorage("CSM.sidebar.width." + statePath) private var savedSidebarWidth = 280.0
    private var sidebarWidth: Double {
        guard savedSidebarWidth.isFinite, savedSidebarWidth >= 260, savedSidebarWidth <= 1600 else { return 280 }
        return savedSidebarWidth
    }
    @State private var renamingProject: ProjectItem?
    @State private var deletingProject: ProjectItem?
    var body: some View {
        NavigationSplitView(columnVisibility: $columnVisibility) {
            VStack(spacing: 0) {
                List(selection: $store.selectedProject) {
                    Label("全部项目", systemImage: "square.grid.2x2").tag("all")
                    let pinned = store.projects.filter { $0.pinned }
                    if !pinned.isEmpty { projectSection("已置顶", pinned) }
                    projectSection("项目", store.projects.filter { !$0.pinned })
                }
                Divider()
                HStack(spacing: 8) {
                    projectSortMenu
                    Button { managingScan = true } label: {
                        Label("扫描管理", systemImage: "checklist")
                    }.buttonStyle(.borderless).fixedSize()
                    Spacer(minLength: 0)
                }.padding(10)
            }.navigationTitle("项目").navigationSplitViewColumnWidth(min: 260, ideal: sidebarWidth)
                .background(SidebarWidthBridge(preferred: sidebarWidth) { savedSidebarWidth = $0 }.allowsHitTesting(false))
                .modifier(RemoveAutomaticSidebarToggle())
        } content: {
            VStack(spacing:0) {
                serviceListHeader
                HStack {
                    TextField("搜索服务", text:$store.search).textFieldStyle(.roundedBorder)
                    Picker("筛选", selection:$store.filter) {
                        Text("全部").tag(nil as ServiceListStatus?)
                        ForEach(ServiceListStatus.allCases, id: \.self) { status in
                            Text(status.title).tag(Optional(status))
                        }
                    }.labelsHidden().frame(width:125).help("按运行状态筛选，与项目和搜索同时生效")
                }.padding(.horizontal,20).padding(.bottom,14)
                Divider()
                if store.filtered.isEmpty {
                    VStack(spacing:12) {
                        Image(systemName:"terminal").font(.system(size:38)).foregroundStyle(.secondary)
                        Text(store.services.isEmpty ? "还没有登记的服务" : "没有符合条件的服务").font(.headline)
                        Text(store.services.isEmpty ? "已加载并启用 hooks 的聊天可自动观察常见服务；其他路径请显式登记或使用发现功能核对。" : "试试其他状态，或清空搜索词。").font(.caption).foregroundStyle(.secondary)
                        Button("添加服务") { adding = true }
                    }.frame(maxWidth:.infinity,maxHeight:.infinity)
                } else {
                    List(selection:$store.selectedService) {
                        ForEach(store.filtered) { item in
                            HStack(alignment:.top,spacing:12) {
                                Image(systemName:item.icon).font(.title3).frame(width:24).foregroundStyle(.secondary)
                                VStack(alignment:.leading,spacing:5) {
                                    HStack { Text(item.name).font(.headline); if item.keep { Image(systemName:"pin.fill").font(.caption).foregroundStyle(.secondary) } }
                                    Text(item.typeName + " · " + item.managementSummary).font(.caption).foregroundStyle(.secondary)
                                    HStack(spacing: 6) {
                                        let status = item.listStatus
                                        StatusBadge(state: status.rawValue, title: status.title)
                                        if status != .attention && needsAttention(item) {
                                            Image(systemName: "exclamationmark.triangle.fill")
                                                .foregroundStyle(.orange)
                                                .accessibilityLabel("需关注")
                                                .help("需关注，查看服务详情")
                                        }
                                    }
                                    if item.observationPaused {
                                        Text("上次已知 · " + checkedLabel(item.lastCheckedAt)).font(.caption).foregroundStyle(.secondary)
                                    }
                                    if let url = item.url { Text(url).font(.caption.monospaced()).foregroundStyle(.secondary).lineLimit(1) }
                                }
                                Spacer()
                            }.padding(.vertical,9).tag(item.id).contextMenu { serviceActions(item) }
                        }
                    }.listStyle(.inset)
                }
                Divider()
                HStack {
                    if store.busy { ProgressView().controlSize(.small) }
                    Text(store.notice.isEmpty ? (store.refreshing ? "正在更新状态…" : "每 2 秒检查开启扫描的项目") : store.notice)
                    Spacer()
                    if let id = store.recentlyDeletedProject { Button("撤销删除") { store.restoreProject(id) }.disabled(store.busy) }
                    Image(systemName:store.watcher["alive"] as? Bool == true ? "eye" : "eye.slash")
                        .help(store.watcher["alive"] as? Bool == true ? "退出观察器运行中" : "退出观察器不可用")
                }.font(.caption).foregroundStyle(.secondary).padding(12)
            }.navigationSplitViewColumnWidth(min:330,ideal:420)
        } detail: {
            if let item = store.selected {
                VStack(alignment:.leading,spacing:16) {
                    HStack {
                        Image(systemName: item.icon).font(.title2)
                        Text(item.name).font(.title2.weight(.semibold))
                        Spacer()
                        Menu { serviceActions(item) } label: { Image(systemName: "ellipsis.circle") }
                            .menuStyle(.borderlessButton).fixedSize().help("服务操作")
                    }
                    HStack {
                        StatusBadge(state:item.state)
                        StatusBadge(state:item.health,prefix:"健康 · ")
                    }.font(.subheadline)
                    HStack {
                        Button(item.kind == "task" ? "重新执行" : (item.canStart ? "启动" : "重启")) { store.action(item.canStart ? "start" : "restart", item) }.disabled(store.busy || (!item.canStart && !item.canRestart))
                        Button(item.kind == "task" ? "取消" : "停止") { store.action("stop", item) }.disabled(store.busy || !item.canStop)
                        if item.state == "stop_failed" { Button("强制停止") { forceItem = item }.disabled(store.busy || !item.canForce) }
                        if let url = item.url, let address = URL(string:url) {
                            Button { NSWorkspace.shared.open(address) } label: { Image(systemName:"arrow.up.right.square") }.help("打开地址")
                            Button { NSPasteboard.general.clearContents(); NSPasteboard.general.setString(url,forType:.string) } label: { Image(systemName:"doc.on.doc") }.help("复制地址")
                        }
                    }
                    if item.observationPaused {
                        Text("已暂停自动扫描 · " + checkedLabel(item.lastCheckedAt))
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    if item.hasPendingConfiguration {
                        Text("配置将在下次成功启动时生效").font(.caption).foregroundStyle(.secondary)
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
                                field("首次登记", creationLabel(item.raw))
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
        .modifier(SidebarToggleToolbar(columnVisibility: $columnVisibility))
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Button { settings = true } label: {
                    Label("管理设置", systemImage: "gearshape")
                }.help("管理设置")
            }
        }
        .frame(minWidth:1000,minHeight:620)
        .onChange(of:store.selectedService) { _ in store.logRunID = nil; store.logs = ""; store.history = []; store.loadDetails() }
        .onChange(of:store.selectedProject) { scope in
            if scope != "all", store.selected?.projectID != scope { store.selectedService = nil }
        }
        .sheet(item:$renamingProject) { RenameProjectView(project:$0,store:store) }
        .sheet(item:$deletingProject) { DeleteProjectView(project:$0,store:store) }
        .sheet(item: $configuringService) { ServiceConfigView(item: $0, store: store) }
        .sheet(item: $renamingService) { RenameServiceView(item: $0, store: store) }
        .sheet(item: $deletingService) { DeleteServiceView(item: $0, store: store) }
        .sheet(isPresented:$adding) { AddView(store:store) }
        .sheet(isPresented:$managingScan) { ScanManagementView(store: store) }
        .sheet(isPresented:$discovering) { DiscoveryView(store:store) }
        .sheet(isPresented:$settings) { SettingsView(store:store) }
        .alert("操作未完成",isPresented:Binding(get:{store.error != nil},set:{if !$0 {store.error = nil}})) { Button("知道了",role:.cancel) {store.error = nil} } message: {Text(store.error ?? "")}
        .confirmationDialog("强制停止会立即终止已确认属于该服务的进程",isPresented:Binding(get:{forceItem != nil},set:{if !$0 {forceItem = nil}}),titleVisibility:.visible) {
            Button("强制停止",role:.destructive) { if let item = forceItem { store.action("force",item) }; forceItem = nil }
            Button("取消",role:.cancel) {forceItem = nil}
        }
    }
    private var serviceListHeader: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text(store.currentProject?.name ?? "全部服务").font(.title2.weight(.semibold))
                Spacer(minLength: 0)
                if let project = store.currentProject {
                    Menu {
                        projectActions(project)
                    } label: {
                        Label("当前项目操作", systemImage: "ellipsis.circle").labelStyle(.iconOnly)
                    }.menuStyle(.borderlessButton).fixedSize().help("当前项目操作")
                }
            }
            Text("\(store.filtered.count) 个服务 · \(store.displayedRunningCount) 个运行中")
                .font(.caption).foregroundStyle(.secondary)
            if let project = store.currentProject, !project.autoScan {
                Text("已暂停自动扫描 · " + checkedLabel(project.lastCheckedAt))
                    .font(.caption).foregroundStyle(.secondary)
            } else if store.selectedProject == "all", store.hasPausedServices {
                Text("含暂停项目的上次已知状态").font(.caption).foregroundStyle(.secondary)
            }
            ViewThatFits(in: .horizontal) {
                serviceListActions
                serviceListActions.labelStyle(.iconOnly)
            }.buttonStyle(.borderless)
        }.frame(maxWidth: .infinity, alignment: .leading).padding(20)
    }

    private var serviceListActions: some View {
        let refreshLabel = store.selectedProject == "all" ? "刷新全部服务" : "刷新当前项目"
        return HStack(spacing: 12) {
            Button { adding = true } label: {
                Label("添加服务", systemImage: "plus")
            }.help("添加服务")
            Button {
                store.perform(["action": "discover"], refreshAfter: false, completion: {
                    store.discovery = $0 as? [String: Any] ?? [:]
                    discovering = true
                })
            } label: {
                Label("发现本机服务", systemImage: "magnifyingglass")
            }.help("发现本机服务").disabled(store.busy)
            Button { store.refresh(projectID: store.selectedProject) } label: {
                Label(refreshLabel, systemImage: "arrow.clockwise")
            }.help(refreshLabel)
        }.fixedSize()
    }

    private var projectSortMenu: some View {
        Menu {
            Picker("排序规则", selection: Binding(get: { store.projectSortKey }, set: { key in
                store.updateProjectSidebar(["action": "project_sort_preferences", "sort_key": key,
                                            "direction": store.projectSortDirection])
            })) {
                Text("项目名称").tag("name")
                Text("创建时间").tag("created_at")
                Text("运行中服务数量").tag("running_count")
            }
            Divider()
            Picker("排序方向", selection: Binding(get: { store.projectSortDirection }, set: { direction in
                store.updateProjectSidebar(["action": "project_sort_preferences", "sort_key": store.projectSortKey,
                                            "direction": direction])
            })) {
                Text("升序").tag("asc")
                Text("降序").tag("desc")
            }
        } label: {
            Label("项目排序", systemImage: "arrow.up.arrow.down")
        }.menuStyle(.borderlessButton).fixedSize().disabled(store.busy)
    }
    private func projectSection(_ title: String, _ projects: [ProjectItem]) -> some View {
        Section(title) {
            ForEach(projects) { project in
                ProjectRow(project: project, status: store.projectStatus(project.id), busy: store.busy,
                           onRename: { renamingProject = project },
                           onPin: { store.togglePin(project) },
                           onScan: { store.toggleScan(project) },
                           onDelete: { deletingProject = project })
                    .equatable().tag(project.id)
            }
        }
    }
    @ViewBuilder private func serviceActions(_ item: ServiceItem) -> some View {
        Button { renamingService = item } label: { Label("重命名服务…", systemImage: "pencil") }.disabled(store.busy)
        Button { configuringService = item } label: { Label("编辑服务配置…", systemImage: "slider.horizontal.3") }.disabled(store.busy)
        Button(role: .destructive) { deletingService = item } label: { Label("删除服务…", systemImage: "trash") }.disabled(store.busy)
    }
    @ViewBuilder func projectActions(_ project: ProjectItem) -> some View {
        Button(project.pinned ? "取消置顶" : "置顶项目") { store.togglePin(project) }.disabled(store.busy)
        Button(project.autoScan ? "暂停自动扫描" : "恢复自动扫描") { store.toggleScan(project) }.disabled(store.busy)
        Button { renamingProject = project } label: { Label("重命名项目…", systemImage:"pencil") }.disabled(store.busy)
        Button(role:.destructive) { deletingProject = project } label: { Label("删除项目…", systemImage:"trash") }.disabled(store.busy)
        Divider()
        Button("结束工作") { store.perform(["action": "end_project", "project_id": project.id]) }.disabled(store.busy)
        Button("停止项目全部服务") {
            store.perform(["action": "batch", "project_id": project.id, "operation": "stop"])
        }.disabled(store.busy)
        Button("重启项目全部服务") {
            store.perform(["action": "batch", "project_id": project.id, "operation": "restart"])
        }.disabled(store.busy)
    }

    func field(_ label:String,_ value:String) -> some View {
        VStack(alignment:.leading,spacing:5) { Text(label).font(.caption).foregroundStyle(.secondary); Text(value).font(.system(.body,design:.monospaced)).textSelection(.enabled) }
    }
}

struct ScanManagementView: View {
    @ObservedObject var store: Store
    @Environment(\.dismiss) private var dismiss
    @State private var selected: Set<String> = []
    @State private var result = ""
    @State private var failure: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("扫描管理").font(.title2.weight(.semibold))
            Text("全量操作适用于执行时已登记的项目。以后首次发现的新项目默认开启自动扫描。")
                .font(.callout).foregroundStyle(.secondary)

            HStack {
                Button("全部启用") { apply(enabled: true, all: true) }
                Button("全部暂停") { apply(enabled: false, all: true) }
            }.disabled(store.busy || store.projects.isEmpty)

            List(store.projects) { project in
                Toggle(isOn: Binding(get: { selected.contains(project.id) }, set: { checked in
                    if checked { selected.insert(project.id) } else { selected.remove(project.id) }
                })) {
                    HStack {
                        VStack(alignment: .leading, spacing: 3) {
                            Text(project.name)
                            Text(project.cwd).font(.caption).foregroundStyle(.secondary)
                                .lineLimit(1).truncationMode(.middle)
                        }
                        Spacer()
                        Text(project.autoScan ? "已启用" : "已暂停").foregroundStyle(.secondary)
                    }
                }.toggleStyle(.checkbox).disabled(store.busy)
            }
            if store.projects.isEmpty {
                Text("还没有已登记的项目").foregroundStyle(.secondary)
            }

            HStack {
                Text("已勾选 \(selected.count) 个项目").foregroundStyle(.secondary)
                Button("启用所选") { apply(enabled: true, all: false) }
                Button("暂停所选") { apply(enabled: false, all: false) }
            }.disabled(store.busy || selected.isEmpty)

            if let failure {
                Text(failure).foregroundStyle(.orange)
            } else if !result.isEmpty {
                Text(result).foregroundStyle(.secondary)
            }
            HStack {
                Spacer()
                Button("完成") { dismiss() }.keyboardShortcut(.defaultAction)
            }
        }.padding(22).frame(width: 560, height: 460)
    }

    private func apply(enabled: Bool, all: Bool) {
        failure = nil
        result = "正在更新…"
        var request: [String: Any] = ["action": "project_scan_policy_batch",
                                      "scope": all ? "all" : "selected", "enabled": enabled]
        if !all { request["ids"] = selected.sorted() }
        store.perform(request, failure: { error in
            failure = error
        }, completion: { response in
            let changed = (response as? [String: Any])?["changed"] as? Int ?? 0
            result = "已更新 \(changed) 个项目"
        })
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
struct RenameServiceView: View {
    let item: ServiceItem
    @ObservedObject var store: Store
    @Environment(\.dismiss) var dismiss
    @State private var name = ""
    @State private var failure = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("重命名服务").font(.title2.weight(.semibold))
            TextField("服务名称", text: $name)
            if !failure.isEmpty { Text(failure).foregroundStyle(.orange) }
            HStack {
                Spacer()
                Button("取消") { dismiss() }
                Button("保存名称") {
                    store.perform(["action": "service_rename", "id": item.id, "name": name],
                                  refreshAfter: false, failure: { failure = $0 }, completion: { result in
                        store.updateService(result)
                        dismiss()
                    })
                }.buttonStyle(.borderedProminent)
                    .disabled(store.busy || name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
        }.padding(24).frame(width: 440).onAppear { name = item.name }
    }
}

struct ServiceConfigView: View {
    let item: ServiceItem
    @ObservedObject var store: Store
    @Environment(\.dismiss) var dismiss
    @State private var name = ""
    @State private var command = ""
    @State private var cwd = ""
    @State private var url = ""
    @State private var envText = ""
    @State private var removeKeys = ""
    @State private var failure = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("编辑服务配置").font(.title2.weight(.semibold))
            Text("名称立即更新。启动和健康检查配置将在下次成功启动时生效。")
                .font(.caption).foregroundStyle(.secondary)
            Form {
                TextField("显示名称", text: $name)
                TextField("健康检查地址", text: $url, prompt: Text("留空可关闭"))
                if item.isLocal {
                    TextField("工作目录", text: $cwd)
                    TextField("命令与参数", text: $command)
                    let keys = item.configuration["env_keys"] as? [String] ?? []
                    if !keys.isEmpty {
                        Text("已保存环境键 " + keys.joined(separator: "、"))
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    Text("新增或替换环境变量（每行 KEY=VALUE）").font(.caption)
                    TextEditor(text: $envText).font(.system(.caption, design: .monospaced)).frame(height: 70)
                    TextField("移除环境键", text: $removeKeys, prompt: Text("多个键以逗号分隔"))
                } else {
                    Text("容器的启动命令、目录和环境由原容器或 Compose 配置管理。")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            if !failure.isEmpty { Text(failure).font(.caption).foregroundStyle(.orange) }
            HStack {
                Spacer()
                Button("取消") { dismiss() }
                Button("保存配置", action: save).buttonStyle(.borderedProminent)
                    .disabled(store.busy || name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
        }.padding(24).frame(width: 610).onAppear {
            name = item.name
            cwd = item.configuration["cwd"] as? String ?? ""
            url = item.configuration["url"] as? String ?? ""
            command = (item.configuration["argv"] as? [String] ?? []).map {
                "'" + $0.replacingOccurrences(of: "'", with: "'\"'\"'") + "'"
            }.joined(separator: " ")
        }
    }

    private func save() {
        var request: [String: Any] = ["action": "service_configure", "id": item.id,
                                      "name": name, "url": url.trimmingCharacters(in: .whitespacesAndNewlines),
                                      "config_revision": item.configurationRevision]
        if item.isLocal {
            if !command.isEmpty || item.configuration["argv"] != nil { request["command"] = command }
            if !cwd.isEmpty || item.configuration["cwd"] != nil { request["cwd"] = cwd }
            var values: [String: String] = [:]
            for line in envText.split(whereSeparator: \.isNewline) where !line.trimmingCharacters(in: .whitespaces).isEmpty {
                let parts = line.split(separator: "=", maxSplits: 1, omittingEmptySubsequences: false)
                guard parts.count == 2 else { failure = "环境变量需要 KEY=VALUE 格式"; return }
                values[parts[0].trimmingCharacters(in: .whitespaces)] = String(parts[1])
            }
            if !values.isEmpty { request["env"] = values }
            let removals = removeKeys.split { $0 == "," || $0 == "，" || $0.isWhitespace }.map(String.init)
            if !removals.isEmpty { request["unset_env"] = removals }
        }
        store.perform(request, refreshAfter: false, failure: { failure = $0 }, completion: { result in
            store.updateService(result)
            dismiss()
        })
    }
}

struct DeleteServiceView: View {
    let item: ServiceItem
    @ObservedObject var store: Store
    @Environment(\.dismiss) var dismiss
    @State private var preview: [String: Any] = [:]
    @State private var loading = false
    @State private var failure = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("删除服务登记").font(.title2.weight(.semibold))
            Text(item.name).font(.headline)
            if loading { ProgressView("正在核对实时状态…").controlSize(.small) }
            ForEach(preview["blockers"] as? [[String: String]] ?? [], id: \.self) { blocker in
                Text(blocker["reason"] ?? "状态未知").foregroundStyle(.orange)
            }
            if preview["can_delete"] as? Bool == true {
                Text("将移除登记及 \(preview["run_count"] as? Int ?? 0) 条运行记录、\(preview["operation_count"] as? Int ?? 0) 条操作记录。")
                Text("源码、容器、卷和日志会保留。服务仍可能被自动采集再次添加。")
                    .font(.caption).foregroundStyle(.secondary)
            } else if !preview.isEmpty {
                Text("请先手动停止服务或核对状态，再重新检查。").font(.caption).foregroundStyle(.secondary)
            }
            if !failure.isEmpty { Text(failure).foregroundStyle(.orange) }
            HStack {
                Button("重新检查", action: check).disabled(loading || store.busy)
                Spacer()
                Button("取消") { dismiss() }
                Button("删除登记", role: .destructive) {
                    store.perform(["action": "service_delete", "id": item.id],
                                  failure: { failure = $0; preview = [:] }, completion: { _ in
                        store.removeService(item.id)
                        dismiss()
                    })
                }.disabled(loading || store.busy || preview["can_delete"] as? Bool != true)
            }
        }.padding(24).frame(width: 510).onAppear(perform: check)
    }

    private func check() {
        loading = true
        failure = ""
        preview = [:]
        DispatchQueue.global(qos: .userInitiated).async {
            do {
                let data = try rpc(["action": "service_delete_preview", "id": item.id]) as? [String: Any] ?? [:]
                DispatchQueue.main.async { preview = data; loading = false }
            } catch {
                DispatchQueue.main.async { failure = error.localizedDescription; loading = false }
            }
        }
    }
}

struct DiscoveryView: View {
    @ObservedObject var store: Store
    @Environment(\.dismiss) private var dismiss
    @State private var projectID: String
    @State private var containerID: String?
    @State private var error: String?

    init(store: Store) {
        self.store = store
        _projectID = State(initialValue: store.currentProject?.id ?? "")
    }

    private var selectedProject: ProjectItem? {
        store.projects.first { $0.id == projectID }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("发现本机服务").font(.title2.weight(.semibold))
            Text("容器可登记到所选项目。本机监听进程用于核对，登记时请确认启动配置。")
                .font(.caption).foregroundStyle(.secondary)
            Picker("归属项目", selection: Binding(get: { selectedProject?.id ?? "" }, set: { projectID = $0 })) {
                Text("请选择归属项目").tag("")
                ForEach(store.projects) { project in
                    Text("\(project.name) · \(project.cwd)").tag(project.id)
                }
            }.disabled(store.busy)
            List(selection: $containerID) {
                Section("未登记的容器") {
                    ForEach(Array((store.discovery["containers"] as? [[String: Any]] ?? []).enumerated()), id: \.offset) { _, item in
                        if let id = item["id"] as? String {
                            HStack {
                                Text(item["name"] as? String ?? "")
                                Spacer()
                                Text(item["state"] as? String ?? "").foregroundStyle(.secondary)
                            }.tag(id)
                        }
                    }
                }
                Section("监听进程（仅查看）") {
                    ForEach(Array((store.discovery["processes"] as? [[String: Any]] ?? []).enumerated()), id: \.offset) { _, item in
                        VStack(alignment: .leading) {
                            Text("\(item["name"] as? String ?? "") · PID \(item["pid"] as? Int ?? 0)")
                            Text((item["ports"] as? [String] ?? []).joined(separator: ", "))
                                .font(.caption.monospaced()).foregroundStyle(.secondary)
                        }
                    }
                }
                ForEach(store.discovery["errors"] as? [String] ?? [], id: \.self) {
                    Text($0).font(.caption).foregroundStyle(.orange)
                }
            }
            if let error {
                Text(error).foregroundStyle(.orange)
            }
            HStack {
                Spacer()
                Button("关闭") { dismiss() }.keyboardShortcut(.cancelAction).disabled(store.busy)
                Button("登记到所选项目") { register() }
                    .disabled(containerID == nil || selectedProject == nil || store.busy)
            }
        }.padding(24).frame(width: 700, height: 520)
    }

    private func register() {
        guard let id = containerID, let project = selectedProject else { return }
        error = nil
        store.perform(["action": "register_container", "container_id": id, "project_id": project.id],
                      failure: { error = $0 }, completion: { _ in dismiss() })
    }
}
struct SettingsView: View {
    @ObservedObject var store:Store
    @Environment(\.dismiss) var dismiss
    @State var days = "30"

    var body:some View {
        ScrollView {
            VStack(alignment:.leading,spacing:18) {
                Text("管理设置").font(.title2.weight(.semibold))
                watcherSettings
                Divider()
                captureSettings
                Divider()
                retentionSettings
                deletedProjectSettings
                Button("打开数据目录") {NSWorkspace.shared.open(URL(fileURLWithPath:statePath))}
                Text("关闭主窗口继续管理。退出本应用保留服务，退出观察器继续运行。系统关机时不保证停止完成。").font(.caption).foregroundStyle(.secondary)
                HStack {Spacer();Button("完成") {dismiss()}}
            }.padding(26)
        }.frame(width:580,height:640)
    }

    @ViewBuilder private var watcherSettings:some View {
        Text("退出观察器：" + (store.watcher["alive"] as? Bool == true ? "运行中" : "不可用"))
        Text("目标应用：" + (store.watcher["bundle_ids"] as? [String] ?? []).joined(separator:", ")).font(.caption.monospaced())
        if let error = store.watcher["error"] as? String, !error.isEmpty {Text(error).foregroundStyle(.orange)}
        Button("恢复退出观察器") {store.ensureWatcher();store.refresh()}
    }

    @ViewBuilder private var captureSettings:some View {
        Text("服务自动采集：" + (store.capture["alive"] as? Bool == true ? "运行中" : "未运行"))
        Text("已记录 \(store.capture["event_count"] as? Int ?? 0) 次启动上下文。首次启用需安装并审核全局 hooks；采集组件运行不代表所有聊天已加载 hooks。").font(.caption).foregroundStyle(.secondary)
        if let error = store.capture["error"] as? String, !error.isEmpty {Text(error).font(.caption).foregroundStyle(.orange)}
        if !store.captureDiagnostic.isEmpty {
            Text(captureDiagnosticText).font(.caption).foregroundStyle(.orange)
        }
        Text("旧聊天、未触发 hooks 的启动和复杂脚本可能漏记；发现结果仅为候选，不会自动接管。").font(.caption).foregroundStyle(.secondary)
        projectCaptureSettings
        Button("启动或恢复自动采集") {store.perform(["action":"capture_start"])}.disabled(store.busy)
    }

    private var captureDiagnosticText:String {
        let stage = store.captureDiagnostic["stage"] as? String ?? ""
        let type = store.captureDiagnostic["type"] as? String ?? ""
        return "最近采集故障：" + stage + " / " + type + "；诊断不保存命令或错误正文。可使用显式登记继续工作。"
    }

    @ViewBuilder private var projectCaptureSettings:some View {
        if let project = store.currentProject {
            Toggle("采集此目录中的 Codex 启动", isOn:Binding(get:{ project.captureEnabled }, set:{ enabled in
                store.perform(["action":"capture_policy", "cwd":project.cwd, "enabled":enabled])
            })).disabled(store.busy)
            Text("此选项控制该工作目录后续的 Codex 启动采集。项目操作菜单还可以暂停全部自动扫描。").font(.caption).foregroundStyle(.secondary)
        } else {
            Text("选择具体项目后可设置该目录的 Codex 启动采集。").font(.caption).foregroundStyle(.secondary)
        }
    }

    @ViewBuilder private var retentionSettings:some View {
        Text("每次运行最多 3 MiB 滚动日志；历史默认保留。按天清理旧历史与采集事件，保留每项最近运行、活动记录与项目回收档案；跳过非本工具日志。").font(.caption).foregroundStyle(.secondary)
        HStack {
            TextField("保留天数",text:$days).frame(width:100)
            Button("清理旧历史") {
                if let number = Int(days), number > 0 {
                    store.perform(["action":"cleanup","days":number])
                }
            }
        }.disabled(store.busy)
    }

    @ViewBuilder private var deletedProjectSettings:some View {
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
        window.center()
        status = NSStatusBar.system.statusItem(withLength:NSStatusItem.variableLength)
        status.button?.image = NSImage(systemSymbolName:"terminal",accessibilityDescription:"服务管理")
        let menu = NSMenu(); menu.delegate = self; status.menu = menu
        store.onRefresh = {[weak self] in self?.status.button?.title = " \(self?.store.runningCount ?? 0)"
            self?.status.button?.contentTintColor = self?.store.services.contains(where: { $0.problem != nil || $0.health == "unavailable" || $0.state == "unknown" }) == true ? .systemOrange : nil}
        store.start(window: window)
        showWindow()
    }
    @objc func showWindow() {
        window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps:true)
        store.refresh()
    }
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
        let cached = store.hasPausedServices ? "（含上次已知状态）" : ""
        let heading = NSMenuItem(title: "\(store.runningCount) 个服务运行中" + cached, action: nil, keyEquivalent: "")
        menu.addItem(heading)
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

@main
struct ServiceManagerMain {
    static func main() {
        if CommandLine.arguments.contains("--watch") {
            ExitWatcher().start()
        } else {
            let app = NSApplication.shared
            let delegate = AppDelegate()
            app.delegate = delegate
            app.run()
        }
    }
}
