import WebKit
import AppKit

class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        return true
    }
}

final class WebViewDelegate: NSObject, WKUIDelegate {
    func webView(
        _ webView: WKWebView,
        runOpenPanelWith parameters: WKOpenPanelParameters,
        initiatedByFrame frame: WKFrameInfo,
        completionHandler: @escaping ([URL]?) -> Void
    ) {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.canCreateDirectories = false

        if let window = webView.window {
            panel.beginSheetModal(for: window) { response in
                completionHandler(response == .OK ? panel.urls : nil)
            }
        } else {
            panel.begin { response in
                completionHandler(response == .OK ? panel.urls : nil)
            }
        }
    }
}

let args = CommandLine.arguments
guard args.count > 1, let url = URL(string: args[1]) else { exit(1) }

let app = NSApplication.shared
let delegate = AppDelegate()
let webViewDelegate = WebViewDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)

let mainMenu = NSMenu()
let appMenuItem = NSMenuItem()
mainMenu.addItem(appMenuItem)

let appMenu = NSMenu()
let quitTitle = "Quit Cyrene"
let quitItem = NSMenuItem(title: quitTitle, action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
quitItem.keyEquivalentModifierMask = [.command]
appMenu.addItem(quitItem)
appMenuItem.submenu = appMenu

let fileMenuItem = NSMenuItem()
mainMenu.addItem(fileMenuItem)
let fileMenu = NSMenu(title: "File")
let closeItem = NSMenuItem(title: "Close Window", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
closeItem.keyEquivalentModifierMask = [.command]
fileMenu.addItem(closeItem)
fileMenuItem.submenu = fileMenu

app.mainMenu = mainMenu

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
webView.uiDelegate = webViewDelegate
webView.load(URLRequest(url: url))
window.contentView?.addSubview(webView)
window.makeKeyAndOrderFront(nil)

app.activate(ignoringOtherApps: true)
app.run()
