import AppKit
final class FixtureDelegate: NSObject, NSApplicationDelegate {
    var window: NSWindow!
    var control: Timer?
    func applicationDidFinishLaunching(_ n:Notification) {
        NSApp.setActivationPolicy(.regular)
        window = NSWindow(contentRect:NSRect(x:0,y:0,width:460,height:240),styleMask:[.titled,.closable],backing:.buffered,defer:false)
        window.title = "退出观察器验收（测试应用）"; window.isReleasedWhenClosed = false
        let message = NSTextField(labelWithString:"关闭窗口应保留服务；退出本测试应用应触发停止。")
        message.frame=NSRect(x:20,y:140,width:420,height:40)
        window.contentView?.addSubview(message)
        let button=NSButton(title:"退出测试应用",target:self,action:#selector(quit))
        button.frame=NSRect(x:140,y:65,width:180,height:36);window.contentView?.addSubview(button)
        window.center();show()
        if let index = CommandLine.arguments.firstIndex(of: "--control"), index + 1 < CommandLine.arguments.count {
            let path = CommandLine.arguments[index + 1]
            control = Timer.scheduledTimer(withTimeInterval: 0.1, repeats: true) { [weak self] _ in
                guard let self = self, let action = try? String(contentsOfFile: path, encoding: .utf8),
                      (try? FileManager.default.removeItem(atPath: path)) != nil else { return }
                if action == "close" { self.window.performClose(nil) }
                if action == "quit" { self.quit() }
            }
        }
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender:NSApplication)->Bool {false}
    func applicationShouldHandleReopen(_ sender:NSApplication,hasVisibleWindows flag:Bool)->Bool {show();return true}
    func show(){window?.makeKeyAndOrderFront(nil);NSApp.activate(ignoringOtherApps:true)}
    @objc func quit(){NSApp.terminate(nil)}
}
let app=NSApplication.shared;let delegate=FixtureDelegate();app.delegate=delegate;app.run()
