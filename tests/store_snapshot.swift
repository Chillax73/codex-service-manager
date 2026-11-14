import AppKit
import Foundation
import Combine

func check(_ condition: @autoclosure () -> Bool, _ message: String) {
    precondition(condition(), message)
}
func row(_ id: String, _ project: String, _ state: String = "running") -> [String: Any] {
    ["id": id, "project_id": project, "name": id, "kind": "task", "state": state,
     "health": "not_configured", "cache_state": "recent", "last_checked_at": 10.0,
     "run": ["id": "run-" + id, "state": state], "spec": ["argv": ["echo", String(repeating: "x", count: 1000)]]]
}
func snapshot(_ services: [[String: Any]]) -> [String: Any] {
    ["services": services, "projects": [["id": "a", "name": "A", "last_checked_at": 10.0],
                                      ["id": "b", "name": "B", "last_checked_at": 10.0]],
     "project_sort_preferences": ["sort_key": "name", "direction": "asc"],
     "watcher": ["alive": true, "updated": 10.0], "observation_available": true]
}
let store = Store()
var items = [row("a-1", "a"), row("a-2", "a", "exited"), row("b-1", "b")]
var payload = snapshot(items)
store.applySnapshot(payload)
store.selectedService = "a-1"
store.logs = "preserved output"; store.logRunID = "historical-run"
var publications = 0, servicesPublished = 0
let token = store.objectWillChange.sink { publications += 1 }
let serviceToken = store.$services.dropFirst().sink { _ in servicesPublished += 1 }
store.applySnapshot(payload)
check(publications == 0 && servicesPublished == 0, "identical snapshot must not publish")
items[0]["last_checked_at"] = 20.0
payload["services"] = items
payload["watcher"] = ["alive": true, "updated": 20.0]
payload["projects"] = [["id": "a", "name": "A", "last_checked_at": 20.0],
                       ["id": "b", "name": "B", "last_checked_at": 20.0]]
store.applySnapshot(payload)
check(publications == 0 && servicesPublished == 0, "timestamps and heartbeat must not invalidate Store")
check(store.checkTimes.services["a-1"] == 20 && store.checkTimes.projects["a"] == 20, "latest check time retained independently")
check(store.checkTimes.caption(store.selected!, unavailable: false, showRecent: true) == checkedLabel(20), "selected timestamp uses latest observation")
store.refreshing = true; store.refreshing = false
check(publications == 0 && !store.refreshState.loading, "loading state is isolated")
check(store.logs == "preserved output" && store.logRunID == "historical-run" && store.selectedService == "a-1", "selection and log state preserved")
check(store.projectStatus("a").total == 2 && store.projectStatus("a").running == 1, "project aggregation")
check(store.runningCount == 2 && store.filtered.count == 3, "global aggregate")
store.selectedProject = "a"; store.filter = .running
check(store.filtered.map(\.id) == ["a-1"] && store.displayedRunningCount == 1, "scope and status filter")
store.search = "a-2"; check(store.filtered.isEmpty, "search intersects state filter")
store.filter = nil; check(store.filtered.map(\.id) == ["a-2"], "search cache invalidates")
store.search = ""; store.selectedProject = "all"
items[0]["cache_state"] = "expired"; payload["services"] = items
store.applySnapshot(payload)
check(store.cacheCaption(store.selected!)?.hasPrefix("状态可能过期") == true, "expiry is a business presentation change")
items[0]["cache_state"] = "recent"; payload["services"] = items
payload["observation_available"] = false
store.applySnapshot(payload)
check(store.observationUnavailable && store.cacheCaption(store.selected!) != nil, "worker failure retains stale warning")
payload["observation_available"] = true; store.applySnapshot(payload)
check(!store.observationUnavailable && store.cacheCaption(store.selected!) == nil, "recovery clears stale warning")
items[0]["observation_paused"] = true; payload["services"] = items
store.applySnapshot(payload)
check(store.hasPausedServices && store.cacheCaption(store.selected!)?.hasPrefix("已暂停") == true, "paused cache retains timestamp")
items[0]["state"] = "exited"; items[0]["run"] = ["id": "new-run", "state": "exited", "ended": 30.0]
payload["services"] = items
check(store.applySnapshot(payload), "run transition requests fresh details")
check(store.runningCount == 1 && store.projectStatus("a").running == 0, "aggregates change with state")
store.removeService("b-1")
check(store.runningCount == 0 && store.services.count == 2, "explicit removal updates derived state")
store.updateService(row("a-2", "a"))
check(store.runningCount == 1, "explicit mutation updates derived state")
payload["services"] = [items[1]]
store.applySnapshot(payload)
check(store.selectedService == nil && store.logs.isEmpty && store.logRunID == nil, "removed selection clears dependent state")
for count in [260, 1000, 5000] {
    let large = snapshot((0..<count).map { row("service-\($0)", $0 % 2 == 0 ? "a" : "b") })
    store.applySnapshot(large)
    let before = servicesPublished
    let start = ProcessInfo.processInfo.systemUptime
    for _ in 0..<5 { store.applySnapshot(large) }
    check(servicesPublished == before, "stable large snapshots do not republish")
    check(store.filtered.count == count && store.projectStatus("a").total == count / 2, "large snapshot aggregate correctness")
    print("stable \(count) rows: \((ProcessInfo.processInfo.systemUptime - start) * 1000 / 5) ms/apply")
}
print("PASS store snapshot: stable publication, timestamp isolation, filtering, freshness, selection, mutations, 260/1000/5000 rows")
