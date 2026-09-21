import AppKit
import Darwin

@MainActor
final class Onboarding: NSObject, NSWindowDelegate {
    private enum State { case pending, ok, problem }
    private let viewModel: ViewModel
    private let window: NSWindow
    private var timer: Timer?
    private var states: [State] = [.pending, .pending, .pending, .pending]
    private var glyphs: [NSImageView] = []
    private var titles: [NSTextField] = []
    private var helps: [NSTextField] = []
    private var buttons: [(NSButton, AppText.Key)] = []
    private let keyField = NSSecureTextField()
    private let geminiKeyField = NSSecureTextField()
    private let geminiTitle = NSTextField(labelWithString: "")
    private let geminiHelp = NSTextField(wrappingLabelWithString: "")
    private let geminiDisclosure = NSTextField(wrappingLabelWithString: "")
    private let geminiStatus = NSTextField(labelWithString: "")
    private var installButton: NSButton!
    private var removeButton: NSButton!
    private var geminiRemoveButton: NSButton!
    private var doneButton: NSButton!
    private var hasKey = false
    private var keyFailed = false
    private var hasGeminiKey = false
    private var geminiKeyFailed = false
    private var liveStatus: StatusMessage?
    private var connectionProblem: String?
    private var installProblem = false
    private var installedThisSession = false
    private var runningScriptOutdated = false
    private var tried = false
    private var checking = false
    private var tryLine: String?
    private static let GEMINIKeyURL = URL(string: "https://aistudio.google.com/apikey")!

