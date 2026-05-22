import WebKit
import AppKit

class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        return true
    }
}

let args = CommandLine.arguments
guard args.count > 1, let url = URL(string: args[1]) else { exit(1) }

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)

let window = NSWindow(
    contentRect: NSRect(x: 0, y: 0, width: 1200, height: 800),
    styleMask: [.titled, .closable, .miniaturizable, .resizable],
    backing: .buffered,
    defer: false
)
window.title = "Cyrene"
window.center()
window.isReleasedWhenClosed = false

let webView = WKWebView(frame: window.contentView!.bounds)
webView.autoresizingMask = [.width, .height]
webView.load(URLRequest(url: url))
window.contentView?.addSubview(webView)
window.makeKeyAndOrderFront(nil)

app.activate(ignoringOtherApps: true)
app.run()
