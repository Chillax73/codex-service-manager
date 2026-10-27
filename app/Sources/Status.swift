import AppKit
import SwiftUI
import Foundation
import Darwin

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
