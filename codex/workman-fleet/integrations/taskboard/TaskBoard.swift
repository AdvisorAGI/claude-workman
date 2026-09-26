import Cocoa
import Darwin

// One UI instance per login; source session/task files are never modified.
let boardLockPath = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".claude/board/taskboard.lock").path
let boardLock = Darwin.open(boardLockPath, O_CREAT | O_RDWR, S_IRUSR | S_IWUSR)
if !CommandLine.arguments.contains("--self-test") && (boardLock < 0 || flock(boardLock, LOCK_EX | LOCK_NB) != 0) { exit(0) }
var textScale: CGFloat = 5
var showCompleted = false


// MARK: - Model

struct Item {
    let text: String
    let project: String
    let state: String        // pending | working | blocked | done
    let est: Int?            // estimated minutes
    let started: Double?
    let finished: Double?
    let ask: String          // what Tariqul has to decide, when blocked
}

struct Sess {
    let title: String
    let task: String
    let project: String
    let items: [Item]
    let summary: String
    let updated: Date
    var fresh: Bool { Date().timeIntervalSince(updated) < 300 }
    var working: Bool { items.contains { $0.state == "working" } }
    var blocked: Bool { items.contains { $0.state == "blocked" } }
}

let sessionsDir = ProcessInfo.processInfo.environment["WORKMAN_BOARD_SESSIONS"].map { URL(fileURLWithPath: $0) } ?? FileManager.default.homeDirectoryForCurrentUser
    .appendingPathComponent(".claude/board/sessions")

func num(_ v: Any?) -> Double? {
    if let d = v as? Double { return d }
    if let i = v as? Int { return Double(i) }
    return nil
}

func loadSessions() -> [Sess] {
    guard let urls = try? FileManager.default.contentsOfDirectory(
        at: sessionsDir, includingPropertiesForKeys: nil) else { return [] }
    var out: [Sess] = []
    for u in urls where u.pathExtension == "json" {
        guard let data = try? Data(contentsOf: u),
              let o = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        else { continue }
        let items = (o["items"] as? [[String: Any]] ?? []).map { i in
            Item(text: i["text"] as? String ?? "",
                 project: i["project"] as? String ?? "",
                 state: i["state"] as? String ?? "pending",
                 est: num(i["est"]).map { Int($0) },
                 started: num(i["started"]),
                 finished: num(i["finished"]),
                 ask: i["ask"] as? String ?? "")
        }
        out.append(Sess(
            title: o["title"] as? String ?? u.deletingPathExtension().lastPathComponent,
            task: o["task"] as? String ?? "",
            project: o["project"] as? String ?? "",
            items: items,
            summary: o["summary"] as? String ?? "",
            updated: Date(timeIntervalSince1970: num(o["updated"]) ?? 0)))
    }
    // Anything waiting on him first, then anything actually running, then recent.
    return out.sorted {
        if $0.blocked != $1.blocked { return $0.blocked }
        if $0.working != $1.working { return $0.working }
        return $0.updated > $1.updated
    }
}

