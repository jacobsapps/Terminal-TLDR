import AppKit
import SwiftUI
import ServiceManagement

struct ServiceStatus: Decodable {
    let version: Int?
    let model: String?
    let tabs: Int
    let agentUpdates: Int?
    let automaticConnected: Bool?
    let automaticTabs: Int?
    let automaticError: String?
    let refreshRunning: Bool?
    let refreshDone: Int?
    let refreshChanged: Int?
    let refreshFailed: Int?
    let refreshSkipped: Int?
}

struct Health: Decodable {
    let version: String
    let service: ServiceStatus?
    let installed: Bool
    let daemonLoaded: Bool
    let iterm: Bool
    let codex: Bool
    let authentication: String
    let hooks: [String: Bool]
    let model: String
    let reasoning: String
    let models: [Choice]
    let reasoningOptions: [Choice]
    let runtime: String
}

struct Choice: Decodable, Identifiable {
    let id: String
    let name: String
}

struct HelperOutput {
    let data: Data
    let success: Bool
}

@MainActor
final class AppModel: ObservableObject {
    @Published var health: Health?
    @Published var busy = false
    @Published var checking = false
    @Published var message: String?
    @Published var failed = false
    @Published var loginEnabled = SMAppService.mainApp.status == .enabled
    var running: Bool { health?.service != nil }
    var installed: Bool { health?.installed == true }
    var activeService: Bool { health?.daemonLoaded == true }
    var automatic: Bool { health?.service?.automaticConnected == true }
    var fullyCovered: Bool {
        automatic && health?.service?.automaticTabs == health?.service?.tabs
    }
    var statusText: String {
        if let status = health?.service {
            if automatic {
                return fullyCovered ? "Automatic titles on for \(status.tabs) tabs"
                    : "\(status.automaticTabs ?? 0) of \(status.tabs) tabs automatic"
            }
            return "Connecting to your agents…"
        }
        return installed ? (activeService ? "Waiting for iTerm2" : "Paused") : "Ready to set up"
    }

    nonisolated static func execute(_ action: String, arguments: [String] = []) throws -> HelperOutput {
        guard let resources = Bundle.main.resourceURL else {
            throw NSError(domain: "TerminalTLDR", code: 1,
                          userInfo: [NSLocalizedDescriptionKey: "The app's helper is missing. Reinstall Terminal TLDR."])
        }
        let process = Process()
        process.executableURL = resources.appendingPathComponent("runtime/terminal-tldr")
        process.arguments = [action] + arguments
        var environment = ProcessInfo.processInfo.environment
        environment["PATH"] = NSHomeDirectory() + "/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        process.environment = environment
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe
        try process.run()
        let timeout = DispatchWorkItem { if process.isRunning { process.terminate() } }
        DispatchQueue.global().asyncAfter(deadline: .now() + 60, execute: timeout)
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        timeout.cancel()
        return HelperOutput(data: data, success: process.terminationStatus == 0)
    }

    func configure(model: String, reasoning: String) async {
        guard !busy else { return }
        busy = true
        defer { busy = false }
        do {
            let result = try await Task.detached {
                try Self.execute("configure", arguments: ["--model", model, "--reasoning", reasoning])
            }.value
            let response = (try? JSONSerialization.jsonObject(with: result.data)) as? [String: Any]
            failed = !result.success
            message = response?[result.success ? "message" : "error"] as? String
            await readHealth()
        } catch {
            failed = true
            message = error.localizedDescription
        }
    }

    func setLogin(_ enabled: Bool) {
        do {
            if enabled && SMAppService.mainApp.status != .enabled { try SMAppService.mainApp.register() }
            else if enabled { return }
            else { try SMAppService.mainApp.unregister() }
            failed = false
            message = SMAppService.mainApp.status == .requiresApproval
                ? "Allow Terminal TLDR in System Settings → Login Items." : nil
        } catch {
            failed = true
            message = "Couldn't change login startup: \(error.localizedDescription)"
        }
        loginEnabled = SMAppService.mainApp.status == .enabled
    }

    func refresh() async {
        guard !busy && !checking else { return }
        checking = true
        defer { checking = false }
        await readHealth()
        // Installation can return before iTerm has accepted the helper.
        for _ in 0..<10 {
            if !activeService || running { break }
            try? await Task.sleep(nanoseconds: 2_000_000_000)
            await readHealth()
        }
    }

    private func readHealth() async {
        do {
            let result = try await Task.detached { try Self.execute("doctor") }.value
            health = try JSONDecoder().decode(Health.self, from: result.data)
            loginEnabled = SMAppService.mainApp.status == .enabled
        } catch {
            failed = true
            message = "Couldn't check the helper: \(error.localizedDescription)"
        }
    }

