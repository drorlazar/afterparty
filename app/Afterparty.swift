// Afterparty.app — native window around the local Afterparty server.
// Starts (or reuses) the server via the repo launcher, always with a fresh session key,
// and reconnects by itself when the server restarts. Built by app/build-app.sh.
import Cocoa
import WebKit
import UserNotifications

let repoDir = (Bundle.main.object(forInfoDictionaryKey: "AfterpartyRepo") as? String) ?? ""

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKScriptMessageHandler, WKUIDelegate, UNUserNotificationCenterDelegate {
    var window: NSWindow!
    var web: WKWebView!
    var connecting = false
    var lastConnect = Date.distantPast

    func applicationDidFinishLaunching(_ n: Notification) {
        buildMenu()
        UNUserNotificationCenter.current().delegate = self
        let cfg = WKWebViewConfiguration()
        // The default (persistent) data store on purpose: the map keeps its own preferences (theme, text size, pane
        // widths, settings) in localStorage. That data, the frame autosave below and saved window state live under
        // ~/Library (WebKit, Caches, HTTPStorages, Preferences, Saved Application State, named after the bundle id);
        // uninstall lists them by exact path and never deletes them (installer.main.native_app_data).
        cfg.userContentController.add(self, name: "cleanup")
        cfg.preferences.setValue(true, forKey: "developerExtrasEnabled")
        web = WKWebView(frame: .zero, configuration: cfg)
        web.navigationDelegate = self
        web.uiDelegate = self
        web.setValue(false, forKey: "drawsBackground")  // no white flash before the page paints
        if #available(macOS 13.3, *) { web.isInspectable = true }

        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1500, height: 920),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "Afterparty"
        window.minSize = NSSize(width: 1040, height: 660)
        window.backgroundColor = NSColor(name: nil) { a in
            a.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua ? NSColor(red: 0.047, green: 0.039, blue: 0.035, alpha: 1)
                                                              : NSColor(red: 0.98, green: 0.98, blue: 0.976, alpha: 1)
        }
        window.contentView = web
        window.setFrameAutosaveName("AfterpartyMain")
        if !window.setFrameUsingName("AfterpartyMain") { window.center() }
        window.makeKeyAndOrderFront(nil)
        showMessage("Starting Afterparty…")
        connect()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ s: NSApplication) -> Bool { true }

    // Quitting stops our own server job, so a restart (e.g. after granting Full Disk Access) starts a fresh process
    // that gets the new permission instead of reusing the old one.
    func applicationWillTerminate(_ n: Notification) {
        let cfgDir = ProcessInfo.processInfo.environment["AFTERPARTY_CONFIG_DIR"] ?? (NSHomeDirectory() + "/.config/afterparty")
        let py = ((try? String(contentsOfFile: cfgDir + "/python", encoding: .utf8)) ?? "")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard !repoDir.isEmpty, !py.isEmpty, FileManager.default.isExecutableFile(atPath: py) else { return }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: py)
        p.arguments = ["-B", "-m", "cleanup.cli", "app", "--stop"]
        p.currentDirectoryURL = URL(fileURLWithPath: repoDir)
        var penv = ProcessInfo.processInfo.environment
        penv["PYTHONPATH"] = repoDir
        p.environment = penv
        p.standardOutput = FileHandle.nullDevice; p.standardError = FileHandle.nullDevice
        let done = DispatchSemaphore(value: 0)
        p.terminationHandler = { _ in done.signal() }
        guard (try? p.run()) != nil else { return }
        if done.wait(timeout: .now() + 5) == .timedOut { p.terminate() }  // quitting must never hang
    }

    // MARK: server
    func connect() {
        if connecting || Date().timeIntervalSince(lastConnect) < 2 { return }
        connecting = true; lastConnect = Date()
        DispatchQueue.global(qos: .userInitiated).async {
            let p = Process()
            let cfgDir = ProcessInfo.processInfo.environment["AFTERPARTY_CONFIG_DIR"] ?? (NSHomeDirectory() + "/.config/afterparty")
            let py = ((try? String(contentsOfFile: cfgDir + "/python", encoding: .utf8)) ?? "")
                .trimmingCharacters(in: .whitespacesAndNewlines)
            guard !repoDir.isEmpty, !py.isEmpty, FileManager.default.isExecutableFile(atPath: py) else {
                DispatchQueue.main.async {
                    self.connecting = false
                    let alert = NSAlert()
                    alert.messageText = "Afterparty isn't set up"
                    alert.informativeText = "Paste the install prompt into Claude Code, or re-run the installer, then open Afterparty again."
                    alert.runModal()
                }
                return
            }
            p.executableURL = URL(fileURLWithPath: py)
            p.arguments = ["-B", "-m", "cleanup.cli", "app", "--launchd", "--no-open"]
            p.currentDirectoryURL = URL(fileURLWithPath: repoDir)
            var penv = ProcessInfo.processInfo.environment
            penv["PYTHONPATH"] = repoDir
            p.environment = penv
            let out = Pipe(), err = Pipe()
            p.standardOutput = out; p.standardError = err
            var url: URL? = nil; var problem = ""
            do {
                try p.run(); p.waitUntilExit()
                let text = String(data: out.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
                problem = String(data: err.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
                url = text.split(separator: "\n").map(String.init).last(where: { $0.hasPrefix("http://127.0.0.1:") }).flatMap(URL.init)
            } catch { problem = error.localizedDescription }
            DispatchQueue.main.async {
                self.connecting = false
                if let u = url { self.web.load(URLRequest(url: u)) }
                else { self.showMessage("Couldn't start the Afterparty server.\n\n\(problem.suffix(600))", retry: true) }
            }
        }
    }

    // Page → app: the server restarted (token rotated) — reconnect with a fresh key.
    // Page → app: progress for the Dock tile, and a notification when a cleanup finishes.
    func userContentController(_ c: WKUserContentController, didReceive m: WKScriptMessage) {
        if (m.body as? String) == "reconnect" { connect(); return }
        guard let d = m.body as? [String: Any], let type = d["type"] as? String else { return }
        if type == "progress" { dock.set((d["fraction"] as? NSNumber)?.doubleValue ?? -1) }
        else if type == "notify" { notify(d["title"] as? String ?? "Afterparty", d["body"] as? String ?? "") }
    }

    let dock = DockProgress()
    func notify(_ title: String, _ body: String) {
        let c = UNUserNotificationCenter.current()
        c.requestAuthorization(options: [.alert, .sound]) { ok, _ in
            guard ok else { return }
            let n = UNMutableNotificationContent(); n.title = title; n.body = body; n.sound = .default
            c.add(UNNotificationRequest(identifier: UUID().uuidString, content: n, trigger: nil))
        }
        if !NSApp.isActive { NSApp.requestUserAttention(.informationalRequest) }
    }
    func userNotificationCenter(_ c: UNUserNotificationCenter, willPresent n: UNNotification,
                                withCompletionHandler h: @escaping (UNNotificationPresentationOptions) -> Void) { h([.banner, .sound]) }

    func webView(_ w: WKWebView, decidePolicyFor r: WKNavigationResponse, decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        if r.isForMainFrame, let h = r.response as? HTTPURLResponse, h.statusCode == 401 || h.statusCode == 403 {
            decisionHandler(.cancel); connect(); return  // stale session key after a server restart
        }
        decisionHandler(.allow)
    }
    func webView(_ w: WKWebView, didFailProvisionalNavigation n: WKNavigation!, withError e: Error) {
        let ns = e as NSError
        if ns.code == NSURLErrorCancelled { return }  // our own cancel (stale key → reconnect) or a redirect
        NSLog("Afterparty: load failed %@ (%ld) %@", ns.domain, ns.code, ns.localizedDescription)
        showMessage("Afterparty server isn't answering — reconnecting…\n\(ns.localizedDescription)")
        DispatchQueue.main.asyncAfter(deadline: .now() + 2) { self.connect() }
    }
    func webViewWebContentProcessDidTerminate(_ w: WKWebView) { w.reload() }

    // Links that leave the app open in the default browser.
    func webView(_ w: WKWebView, decidePolicyFor a: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if let u = a.request.url, let host = u.host, host != "127.0.0.1", u.scheme?.hasPrefix("http") == true {
            NSWorkspace.shared.open(u); decisionHandler(.cancel); return
        }
        decisionHandler(.allow)
    }

    func showMessage(_ text: String, retry: Bool = false) {
        let esc = text.replacingOccurrences(of: "&", with: "&amp;").replacingOccurrences(of: "<", with: "&lt;")
        let html = """
        <html><body style="margin:0;height:100vh;display:grid;place-items:center;background:#0c0a09;color:#b3ada6;
        font:13px -apple-system,sans-serif"><div style="text-align:center;max-width:560px;white-space:pre-wrap">
        <div style="font:700 15px ui-monospace,Menlo,monospace;color:#fafaf9;margin-bottom:10px">Afterparty</div>\(esc)
        \(retry ? "<br><br><button onclick=\"webkit.messageHandlers.cleanup.postMessage('reconnect')\" style=\"background:#f59e0b;border:0;border-radius:8px;padding:8px 14px;font-weight:600\">Try again</button>" : "")
        </div></body></html>
        """
        web.loadHTMLString(html, baseURL: nil)
    }

    // MARK: menu
    func buildMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem(); main.addItem(appItem)
        let app = NSMenu()
        app.addItem(withTitle: "About Afterparty", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        app.addItem(.separator())
        app.addItem(item("Settings…", #selector(settings), ","))
        app.addItem(.separator())
        app.addItem(withTitle: "Hide Afterparty", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        app.addItem(withTitle: "Quit Afterparty", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = app

        let file = NSMenuItem(); main.addItem(file)
        let f = NSMenu(title: "File")
        f.addItem(item("Rescan Disk", #selector(rescan), "r"))
        f.addItem(item("Review Tray…", #selector(review), "t"))
        file.submenu = f

        let edit = NSMenuItem(); main.addItem(edit)
        let e = NSMenu(title: "Edit")
        e.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        e.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        e.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        e.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        edit.submenu = e

        let view = NSMenuItem(); main.addItem(view)
        let v = NSMenu(title: "View")
        v.addItem(item("Back", #selector(back), "["))
        v.addItem(item("Forward", #selector(fwd), "]"))
        v.addItem(item("Whole Disk", #selector(home), "h", [.command, .shift]))
        v.addItem(.separator())
        v.addItem(item("Focus Map", #selector(focus), "z", [.command, .shift]))
        v.addItem(item("Reload Window", #selector(reloadWindow), "r", [.command, .shift]))
        view.submenu = v

        let win = NSMenuItem(); main.addItem(win)
        let w = NSMenu(title: "Window")
        w.addItem(withTitle: "Minimize", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        w.addItem(withTitle: "Zoom", action: #selector(NSWindow.performZoom(_:)), keyEquivalent: "")
        win.submenu = w
        NSApp.windowsMenu = w
        NSApp.mainMenu = main
    }
    func item(_ t: String, _ s: Selector, _ k: String, _ m: NSEvent.ModifierFlags = [.command]) -> NSMenuItem {
        let i = NSMenuItem(title: t, action: s, keyEquivalent: k); i.keyEquivalentModifierMask = m; i.target = self; return i
    }
    func js(_ code: String) { web.evaluateJavaScript(code, completionHandler: nil) }
    func click(_ id: String) { js("document.getElementById('\(id)')?.click()") }
    @objc func settings() { click("brand") }
    @objc func rescan() { click("bRescan") }
    @objc func review() { click("reviewBtn") }
    @objc func back() { click("bBack") }
    @objc func fwd() { click("bFwd") }
    @objc func home() { click("bHome") }
    @objc func focus() { js("document.querySelector('#presets button[data-p=focus]')?.click()") }
    @objc func reloadWindow() { connect() }
}

// Dock tile: the app icon with a progress bar along the bottom while a cleanup or scan runs.
final class DockProgress: NSView {
    private var fraction: Double = -1
    func set(_ f: Double) {
        let tile = NSApp.dockTile
        if f < 0 { if fraction >= 0 { fraction = -1; tile.contentView = nil; tile.display() }; return }
        if abs(f - fraction) < 0.005 && tile.contentView != nil { return }
        fraction = min(1, f)
        if tile.contentView == nil { frame = NSRect(origin: .zero, size: tile.size); tile.contentView = self }
        tile.display()
    }
    override func draw(_ r: NSRect) {
        NSApp.applicationIconImage.draw(in: bounds)
        let bar = NSRect(x: bounds.width * 0.1, y: bounds.height * 0.08, width: bounds.width * 0.8, height: bounds.height * 0.1)
        NSColor(white: 0, alpha: 0.6).setFill(); NSBezierPath(roundedRect: bar, xRadius: bar.height / 2, yRadius: bar.height / 2).fill()
        var fill = bar.insetBy(dx: 2, dy: 2); fill.size.width = max(fill.height, fill.width * CGFloat(fraction))
        NSColor(red: 0.96, green: 0.62, blue: 0.04, alpha: 1).setFill()
        NSBezierPath(roundedRect: fill, xRadius: fill.height / 2, yRadius: fill.height / 2).fill()
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.activate(ignoringOtherApps: true)
app.run()