// A reported action is not a verification. Match IDs against the local journal.
var proofCacheKey = ""
var proofCacheLabel = "No current verified evidence"
func verifiedLabel(_ status: [String: Any]) -> String {
    guard let id = status["verification_event"] as? String, id.count == 32 else { return "No current verified evidence" }
    let url = ProcessInfo.processInfo.environment["WORKMAN_BOARD_JOURNAL"].map { URL(fileURLWithPath: $0) } ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".grok/workman-learn/journal.jsonl")
    let attr = try? FileManager.default.attributesOfItem(atPath: url.path)
    let key = id + String(describing: status["device"]) + String(describing: attr?[.modificationDate])
    if proofCacheKey == key { return proofCacheLabel }
    proofCacheKey = key; proofCacheLabel = "Evidence needs rechecking"
    guard let size = attr?[.size] as? NSNumber, size.intValue < 20_000_000,
          let data = try? String(contentsOf: url, encoding: .utf8) else { return proofCacheLabel }
    var events: [String: [String: Any]] = [:]
    var order: [String: Int] = [:]
    var corrections = Set<String>()
    for line in data.split(separator: "\n") {
        guard let bytes = String(line).data(using: .utf8),
              let row = (try? JSONSerialization.jsonObject(with: bytes)) as? [String: Any],
              row["tool"] as? String == "workman_fleet_v1.1",
              let event = row["payload"] as? [String: Any], let eid = event["id"] as? String else { continue }
        order[eid] = events.count; events[eid] = event
        if let corrected = event["corrects"] as? String { corrections.insert(corrected) }
    }
    if let verification = events[id], verification["verified"] as? Bool == true, !corrections.contains(id),
       let aid = verification["verifies"] as? String, let action = events[aid], action["ok"] as? Bool == true,
       let sid = verification["evidence_id"] as? String, let shot = events[sid], shot["action"] as? String == "shot",
       let sha = shot["capture_sha256"] as? String, sha.count == 64,
       sha.range(of: "^[a-f0-9]{64}$", options: .regularExpression) != nil,
       let ai = order[aid], let si = order[sid], let vi = order[id], ai < si, si < vi,
       verification["node"] as? String == status["device"] as? String,
       action["node"] as? String == status["device"] as? String,
       shot["node"] as? String == status["device"] as? String {
        proofCacheLabel = (action["action"] as? String ?? "action") + " visually verified"
    }
    return proofCacheLabel
}

// MARK: - Colours

let bg      = NSColor(calibratedRed: 0.07, green: 0.07, blue: 0.09, alpha: 1.0)
let cHead   = NSColor(calibratedWhite: 0.55, alpha: 1)
let cTitle  = NSColor(calibratedRed: 0.62, green: 0.78, blue: 1.00, alpha: 1)
let cTask   = NSColor(calibratedWhite: 1.00, alpha: 1)
let cDone   = NSColor(calibratedRed: 0.42, green: 0.80, blue: 0.52, alpha: 1)
let cWork   = NSColor(calibratedRed: 1.00, green: 0.78, blue: 0.35, alpha: 1)
let cBlock  = NSColor(calibratedRed: 1.00, green: 0.42, blue: 0.42, alpha: 1)
let cPend   = NSColor(calibratedWhite: 0.55, alpha: 1)
let cProj   = NSColor(calibratedRed: 0.58, green: 0.55, blue: 0.85, alpha: 1)
let cTime   = NSColor(calibratedWhite: 0.48, alpha: 1)
let cSum    = NSColor(calibratedWhite: 0.70, alpha: 1)
let cStale  = NSColor(calibratedWhite: 0.38, alpha: 1)

func para(_ top: CGFloat, indent: CGFloat = 0) -> NSMutableParagraphStyle {
    let p = NSMutableParagraphStyle()
    p.paragraphSpacingBefore = top
    p.lineSpacing = 2
    p.lineBreakMode = .byWordWrapping
    p.firstLineHeadIndent = indent
    p.headIndent = indent + 13
    return p
}

func run(_ s: String, _ baseSize: CGFloat, _ c: NSColor, bold: Bool = false,
         style: NSParagraphStyle) -> NSAttributedString {
    let size = max(16, baseSize + textScale)
    return NSAttributedString(string: s, attributes: [
        .font: bold ? NSFont.systemFont(ofSize: size, weight: .semibold)
                    : NSFont.systemFont(ofSize: size, weight: .regular),
        .foregroundColor: c, .paragraphStyle: style])
}

func mins(_ sec: Double) -> String {
    let m = Int((sec / 60).rounded())
    return m < 60 ? "\(m)m" : String(format: "%dh%02d", m / 60, m % 60)
}