    func perform(_ action: String) async {
        guard !busy else { return }
        busy = true
        message = nil
        defer { busy = false }
        do {
            let result = try await Task.detached { try Self.execute(action) }.value
            let response = (try? JSONSerialization.jsonObject(with: result.data)) as? [String: Any]
            failed = !result.success
            message = action == "install" && result.success ? "Automatic titles enabled." : response?[result.success ? "message" : "error"] as? String
                ?? (result.success ? (action == "pause" ? "Paused. Your terminals and titles are untouched." : "Background service started.")
                    : "The helper couldn't finish. Open the local log for details.")
            try? await Task.sleep(nanoseconds: 1_000_000_000)
            await readHealth()
            if action == "refresh" && result.success {
                for _ in 0..<90 {
                    guard let status = health?.service else {
                        failed = true
                        message = "The helper disconnected. Try Resume."
                        break
                    }
                    if status.refreshRunning != true {
                        let done = status.refreshDone ?? 0
                        let changed = status.refreshChanged ?? 0
                        let errors = status.refreshFailed ?? 0
                        let skipped = status.refreshSkipped ?? 0
                        failed = errors > 0 || skipped > 0
                        message = errors > 0 ? "Reviewed \(done) tabs. \(errors) failed; try again."
                            : skipped > 0 ? "Reviewed \(done) tabs. \(skipped) couldn't be matched."
                            : done == 0 ? "No connected agent tabs to review."
                            : changed == 0 ? "Checked \(done) tabs. Titles are current."
                            : "Updated \(changed) titles. \(done - changed) already current."
                        break
                    }
                    message = "Reviewing tasks… \(status.refreshDone ?? 0) checked."
                    try? await Task.sleep(nanoseconds: 1_000_000_000)
                    await readHealth()
                }
                if health?.service?.refreshRunning == true {
                    message = "Still reviewing in the background."
                }
            }
        } catch {
            failed = true
            message = error.localizedDescription
        }
    }

    func open(_ address: String) {
        if let url = URL(string: address) { NSWorkspace.shared.open(url) }
    }

    func showLog() {
        NSWorkspace.shared.open(URL(fileURLWithPath: NSHomeDirectory()).appendingPathComponent(".terminal-tldr/service.log"))
    }
}

struct ContentView: View {
    @ObservedObject var model: AppModel
    @State private var confirmRemoval = false
    private let accent = Color(red: 0.48, green: 0.36, blue: 0.9)

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            HStack(spacing: 12) {
                Image(nsImage: NSApplication.shared.applicationIconImage)
                    .resizable().frame(width: 48, height: 48)
                VStack(alignment: .leading, spacing: 3) {
                    Text("Terminal TLDR").font(.system(size: 23, weight: .semibold, design: .rounded))
                    Text("Less tab hunting.").font(.system(size: 12)).foregroundStyle(.secondary)
                }
                Spacer()
            }
            VStack(spacing: 12) {
                Picker("Model", selection: Binding(
                    get: { model.health?.model ?? "gpt-6-luna" },
                    set: { value in Task { await model.configure(model: value, reasoning: model.health?.reasoning ?? "xhigh") } }
                )) {
                    ForEach(model.health?.models ?? [Choice(id: "gpt-6-luna", name: "GPT-6 Luna")]) { option in
                        Text(option.name).tag(option.id)
                    }
                }.accessibilityIdentifier("title-model")
                Picker("Reasoning", selection: Binding(
                    get: { model.health?.reasoning ?? "xhigh" },
                    set: { value in Task { await model.configure(model: model.health?.model ?? "gpt-6-luna", reasoning: value) } }
                )) {
                    ForEach(model.health?.reasoningOptions ?? [Choice(id: "xhigh", name: "Extra high")]) { option in
                        Text(option.name).tag(option.id)
                    }
                }.accessibilityIdentifier("title-reasoning")
            }
            .pickerStyle(.menu).controlSize(.large)
            .disabled(model.busy || model.health == nil)