    init(viewModel: ViewModel) {
        self.viewModel = viewModel
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 520, height: 650),
                          styleMask: [.titled, .closable, .miniaturizable], backing: .buffered, defer: false)
        super.init()
        window.delegate = self
        window.isReleasedWhenClosed = false
        build()
        viewModel.onSetupMessage = { [weak self] message in self?.receive(message) }
        viewModel.onSetupConnection = { [weak self] line in
            guard let self, self.window.isVisible else { return }
            self.liveStatus = nil
            self.connectionProblem = line
            self.checking = false
            if self.tried { self.tryLine = line }
            self.refresh()
        }
        updateLocalizedText()
    }

    private func text(_ key: AppText.Key) -> String { viewModel.text(key) }

    func show() {
        readKey()
        readGeminiKey()
        liveStatus = nil
        connectionProblem = nil
        installedThisSession = false
        runningScriptOutdated = false
        tried = false
        checking = false
        tryLine = nil
        refresh()
        window.center()
        NSApp.setActivationPolicy(.regular)
        if window.isMiniaturized { window.deminiaturize(nil) }
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        startPolling()
    }

    func windowWillClose(_ notification: Notification) {
        timer?.invalidate()
        timer = nil
        keyField.stringValue = ""
        geminiKeyField.stringValue = ""
        NSApp.setActivationPolicy(.accessory)
    }

    func windowDidMiniaturize(_ notification: Notification) {
        timer?.invalidate()
        timer = nil
    }

    func windowDidDeminiaturize(_ notification: Notification) { startPolling() }

    private func startPolling() {
        timer?.invalidate()
        viewModel.pollSetupStatus()
        timer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in
            Task { @MainActor [weak self] in
                guard let self, self.window.isVisible, !self.window.isMiniaturized else { return }
                self.viewModel.pollSetupStatus()
            }
        }
    }

    private func build() {
        let material = NSVisualEffectView()
        material.material = .windowBackground
        material.blendingMode = .behindWindow
        window.contentView = material
        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 24
        stack.translatesAutoresizingMaskIntoConstraints = false
        let scrollView = NSScrollView()
        scrollView.hasVerticalScroller = true
        scrollView.drawsBackground = false
        scrollView.translatesAutoresizingMaskIntoConstraints = false
        let document = NSView()
        document.translatesAutoresizingMaskIntoConstraints = false
        scrollView.documentView = document
        material.addSubview(scrollView)
        document.addSubview(stack)
        NSLayoutConstraint.activate([
            scrollView.leadingAnchor.constraint(equalTo: material.leadingAnchor),
            scrollView.trailingAnchor.constraint(equalTo: material.trailingAnchor),
            scrollView.topAnchor.constraint(equalTo: material.topAnchor),
            scrollView.bottomAnchor.constraint(equalTo: material.bottomAnchor),
            document.widthAnchor.constraint(equalTo: scrollView.contentView.widthAnchor),
            stack.leadingAnchor.constraint(equalTo: document.leadingAnchor, constant: 28),
            stack.trailingAnchor.constraint(equalTo: document.trailingAnchor, constant: -28),
            stack.topAnchor.constraint(equalTo: document.topAnchor, constant: 28),
            stack.bottomAnchor.constraint(equalTo: document.bottomAnchor, constant: -24)
        ])
        for index in 0..<4 {
            let glyph = NSImageView()
            glyph.setAccessibilityElement(true)
            glyph.widthAnchor.constraint(equalToConstant: 20).isActive = true
            glyph.heightAnchor.constraint(equalToConstant: 20).isActive = true
            glyphs.append(glyph)
            let title = NSTextField(labelWithString: "")
            title.font = .systemFont(ofSize: 14, weight: .semibold)
            titles.append(title)
            let help = NSTextField(wrappingLabelWithString: "")
            help.font = .systemFont(ofSize: 12)
            help.textColor = .secondaryLabelColor
            helps.append(help)
            let body = NSStackView(views: [title, help])
            body.orientation = .vertical
            body.alignment = .leading
            body.spacing = 8
            let row = NSStackView(views: [glyph, body])
            row.alignment = .top
            row.spacing = 12
            stack.addArrangedSubview(row)
            row.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
            help.widthAnchor.constraint(equalTo: body.widthAnchor).isActive = true
            switch index {
            case 0:
                installButton = button(.install, #selector(installScript))
                body.addArrangedSubview(NSStackView(views: [installButton, button(.chooseLibrary, #selector(chooseLibrary), secondary: true)]))
            case 2:
                keyField.font = .systemFont(ofSize: 13)
                keyField.widthAnchor.constraint(equalToConstant: 290).isActive = true
                body.addArrangedSubview(NSStackView(views: [keyField, button(.saveKey, #selector(saveKey))]))
                removeButton = button(.removeKey, #selector(removeKey), secondary: true)
                body.addArrangedSubview(NSStackView(views: [button(.getKey, #selector(getKey), secondary: true), removeButton]))
            case 3:
                body.addArrangedSubview(button(.tryIt, #selector(tryConnection)))
            default: break
            }
        }
        geminiTitle.font = .systemFont(ofSize: 14, weight: .semibold)
        geminiHelp.font = .systemFont(ofSize: 12)
        geminiHelp.textColor = .secondaryLabelColor
        geminiDisclosure.font = .systemFont(ofSize: 12)
        geminiDisclosure.textColor = .secondaryLabelColor
        geminiStatus.font = .systemFont(ofSize: 12)
        geminiStatus.textColor = .secondaryLabelColor
        geminiKeyField.font = .systemFont(ofSize: 13)
        geminiKeyField.widthAnchor.constraint(equalToConstant: 290).isActive = true
        let geminiBody = NSStackView(views: [geminiTitle, geminiHelp, geminiDisclosure])
        geminiBody.orientation = .vertical
        geminiBody.alignment = .leading
        geminiBody.spacing = 8
        geminiBody.addArrangedSubview(NSStackView(views: [geminiKeyField, button(.saveKey, #selector(saveGeminiKey))]))
        geminiRemoveButton = button(.removeKey, #selector(removeGeminiKey), secondary: true)
        geminiBody.addArrangedSubview(NSStackView(views: [button(.getGeminiKey, #selector(getGeminiKey), secondary: true), geminiRemoveButton]))
        geminiBody.addArrangedSubview(geminiStatus)
        stack.insertArrangedSubview(geminiBody, at: 3)
        geminiBody.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
        let footer = NSStackView()
        let spacer = NSView()
        doneButton = button(.finishLater, #selector(finish))
        doneButton.keyEquivalent = "\r"
        footer.addArrangedSubview(spacer)
        footer.addArrangedSubview(doneButton)
        stack.addArrangedSubview(footer)
        footer.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
    }

    private func button(_ key: AppText.Key, _ action: Selector, secondary: Bool = false) -> NSButton {
        let button = NSButton(title: text(key), target: self, action: action)
        button.bezelStyle = .rounded
        button.isBordered = !secondary
        button.contentTintColor = .labelColor
        button.setAccessibilityLabel(text(key))
        buttons.append((button, key))
        return button
    }

    func updateLocalizedText() {
        window.title = text(.setupTitle)
        for (button, key) in buttons {
            button.title = text(key)
            button.setAccessibilityLabel(text(key))
        }
        for (index, key) in [AppText.Key.installScript, .selectLive, .addKey, .tryIt].enumerated() {
            titles[index].stringValue = text(key)
        }
        keyField.setAccessibilityLabel(text(.addKey))
        keyField.setAccessibilityHelp(text(.keyHelp))
        geminiTitle.stringValue = text(.geminiKey)
        geminiHelp.stringValue = text(.geminiHelp)
        geminiDisclosure.stringValue = text(.geminiDisclosure)
        geminiKeyField.setAccessibilityLabel(text(.geminiKey))
        geminiKeyField.setAccessibilityHelp(text(.geminiHelp))
        refresh()
    }

    private var library: URL {
        if let path = UserDefaults.standard.string(forKey: "LiveJevUserLibrary") {
            return URL(fileURLWithPath: path, isDirectory: true)
        }
        return FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Music/Ableton/User Library")
    }

    private var destination: URL { library.appendingPathComponent("Remote Scripts/LiveJev") }

    private func version(in folder: URL) -> String? {
        guard let content = try? String(contentsOf: folder.appendingPathComponent("LiveJev.py"), encoding: .utf8),
              let regex = try? NSRegularExpression(pattern: #"["']version["']\s*:\s*["']([^"']+)["']"#),
              let match = regex.firstMatch(in: content, range: NSRange(content.startIndex..., in: content)),
              let range = Range(match.range(at: 1), in: content) else { return nil }
        return String(content[range])
    }

    private func readKey() {
        do {
            hasKey = try Keychain.read() != nil
            keyFailed = false
        } catch { hasKey = false; keyFailed = true }
    }

    private func readGeminiKey() {
        do {
            hasGeminiKey = try Keychain.read(.gemini) != nil
            geminiKeyFailed = false
        } catch {
            hasGeminiKey = false
            geminiKeyFailed = true
        }
    }

    private func refresh() {
        let sourceVersion = version(in: DaemonClient.remoteScriptSource)
        let installedVersion = version(in: destination)
        let exists = FileManager.default.fileExists(atPath: destination.path)
        let older = installedVersion.map { $0.compare(sourceVersion ?? "", options: .numeric) == .orderedAscending } ?? false
        let diskMatches = sourceVersion != nil && sourceVersion == installedVersion
        let needsRestart = installedThisSession || (diskMatches && runningScriptOutdated)
        states[0] = needsRestart ? .problem : diskMatches ? .ok : (exists || sourceVersion == nil ? .problem : .pending)
        helps[0].stringValue = text(installProblem ? .installFailed : installedThisSession ? .installedRestart : diskMatches && runningScriptOutdated ? .runningScriptOutdated : sourceVersion == nil ? .scriptProblem : diskMatches ? .installed : exists ? (installedVersion == nil ? .scriptProblem : older ? .olderScript : .differentScript) : .missingScript)
        if installProblem { states[0] = .problem }
        installButton.title = text(exists ? .update : .install)
        installButton.setAccessibilityLabel(installButton.title)
        installButton.isEnabled = sourceVersion != nil && !diskMatches
        installButton.toolTip = destination.path
        states[1] = liveStatus?.live == true ? .ok : (liveStatus != nil || connectionProblem != nil ? .problem : .pending)
        helps[1].stringValue = text(.selectLiveHelp)
        states[2] = hasKey || liveStatus?.jev == true ? .ok : (keyFailed ? .problem : .pending)
        helps[2].stringValue = text(keyFailed ? .keyProblem : !hasKey && liveStatus?.jev == true ? .shellKey : .keyHelp)
        removeButton.isHidden = !hasKey
        geminiRemoveButton.isHidden = !hasGeminiKey
        geminiStatus.stringValue = text(geminiKeyFailed ? .geminiSaveFailed : hasGeminiKey ? .geminiSaved : .geminiEmpty)
        states[3] = tried && !checking ? (liveStatus?.live == true && connectionProblem == nil ? .ok : .problem) : .pending
        helps[3].stringValue = tryLine ?? text(.tryHelp)
        for index in 0..<4 {
            let state = states[index]
            let symbol = state == .ok ? "checkmark.circle.fill" : state == .problem ? "exclamationmark.circle" : "circle.dotted"
            let label = text(state == .ok ? .ready : state == .problem ? .problem : .pending)
            glyphs[index].image = NSImage(systemSymbolName: symbol, accessibilityDescription: label)
            glyphs[index].contentTintColor = state == .pending ? .tertiaryLabelColor : .labelColor
            glyphs[index].setAccessibilityLabel("\(titles[index].stringValue): \(label)")
        }
        doneButton.title = text(states.prefix(3).allSatisfy { $0 == .ok } ? .done : .finishLater)
        doneButton.setAccessibilityLabel(doneButton.title)
    }

    private func receive(_ message: DaemonMessage) {
        guard window.isVisible else { return }
        switch message {
        case .status(let status):
            checking = false
            liveStatus = status
            connectionProblem = nil
            runningScriptOutdated = status.live && status.line.contains("Remote Script") && (status.line.contains("古い版") || status.line.localizedCaseInsensitiveContains("out of date"))
            if tried { tryLine = status.line }
        case .error(let error) where error.id?.hasPrefix("setup-") == true:
            checking = false
            connectionProblem = error.line
            liveStatus = nil
            if tried { tryLine = error.line }
        default: return
        }
        refresh()
    }

    @objc private func installScript() {
        let manager = FileManager.default
        let parent = destination.deletingLastPathComponent()
        let temporary = parent.appendingPathComponent(".LiveJev-\(UUID().uuidString)")
        do {
            try manager.createDirectory(at: parent, withIntermediateDirectories: true)
            defer { try? manager.removeItem(at: temporary) }
            try manager.copyItem(at: DaemonClient.remoteScriptSource, to: temporary)
            guard version(in: temporary) != nil else { throw CocoaError(.fileReadCorruptFile) }
            if manager.fileExists(atPath: destination.path) {
                // Exchange siblings so Live never sees a partially copied script folder.
                guard renamex_np(temporary.path, destination.path, UInt32(RENAME_SWAP)) == 0 else {
                    throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
                }
            } else {
                try manager.moveItem(at: temporary, to: destination)
            }
            installProblem = false
            installedThisSession = true
        } catch { installProblem = true }
        refresh()
    }

    @objc private func chooseLibrary() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.directoryURL = library
        panel.prompt = text(.chooseLibrary)
        panel.beginSheetModal(for: window) { [weak self] response in
            guard response == .OK, let url = panel.url, let self else { return }
            UserDefaults.standard.set(url.path, forKey: "LiveJevUserLibrary")
            self.installProblem = false
            self.refresh()
        }
    }

    @objc private func saveKey() {
        let key = keyField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !key.isEmpty else { return }
        do {
            try Keychain.save(key)
            keyField.stringValue = ""
            keyChanged()
        } catch { keyFailed = true; refresh() }
    }

    @objc private func removeKey() {
        do {
            try Keychain.delete()
            keyField.stringValue = ""
            keyChanged()
        } catch { keyFailed = true; refresh() }
    }

    @objc private func saveGeminiKey() {
        let key = geminiKeyField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !key.isEmpty else { return }
        do {
            try Keychain.save(key, account: .gemini)
            guard try Keychain.read(.gemini) == key else { throw Keychain.Failure(status: errSecDecode) }
            geminiKeyField.stringValue = ""
            geminiKeyChanged()
        } catch {
            geminiKeyFailed = true
            refresh()
        }
    }

    @objc private func removeGeminiKey() {
        do {
            try Keychain.delete(.gemini)
            geminiKeyField.stringValue = ""
            geminiKeyChanged()
        } catch {
            geminiKeyFailed = true
            refresh()
        }
    }

    private func geminiKeyChanged() {
        readGeminiKey()
        refresh()
        viewModel.restartDaemon()
    }

    private func keyChanged() {
        readKey()
        liveStatus = nil
        connectionProblem = nil
        tryLine = nil
        tried = false
        checking = false
        refresh()
        viewModel.restartDaemon()
    }

    @objc private func getKey() {
        NSWorkspace.shared.open(URL(string: "https://typesafe.ai")!)
    }

    @objc private func getGeminiKey() {
        NSWorkspace.shared.open(Self.GEMINIKeyURL)
    }

    @objc private func tryConnection() {
        tried = true
        checking = true
        tryLine = text(.checkingLive)
        refresh()
        viewModel.pollSetupStatus()
    }

    @objc private func finish() {
        UserDefaults.standard.set(true, forKey: "LiveJevSetupDone")
        window.close()
    }
}