/// Time column for one item: elapsed, actual vs estimate, or the estimate alone.
func timing(_ it: Item) -> String {
    switch it.state {
    case "done":
        if let s = it.started, let f = it.finished, f > s {
            var t = mins(f - s)
            if let e = it.est { t += " / \(e)m" }
            return t
        }
        return it.est.map { "\($0)m est" } ?? ""
    case "working":
        if let s = it.started {
            var t = mins(Date().timeIntervalSince1970 - s)
            if let e = it.est { t += " / \(e)m" }
            return t
        }
        return it.est.map { "~\($0)m" } ?? ""
    default:
        return it.est.map { "~\($0)m" } ?? ""
    }
}

/// Returns the board and the range of the step that should stay on screen.
func build() -> (NSAttributedString, NSRange?) {
    let out = NSMutableAttributedString()
    var focus: NSRange? = nil
    let sessions = loadSessions()
    let wmPath = ProcessInfo.processInfo.environment["WORKMAN_BOARD_STATUS"].map { URL(fileURLWithPath: $0) } ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".grok/workman-learn/fleet-status.json")
    if let data = try? Data(contentsOf: wmPath), let wm = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
       wm["schema_version"] as? Int == 1, wm["device"] is String, wm["task"] is String {
        let device = wm["device"] as? String ?? "unknown device"
        let task = wm["task"] as? String ?? "Workman"
        let updated = wm["updated"] as? String ?? "unknown"
        out.append(run("WORKMAN · \(device)\n", 15, cTitle, bold: true, style: para(0)))
        out.append(run(task + "\n", 18, cTask, bold: true, style: para(4)))
        out.append(run("Current: " + (wm["current_action"] as? String ?? "unknown") + " · " + (wm["outcome"] as? String ?? "unverified") + "\n", 12, cSum, style: para(3)))
        out.append(run("Verified: " + verifiedLabel(wm) + "\n", 14, cDone, style: para(4)))
        out.append(run("Next: " + (wm["next_action"] as? String ?? "await task") + "\n", 14, cWork, style: para(4)))
        out.append(run("Checked: " + updated + "\n", 11, cTime, style: para(4)))
        let grantTime = (wm["permissions_observed_at"] as? String).flatMap { ISO8601DateFormatter().date(from: $0) }
        if let time = grantTime, Date().timeIntervalSince(time) > 300 {
            out.append(run("Permission status stale · recheck\n", 13, cBlock, style: para(3)))
        } else if let permissions = wm["permissions"] as? [String: Bool] {
            for (number, key, label) in [(1, "screen_recording", "Screen Recording"), (2, "accessibility", "Accessibility")] {
                if permissions[key] == false { out.append(run("\(number). Human approval: \(label) for Workman\n", 14, cBlock, style: para(3))) }
            }
        }
    }

    let f = DateFormatter(); f.dateFormat = "HH:mm"
    out.append(run("TASK BOARD   ·   \(f.string(from: Date()))\n", 12, cHead,
                   bold: true, style: para(0)))

    let blockedCount = sessions.reduce(0) { $0 + $1.items.filter { $0.state == "blocked" }.count }
    if blockedCount > 0 {
        let word = blockedCount == 1 ? "step needs" : "steps need"
        out.append(run("⚠ \(blockedCount) \(word) your go-ahead\n", 15, cBlock,
                       bold: true, style: para(7)))
    }

    if sessions.isEmpty {
        out.append(run("No sessions reporting yet.\n", 15, cPend, style: para(10)))
        return (out, nil)
    }

    for s in sessions {
        let dot = s.fresh ? "●" : "○"
        out.append(run("\(dot) \(s.title)\n", 14, s.fresh ? cTitle : cStale,
                       bold: true, style: para(12)))
        if !s.task.isEmpty {
            out.append(run(s.task + "\n", 18, s.fresh ? cTask : cStale,
                           bold: true, style: para(3)))
        }

        let done = s.items.filter { $0.state == "done" }.count
        if done > 0 && !showCompleted {
            out.append(run("\(done) completed · collapsed\n", 12, cTime, style: para(3)))
        }
        var seen = Set<String>()
        for it in s.items {
            if it.state == "done" && !showCompleted { continue }
            let key = it.state + "|" + it.project + "|" + it.text
            if !seen.insert(key).inserted { continue }
            let glyph: String, colour: NSColor, bold: Bool
            switch it.state {
            case "done":    glyph = "✓"; colour = cDone;  bold = false
            case "working": glyph = "▸"; colour = cWork;  bold = true
            case "blocked": glyph = "✋"; colour = cBlock; bold = true
            default:        glyph = "○"; colour = cPend;  bold = false
            }
            let size: CGFloat = (it.state == "pending" || it.state == "done") ? 13.5 : 15
            let st = para(3, indent: 6)
            let start = out.length

            out.append(run("\(glyph) ", size, colour, bold: bold, style: st))
            if !it.project.isEmpty {
                out.append(run("\(it.project) ", size - 1.5,
                               it.state == "done" ? cTime : cProj, style: st))
            }
            out.append(run(it.text, size, colour, bold: bold, style: st))
            let t = timing(it)
            if !t.isEmpty {
                out.append(run("  \(t)", size - 2, cTime, style: st))
            }
            out.append(run("\n", size, colour, style: st))

            if it.state == "blocked" && !it.ask.isEmpty {
                out.append(run("needs you: \(it.ask)\n", 13, cBlock, style: para(1, indent: 22)))
            }
            // Keep the blocked step on screen above all else, else the running one.
            if it.state == "blocked" || (it.state == "working" && focus == nil) {
                if it.state == "blocked" || focus == nil {
                    focus = NSRange(location: start, length: out.length - start)
                }
            }
        }

        if !s.summary.isEmpty {
            out.append(run(s.summary + "\n", 12.5, cSum, style: para(5)))
        }
    }
    return (out, focus)
}