            HStack(spacing: 8) {
                Circle().fill(model.fullyCovered ? Color.green : model.running ? Color.orange : Color.secondary)
                    .frame(width: 6, height: 6)
                Text(model.statusText).font(.system(size: 12)).foregroundStyle(.secondary)
                Spacer()
                if model.busy { ProgressView().controlSize(.mini) }
            }
            HStack(spacing: 10) {
                Button {
                    Task { await model.perform(model.installed ? "refresh" : "install") }
                } label: {
                    Label(model.installed ? "Refresh titles" : "Enable titles", systemImage: "arrow.clockwise")
                        .font(.system(size: 13, weight: .semibold))
                        .frame(maxWidth: .infinity).padding(.vertical, 4)
                }
                .buttonStyle(.borderedProminent).tint(accent).controlSize(.large)
                .disabled(model.busy || (model.installed && !model.running))
                .accessibilityIdentifier("refresh-titles")
                if model.installed {
                    Button(model.activeService ? "Pause" : "Resume") {
                        Task { await model.perform(model.activeService ? "pause" : "resume") }
                    }.controlSize(.large).disabled(model.busy)
                }
            }
            Divider()
            HStack {
                Toggle("Open at login", isOn: Binding(get: { model.loginEnabled }, set: model.setLogin))
                    .toggleStyle(.checkbox).font(.system(size: 12))
                    .accessibilityIdentifier("open-at-login")
                Spacer()
                Menu {
                    Text(model.health?.authentication ?? "Checking sign-in…")
                    Text("Reviews after 10 messages or 60 minutes with activity.")
                    Text("The background helper runs even with this app closed.")
                    Divider()
                    Button("Set up / repair") { Task { await model.perform("install") } }.disabled(model.busy)
                    Button("Login Items settings") { SMAppService.openSystemSettingsLoginItems() }
                    Button("Open local log") { model.showLog() }
                    Button("View source") { model.open("https://github.com/jacobsapps/Terminal-TLDR") }
                    Divider()
                    Button("Remove service and hooks…", role: .destructive) { confirmRemoval = true }
                } label: { Image(systemName: "ellipsis.circle") }
                    .menuStyle(.borderlessButton).fixedSize().help("More")
                    .accessibilityLabel("More")
            }
            if let message = model.message {
                Text(message).font(.system(size: 11))
                    .foregroundStyle(model.failed ? Color.orange : Color.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(26).frame(width: 380)
        .background {
            Color(nsColor: .windowBackgroundColor)
                .overlay(alignment: .topTrailing) {
                    LinearGradient(colors: [accent.opacity(0.07), .clear], startPoint: .topTrailing, endPoint: .bottomLeading)
                }
        }
        .task {
            if CommandLine.arguments.contains("--enable-login") { model.setLogin(true) }
            await model.refresh()
            while !Task.isCancelled {
                do { try await Task.sleep(nanoseconds: 5_000_000_000) } catch { break }
                if NSApp.isActive { await model.refresh() }
            }
        }
        .onReceive(NotificationCenter.default.publisher(for: NSApplication.didBecomeActiveNotification)) { _ in
            Task { await model.refresh() }
        }
        .confirmationDialog("Remove the background service and title hooks?", isPresented: $confirmRemoval) {
            Button("Remove", role: .destructive) { Task { await model.perform("uninstall") } }
        } message: { Text("Your terminals, logins and other hooks stay untouched.") }
    }
}

struct MenuContent: View {
    @ObservedObject var model: AppModel
    @Environment(\.openWindow) private var openWindow
    var body: some View {
        Button("Open Terminal TLDR") {
            openWindow(id: "main")
            NSApp.activate(ignoringOtherApps: true)
        }
        Divider()
        Button("Refresh titles") {
            Task { await model.perform("refresh") }
        }.disabled(!model.running || model.busy)
        Button(model.activeService ? "Pause title updates" : "Resume title updates") {
            Task { await model.perform(model.activeService ? "pause" : "resume") }
        }.disabled(!model.installed || model.busy)
        Button("Refresh status") { Task { await model.refresh() } }.disabled(model.busy)
        Divider()
        Button("Quit app") { NSApp.terminate(nil) }.keyboardShortcut("q")
        Text("The background helper keeps running.")
    }
}

@main
struct TerminalTLDRApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @StateObject private var model = AppModel()
    @Environment(\.openWindow) private var openWindow
    var body: some Scene {
        Window("Terminal TLDR", id: "main") {
            ContentView(model: model)
                .onAppear { delegate.presentWindow = { openWindow(id: "main") } }
                .onChange(of: model.busy) { _, busy in
                    if !busy { delegate.captureOwnWindowIfRequested() }
                }
                .onChange(of: model.checking) { _, checking in
                    if !checking { delegate.captureOwnWindowIfRequested() }
                }
        }
        .windowResizability(.contentSize)
        .windowStyle(.hiddenTitleBar)
        .defaultPosition(.center)
        .commands {
            CommandGroup(after: .newItem) {
                Button("Refresh status") { Task { await model.refresh() } }.keyboardShortcut("r")
            }
        }
        MenuBarExtra("Terminal TLDR", systemImage: "terminal") {
            MenuContent(model: model)
        }
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    var presentWindow: (() -> Void)?
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        presentWindow?()
        NSApp.activate(ignoringOtherApps: true)
        return true
    }
    // Developer verification of this app's actual content view. No screen
    // recording permission, desktop enumeration or other-app capture.
    func captureOwnWindowIfRequested() {
        let prefix = "--capture-own-window="
        guard let argument = CommandLine.arguments.first(where: { $0.hasPrefix(prefix) }) else { return }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) {
            guard let window = NSApp.windows.first(where: { $0.title == "Terminal TLDR" }),
                  let view = window.contentView?.superview,
                  let bitmap = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { return }
            view.cacheDisplay(in: view.bounds, to: bitmap)
            guard let data = bitmap.representation(using: .png, properties: [:]) else { return }
            try? data.write(to: URL(fileURLWithPath: String(argument.dropFirst(prefix.count))), options: .atomic)
        }
    }
}