// MARK: - Window

final class FollowScroll: NSScrollView {
    var onScrollAway: (() -> Void)?
    override func scrollWheel(with event: NSEvent) {
        super.scrollWheel(with: event)
        if contentView.bounds.minY > 8 { onScrollAway?() }
    }
}

final class Board: NSObject, NSApplicationDelegate {
    var panel: NSPanel!
    var textView: NSTextView!
    var scroll: FollowScroll!
    var resume: NSButton!
    var lastRendered = ""
    var following = true
    var statusItem: NSStatusItem!
    let width: CGFloat = 470
    let toolbarHeight: CGFloat = 40

    func applicationDidFinishLaunching(_ n: Notification) {
        let vis = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 960, height: 540)
        let rect = NSRect(x: vis.maxX - width - 12, y: vis.minY + 12,
                          width: min(width, vis.width - 24), height: min(360, vis.height * 0.55))
        panel = NSPanel(contentRect: rect, styleMask: [.titled, .resizable, .nonactivatingPanel], backing: .buffered, defer: false)
        panel.title = "Workman · task progress"
        panel.isMovableByWindowBackground = false
        panel.minSize = NSSize(width: 360, height: 200)
        panel.backgroundColor = bg
        panel.hasShadow = true
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.isFloatingPanel = true
        panel.hidesOnDeactivate = false
        panel.setFrameAutosaveName("WorkmanTaskboardV11")

        let root = panel.contentView!
        scroll = FollowScroll(frame: NSRect(x: 0, y: 0, width: root.bounds.width, height: root.bounds.height - toolbarHeight))
        scroll.autoresizingMask = [.width, .height]
        scroll.hasVerticalScroller = true
        scroll.drawsBackground = false
        scroll.onScrollAway = { [weak self] in self?.pauseLive() }
        textView = NSTextView(frame: NSRect(x: 0, y: 0, width: root.bounds.width, height: 10))
        textView.isEditable = false
        textView.isSelectable = true
        textView.drawsBackground = false
        textView.textContainerInset = NSSize(width: 14, height: 12)
        textView.autoresizingMask = [.width]
        textView.isVerticallyResizable = true
        textView.isHorizontallyResizable = false
        textView.textContainer?.widthTracksTextView = true
        scroll.documentView = textView
        root.addSubview(scroll)

        let controls = NSStackView(frame: NSRect(x: 10, y: root.bounds.height - toolbarHeight, width: root.bounds.width - 20, height: toolbarHeight))
        controls.autoresizingMask = [.width, .minYMargin]
        controls.orientation = .horizontal; controls.spacing = 8
        resume = NSButton(title: "Live · pause", target: self, action: #selector(toggleLive))
        for button in [resume!, NSButton(title: "A−", target: self, action: #selector(smaller)),
                       NSButton(title: "A+", target: self, action: #selector(larger)),
                       NSButton(title: "Completed", target: self, action: #selector(completed))] {
            button.font = .systemFont(ofSize: 14, weight: .medium)
            controls.addArrangedSubview(button)
        }
        root.addSubview(controls)
        // Hide without discarding tasks; the menu-bar control always reopens it.
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.title = "WM"
        statusItem.button?.toolTip = "Show or hide Workman task progress"
        statusItem.button?.target = self
        statusItem.button?.action = #selector(togglePanel)
        clampToScreen()
        panel.orderFrontRegardless()
        refresh()
        Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in self?.refresh() }
    }

    func pauseLive() { following = false; resume.title = "Resume live" }
    @objc func togglePanel() {
        if panel.isVisible { panel.orderOut(nil) }
        else { clampToScreen(); panel.orderFrontRegardless() }
    }
    @objc func toggleLive() {
        if following { pauseLive() }
        else { following = true; resume.title = "Live · pause"; lastRendered = ""; refresh() }
    }
    @objc func smaller() { textScale = max(1, textScale - 1); rerender() }
    @objc func larger() { textScale = min(11, textScale + 1); rerender() }
    @objc func completed() { showCompleted.toggle(); rerender() }
    func rerender() {
        let old = following; following = true; lastRendered = ""; refresh(); following = old
    }
    func clampToScreen() {
        let vis = panel.screen?.visibleFrame ?? NSScreen.main!.visibleFrame
        var frame = panel.frame
        frame.size.width = min(frame.width, vis.width - 24)
        frame.size.height = min(frame.height, vis.height - 24)
        frame.origin.x = min(max(frame.minX, vis.minX + 12), vis.maxX - frame.width - 12)
        frame.origin.y = min(max(frame.minY, vis.minY + 12), vis.maxY - frame.height - 12)
        if panel.frame != frame { panel.setFrame(frame, display: true, animate: false) }
    }
    func refresh() {
        guard following else { return } // User scroll-away freezes the readable view.
        let (attr, _) = build()
        guard attr.string != lastRendered else { return }
        lastRendered = attr.string
        textView.textStorage?.setAttributedString(attr)
        clampToScreen()
        scroll.contentView.scroll(to: .zero)
        scroll.reflectScrolledClipView(scroll.contentView)
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
if CommandLine.arguments.contains("--self-test") {
    guard ProcessInfo.processInfo.environment["WORKMAN_BOARD_SESSIONS"] != nil else { exit(2) }
    let text = build().0.string
    precondition(!text.contains("completed-fixture-item"))
    precondition(text.components(separatedBy: "active-fixture-item").count == 2)
    precondition(text.contains("collapsed"))
    showCompleted = true
    precondition(build().0.string.contains("completed-fixture-item"))
    print("Taskboard model: 4 checks passed; no task files changed")
    if let expected = ProcessInfo.processInfo.environment["WORKMAN_BOARD_PROOF_EXPECT"],
       let path = ProcessInfo.processInfo.environment["WORKMAN_BOARD_STATUS"],
       let data = try? Data(contentsOf: URL(fileURLWithPath: path)),
       let status = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
        precondition(verifiedLabel(status) == expected)
        print("Taskboard evidence: 1 check passed")
    }
    exit(0)
}
let d = Board()
app.delegate = d
app.run()
