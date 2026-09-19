import AppKit
import QuartzCore

final class LiveJevPanel: NSPanel {
    var handleUndo: (() -> Bool)?
    var handleCancel: (() -> Void)?

    override func cancelOperation(_ sender: Any?) {
        handleCancel?()
    }

    override func performKeyEquivalent(with event: NSEvent) -> Bool {
        if event.modifierFlags.intersection(.deviceIndependentFlagsMask) == .command,
           event.charactersIgnoringModifiers == "z", handleUndo?() == true {
            return true
        }
        return super.performKeyEquivalent(with: event)
    }

    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { true }
}

@MainActor
final class WaveformView: NSView {
    var isConnected = false {
        didSet {
            if isConnected != oldValue { updateWaveform() }
        }
    }

    private let bars = (0..<4).map { _ in CALayer() }
    private let restingHeights: [CGFloat] = [7, 12, 15, 9]

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        for bar in bars {
            bar.cornerRadius = 1.25
            layer?.addSublayer(bar)
        }
        setAccessibilityElement(false)
        NSWorkspace.shared.notificationCenter.addObserver(
            self,
            selector: #selector(updateWaveform),
            name: NSWorkspace.accessibilityDisplayOptionsDidChangeNotification,
            object: nil
        )
        updateWaveform()
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    override var intrinsicContentSize: NSSize { NSSize(width: 18, height: 16) }

    override func viewWillMove(toWindow newWindow: NSWindow?) {
        if let window {
            NotificationCenter.default.removeObserver(
                self, name: NSWindow.didChangeOcclusionStateNotification, object: window
            )
        }
        super.viewWillMove(toWindow: newWindow)
    }

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        if let window {
            NotificationCenter.default.addObserver(
                self,
                selector: #selector(updateWaveform),
                name: NSWindow.didChangeOcclusionStateNotification,
                object: window
            )
        }
        updateWaveform()
    }

    override func viewDidHide() {
        super.viewDidHide()
        updateWaveform()
    }

    override func viewDidUnhide() {
        super.viewDidUnhide()
        updateWaveform()
    }

    override func viewDidChangeEffectiveAppearance() {
        super.viewDidChangeEffectiveAppearance()
        updateWaveform()
    }

    override func layout() {
        super.layout()
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        for (index, bar) in bars.enumerated() {
            bar.position = CGPoint(x: bounds.midX - 7.5 + CGFloat(index) * 5, y: bounds.midY)
        }
        CATransaction.commit()
    }

    @objc private func updateWaveform() {
        // Keep this static. Animation suggests voice input; a still bar indicates only connection status.
        let shouldAnimate = false
            && isConnected
            && window?.isVisible == true
            && window?.occlusionState.contains(.visible) == true
            && !isHiddenOrHasHiddenAncestor
            && !NSWorkspace.shared.accessibilityDisplayShouldReduceMotion
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        effectiveAppearance.performAsCurrentDrawingAppearance {
            let color = (NSColor.white.withAlphaComponent(isConnected ? 0.9 : 0.35)).cgColor
            for (index, bar) in bars.enumerated() {
                bar.backgroundColor = color
                bar.bounds = CGRect(x: 0, y: 0, width: 2.5, height: isConnected ? restingHeights[index] : 4)
                if shouldAnimate {
                    guard bar.animation(forKey: "wave") == nil else { continue }
                    let animation = CABasicAnimation(keyPath: "bounds.size.height")
                    animation.fromValue = 4
                    animation.toValue = restingHeights[index]
                    animation.duration = 0.45 + Double(index) * 0.07
                    animation.beginTime = bar.convertTime(CACurrentMediaTime(), from: nil) - Double(index) * 0.23
                    animation.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
                    animation.autoreverses = true
                    animation.repeatCount = .infinity
                    bar.add(animation, forKey: "wave")
                } else {
                    bar.removeAnimation(forKey: "wave")
                }
            }
        }
        CATransaction.commit()
    }
}

final class WrappingButtonRow: NSView {
    private let buttons: [NSButton]
    private let fallbackWidth: CGFloat
    private let horizontalSpacing: CGFloat = 6
    private let verticalSpacing: CGFloat = 6

    init(buttons: [NSButton], fallbackWidth: CGFloat) {
        self.buttons = buttons
        self.fallbackWidth = fallbackWidth
        super.init(frame: .zero)
        buttons.forEach(addSubview)
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    override var isFlipped: Bool { true }

    override var intrinsicContentSize: NSSize {
        NSSize(width: NSView.noIntrinsicMetric, height: layoutButtons(applyFrames: false))
    }

    override func setFrameSize(_ newSize: NSSize) {
        let widthChanged = frame.width != newSize.width
        super.setFrameSize(newSize)
        if widthChanged {
            invalidateIntrinsicContentSize()
        }
    }

    override func layout() {
        super.layout()
        _ = layoutButtons(applyFrames: true)
    }

    private func layoutButtons(applyFrames: Bool) -> CGFloat {
        let availableWidth = bounds.width > 0 ? bounds.width : fallbackWidth
        var x: CGFloat = 0
        var y: CGFloat = 0
        var lineHeight: CGFloat = 0

        for button in buttons {
            let fittingSize = button.fittingSize
            let width = min(fittingSize.width, availableWidth)
            if x > 0, x + width > availableWidth {
                x = 0
                y += lineHeight + verticalSpacing
                lineHeight = 0
            }
            if applyFrames {
                button.frame = NSRect(x: x, y: y, width: width, height: fittingSize.height)
            }
            x += width + horizontalSpacing
            lineHeight = max(lineHeight, fittingSize.height)
        }
        return buttons.isEmpty ? 0 : y + lineHeight
    }
}

private final class ResultsStackView: NSStackView {
    override var isFlipped: Bool { true }
}

class CapsuleButton: NSButton {
    override var intrinsicContentSize: NSSize {
        NSSize(width: min(516, super.intrinsicContentSize.width + 24), height: 30)
    }
}

final class ConfirmationButton: CapsuleButton {
    let confirmationID: String
    let confirmed: Bool

    init(title: String, confirmationID: String, confirmed: Bool, target: AnyObject?, action: Selector?) {
        self.confirmationID = confirmationID
        self.confirmed = confirmed
        super.init(frame: .zero)
        self.title = title
        self.target = target
        self.action = action
        self.bezelStyle = .rounded
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }
}

@MainActor
final class PanelController: NSWindowController, NSTextFieldDelegate, NSWindowDelegate {
    private let viewModel: ViewModel
    private let waveform = WaveformView(frame: .zero)
    private let inputField = NSTextField()
    private let undoButton = NSButton(title: "", target: nil, action: nil)
    private let resultsStack = ResultsStackView()
    private let resultsScrollView = NSScrollView()
    private let resultSurface = NSVisualEffectView()
    private let contentWidth: CGFloat = 516
    private let pillHeight: CGFloat = 52
    private var resultHeightConstraint: NSLayoutConstraint?
    private var historyIndex: Int?
    private var displayedItem: ResultItem?
    private var seenResults = Set<UUID>()
    private var autoHideTask: Task<Void, Never>?
    private var awaitingHiddenResult = false
    private var isHiding = false

    var isPanelVisible: Bool { window?.isVisible == true }

    init(viewModel: ViewModel) {
        self.viewModel = viewModel
        let panel = LiveJevPanel(
            contentRect: NSRect(x: 0, y: 0, width: 560, height: 52),
            styleMask: [.borderless, .fullSizeContentView],
            backing: .buffered,
            defer: false
        )
        super.init(window: panel)
        configurePanel(panel)
        buildContent(in: panel)
        viewModel.onChange = { [weak self] in self?.render() }
        render()
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    private var previousApp: NSRunningApplication?

    // Bring this app to the front when showing the window. Otherwise voice-input apps such as Aqua Voice
    // and Cmd-C/Cmd-V target the previously active app, so text never reaches this window.
    func showAndFocus() {
        guard let window else { return }
        let frontmost = NSWorkspace.shared.frontmostApplication
        if frontmost?.processIdentifier != ProcessInfo.processInfo.processIdentifier {
            previousApp = frontmost
        }
        cancelAutoHide()
        window.level = .floating
        NSApp.activate(ignoringOtherApps: true)
        window.orderFrontRegardless()
        window.makeKeyAndOrderFront(nil)
        window.makeFirstResponder(inputField)
        if let pending = viewModel.results.first(where: { $0.confirmationID != nil }) {
            displayedItem = pending
        } else if let latest = viewModel.results.first, latest.kind == .ask {
            displayedItem = latest
        }
        renderContent(animated: false)
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) {
            Log.shared.write("showAndFocus: active=\(NSApp.isActive) key=\(window.isKeyWindow) front=\(NSWorkspace.shared.frontmostApplication?.localizedName ?? "-")")
        }
    }

    // When the window closes, return focus to the previous app, usually Live.
    func hide() {
        hide(returnFocus: true)
    }

    private func hide(returnFocus: Bool) {
        guard !isHiding else { return }
        isHiding = true
        cancelAutoHide()
        window?.orderOut(nil)
        inputField.stringValue = ""
        historyIndex = nil
        displayedItem = nil
        renderContent(animated: false)
        if returnFocus, let previousApp, !previousApp.isTerminated {
            previousApp.activate()
        }
        previousApp = nil
        isHiding = false
    }

    func windowDidResignKey(_ notification: Notification) {
        guard isPanelVisible, !isHiding else { return }
        hide(returnFocus: false)
    }

    func windowDidMove(_ notification: Notification) {
        guard let window, isPanelVisible else { return }
        UserDefaults.standard.set([Double(window.frame.minX), Double(window.frame.maxY)],
                                  forKey: "LiveJevPillTopLeft")
    }

    func controlTextDidChange(_ notification: Notification) {
        cancelAutoHide()
    }

    private func cancelAutoHide() {
        autoHideTask?.cancel()
        autoHideTask = nil
    }

    func toggle() {
        if isPanelVisible {
            hide()
        } else {
            showAndFocus()
        }
    }

    func control(
        _ control: NSControl,
        textView: NSTextView,
        doCommandBy commandSelector: Selector
    ) -> Bool {
        switch commandSelector {
        case #selector(NSResponder.insertNewline(_:)):
            if viewModel.hasPendingConfirmation {
                cancelAutoHide()
                inputField.stringValue = ""
                historyIndex = nil
                viewModel.answerLatestConfirmation(true)
            } else {
                submitInput()
            }
        case #selector(NSResponder.cancelOperation(_:)):
            cancelOrHide()
        case #selector(NSResponder.moveUp(_:)):
            moveThroughHistory(by: 1)
        case #selector(NSResponder.moveDown(_:)):
            moveThroughHistory(by: -1)
        default:
            return false
        }
        return true
    }

    private func cancelOrHide() {
        if viewModel.hasPendingConfirmation {
            cancelAutoHide()
            viewModel.answerLatestConfirmation(false)
        } else {
            hide()
        }
    }

    private func configurePanel(_ panel: LiveJevPanel) {
        panel.delegate = self
        panel.handleCancel = { [weak self] in self?.cancelOrHide() }
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.isFloatingPanel = true
        panel.hidesOnDeactivate = false
        panel.isReleasedWhenClosed = false
        panel.isMovableByWindowBackground = true
        panel.backgroundColor = .clear
        panel.isOpaque = false
        panel.hasShadow = true
        panel.appearance = NSAppearance(named: .darkAqua)
        panel.handleUndo = { [weak self] in
            guard let self, self.inputField.stringValue.isEmpty,
                  self.displayedItem?.kind == .result else { return false }
            self.undoLast()
            return true
        }
        let screen = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
        var origin = NSPoint(x: screen.midX - 280, y: screen.maxY - screen.height * 0.22 - pillHeight)
        if let saved = UserDefaults.standard.array(forKey: "LiveJevPillTopLeft") as? [Double],
           saved.count == 2, saved.allSatisfy({ $0.isFinite }) {
            let candidate = NSRect(x: saved[0], y: saved[1] - pillHeight, width: 560, height: pillHeight)
            if NSScreen.screens.contains(where: { $0.visibleFrame.contains(candidate) }) {
                origin = candidate.origin
            }
        }
        panel.setFrameOrigin(origin)
    }

    private func prepareSurface(_ surface: NSVisualEffectView, radius: CGFloat) {
        surface.material = .hudWindow
        surface.blendingMode = .behindWindow
        surface.state = .active
        surface.wantsLayer = true
        surface.layer?.cornerRadius = radius
        surface.layer?.masksToBounds = true
        // The HUD material disappears against Live's gray UI, so add black to match the density of a voice-input bar.
        let tint = NSView()
        tint.wantsLayer = true
        tint.layer?.backgroundColor = NSColor.black.withAlphaComponent(0.58).cgColor
        tint.translatesAutoresizingMaskIntoConstraints = false
        surface.addSubview(tint, positioned: .below, relativeTo: nil)
        NSLayoutConstraint.activate([
            tint.leadingAnchor.constraint(equalTo: surface.leadingAnchor),
            tint.trailingAnchor.constraint(equalTo: surface.trailingAnchor),
            tint.topAnchor.constraint(equalTo: surface.topAnchor),
            tint.bottomAnchor.constraint(equalTo: surface.bottomAnchor)
        ])
    }

    private func buildContent(in panel: NSPanel) {
        let root = NSView()
        root.wantsLayer = true
        root.layer?.masksToBounds = true
        panel.contentView = root
        let pill = NSVisualEffectView()
        prepareSurface(pill, radius: pillHeight / 2)
        prepareSurface(resultSurface, radius: 22)

        inputField.font = .systemFont(ofSize: 18)
        inputField.textColor = .white
        inputField.isBordered = false
        inputField.isBezeled = false
        inputField.drawsBackground = false
        inputField.focusRingType = .none
        inputField.usesSingleLineMode = true
        inputField.cell?.isScrollable = true
        inputField.delegate = self

        undoButton.isBordered = false
        undoButton.contentTintColor = .secondaryLabelColor
        undoButton.imagePosition = .imageOnly
        undoButton.symbolConfiguration = .init(pointSize: 13, weight: .regular)
        undoButton.target = self
        undoButton.action = #selector(undoLast)

        resultsStack.orientation = .vertical
        resultsStack.alignment = .leading
        resultsStack.widthAnchor.constraint(equalToConstant: contentWidth).isActive = true
        resultsScrollView.drawsBackground = false
        resultsScrollView.borderType = .noBorder
        resultsScrollView.hasVerticalScroller = true
        resultsScrollView.autohidesScrollers = true
        resultsScrollView.scrollerStyle = .overlay
        resultsScrollView.documentView = resultsStack

        for view in [pill, resultSurface] {
            view.translatesAutoresizingMaskIntoConstraints = false
            root.addSubview(view)
        }
        for view in [waveform, inputField, undoButton] {
            view.translatesAutoresizingMaskIntoConstraints = false
            pill.addSubview(view)
        }
        resultHeightConstraint = resultSurface.heightAnchor.constraint(equalToConstant: 32)
        resultHeightConstraint?.isActive = true
        resultsScrollView.translatesAutoresizingMaskIntoConstraints = false
        resultSurface.addSubview(resultsScrollView)
        NSLayoutConstraint.activate([
            pill.topAnchor.constraint(equalTo: root.topAnchor),
            pill.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            pill.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            pill.heightAnchor.constraint(equalToConstant: pillHeight),
            waveform.leadingAnchor.constraint(equalTo: pill.leadingAnchor, constant: 22),
            waveform.centerYAnchor.constraint(equalTo: pill.centerYAnchor),
            waveform.widthAnchor.constraint(equalToConstant: 18),
            waveform.heightAnchor.constraint(equalToConstant: 16),
            inputField.leadingAnchor.constraint(equalTo: waveform.trailingAnchor, constant: 14),
            inputField.centerYAnchor.constraint(equalTo: pill.centerYAnchor),
            inputField.trailingAnchor.constraint(equalTo: undoButton.leadingAnchor, constant: -8),
            undoButton.trailingAnchor.constraint(equalTo: pill.trailingAnchor, constant: -16),
            undoButton.centerYAnchor.constraint(equalTo: pill.centerYAnchor),
            undoButton.widthAnchor.constraint(equalToConstant: 26),
            undoButton.heightAnchor.constraint(equalToConstant: 26),
            resultSurface.topAnchor.constraint(equalTo: pill.bottomAnchor, constant: 6),
            resultSurface.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            resultSurface.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            resultsScrollView.leadingAnchor.constraint(equalTo: resultSurface.leadingAnchor, constant: 22),
            resultsScrollView.trailingAnchor.constraint(equalTo: resultSurface.trailingAnchor, constant: -22),
            resultsScrollView.topAnchor.constraint(equalTo: resultSurface.topAnchor, constant: 16),
            resultsScrollView.bottomAnchor.constraint(equalTo: resultSurface.bottomAnchor, constant: -16)
        ])
        updateLocalizedText()
    }

    func updateLocalizedText() {
        inputField.setAccessibilityLabel(viewModel.text(.inputLabel))
        inputField.setAccessibilityHelp(viewModel.text(.inputHelp))
        undoButton.image = NSImage(systemSymbolName: "arrow.uturn.backward", accessibilityDescription: viewModel.text(.undo))
        undoButton.toolTip = viewModel.text(.undoTooltip)
        resultsStack.setAccessibilityLabel(viewModel.text(.latestResult))
        renderContent(animated: false)
    }

    private func styleSecondaryButton(_ button: NSButton) {
        button.bezelStyle = .roundRect
        button.isBordered = false
        button.wantsLayer = true
        button.layer?.backgroundColor = NSColor.white.withAlphaComponent(0.1).cgColor
        button.layer?.cornerRadius = 15
        button.contentTintColor = .white
        button.font = .systemFont(ofSize: 13)
        button.lineBreakMode = .byTruncatingTail
    }

    private var showDetails: Bool { UserDefaults.standard.bool(forKey: "showDetails") }

    @objc func toggleDetails(_ sender: NSMenuItem) {
        UserDefaults.standard.set(!showDetails, forKey: "showDetails")
        sender.state = showDetails ? .on : .off
        render()
    }

    private func render() {
        updateLocalizedTextWithoutRendering()
        waveform.isConnected = viewModel.isLiveConnected
        waveform.toolTip = viewModel.statusLine
        let latest = viewModel.results.first
        let isNew = latest.map { !seenResults.contains($0.id) } ?? false
        seenResults = Set(viewModel.results.map(\.id))
        // Removal of a confirmation must not reveal an older result.
        if let displayedItem, displayedItem.kind == .confirm,
           !viewModel.results.contains(where: { $0.id == displayedItem.id }) {
            self.displayedItem = nil
        }
        let isAwaitingAnswer = displayedItem?.kind == .ask || displayedItem?.kind == .confirm
        let isIncomingQuestion = latest?.kind == .ask || latest?.kind == .confirm
        if isNew, awaitingHiddenResult, latest?.kind == .result {
            // Success needs no message; it would interrupt users who reopen the panel to enter the next command.
            awaitingHiddenResult = false
        } else if isNew, !isPanelVisible, awaitingHiddenResult, let latest {
            awaitingHiddenResult = false
            if latest.kind != .result {
                showAndFocus()
                displayedItem = latest
                cancelAutoHide()
                if latest.kind != .ask, latest.kind != .confirm {
                    autoHideTask = Task { [weak self] in
                        do { try await Task.sleep(for: .seconds(3.0)) }
                        catch { return }
                        guard let self, self.isPanelVisible, !self.viewModel.hasPendingConfirmation,
                              self.inputField.stringValue.isEmpty else { return }
                        self.hide()
                    }
                }
            }
        } else if isNew, isPanelVisible, !isAwaitingAnswer || isIncomingQuestion {
            displayedItem = latest
            cancelAutoHide()
            if let latest, latest.kind != .ask, latest.kind != .confirm,
               !viewModel.hasPendingConfirmation, inputField.stringValue.isEmpty {
                // Flash success briefly and dismiss it as soon as possible. Keep notices and errors visible long enough to read.
                let delay = latest.kind == .result ? 0.15 : 3.0
                autoHideTask = Task { [weak self] in
                    do { try await Task.sleep(for: .seconds(delay)) }
                    catch { return }
                    guard let self, self.isPanelVisible, !self.viewModel.hasPendingConfirmation else { return }
                    self.hide()
                }
            }
        }
        renderContent(animated: isPanelVisible)
    }

    private func updateLocalizedTextWithoutRendering() {
        inputField.setAccessibilityLabel(viewModel.text(.inputLabel))
        inputField.setAccessibilityHelp(viewModel.text(.inputHelp))
        undoButton.image = NSImage(systemSymbolName: "arrow.uturn.backward", accessibilityDescription: viewModel.text(.undo))
        undoButton.toolTip = viewModel.text(.undoTooltip)
        resultsStack.setAccessibilityLabel(viewModel.text(.latestResult))
    }

    private func renderContent(animated: Bool) {
        resultsStack.arrangedSubviews.forEach {
            resultsStack.removeArrangedSubview($0)
            $0.removeFromSuperview()
        }
        if let displayedItem { addResultRow(makeResultRow(displayedItem, isLatest: true)) }
        undoButton.isHidden = displayedItem?.kind != .result
        resultSurface.isHidden = displayedItem == nil
        resultsStack.layoutSubtreeIfNeeded()
        let height = resultsStack.fittingSize.height
        resultsStack.setFrameSize(NSSize(width: contentWidth, height: height))
        guard let window else { return }
        let available = max(80, window.frame.maxY - (window.screen?.visibleFrame.minY ?? 0) - pillHeight - 18)
        let resultHeight = min(height + 32, available)
        resultHeightConstraint?.constant = resultHeight
        let targetHeight = pillHeight + (displayedItem == nil ? 0 : resultHeight + 6)
        var frame = window.frame
        frame.origin.y = frame.maxY - targetHeight
        frame.size.height = targetHeight
        if frame == window.frame { return }
        if animated, !NSWorkspace.shared.accessibilityDisplayShouldReduceMotion {
            NSAnimationContext.runAnimationGroup { context in
                context.duration = 0.18
                context.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
                window.animator().setFrame(frame, display: true)
            }
        } else {
            window.setFrame(frame, display: true)
        }
        resultsStack.scroll(.zero)
    }

    private func makeResultRow(_ item: ResultItem, isLatest: Bool) -> NSView {
        if let confirmationID = item.confirmationID {
            return makeConfirmationRow(item, confirmationID: confirmationID, isLatest: isLatest)
        }
        if !item.options.isEmpty {
            return makeAskRow(item, isLatest: isLatest)
        }

        let line = NSTextField(labelWithString: item.line)
        line.font = .systemFont(ofSize: isLatest ? 16 : 12, weight: isLatest ? .medium : .regular)
        line.textColor = isLatest && item.decision != nil ? .labelColor : .secondaryLabelColor
        line.toolTip = item.line
        line.lineBreakMode = .byTruncatingTail
        line.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        line.setContentHuggingPriority(.defaultLow, for: .horizontal)

        var views: [NSView] = []
        do {
            let symbol = NSImageView()
            symbol.image = NSImage(systemSymbolName: item.kind == .result ? "checkmark" : (item.kind == .error ? "exclamationmark.triangle" : "info.circle"), accessibilityDescription: nil)
            symbol.contentTintColor = .secondaryLabelColor
            symbol.symbolConfiguration = .init(pointSize: 12, weight: .regular)
            symbol.setAccessibilityElement(false)
            NSLayoutConstraint.activate([
                symbol.widthAnchor.constraint(equalToConstant: 14),
                symbol.heightAnchor.constraint(equalToConstant: 14)
            ])
            views.append(symbol)
        }
        views.append(line)
        if showDetails, let milliseconds = item.totalMilliseconds {
            let timing = NSTextField(labelWithString: "\(milliseconds) ms")
            timing.font = .monospacedDigitSystemFont(ofSize: 10, weight: .regular)
            timing.textColor = .secondaryLabelColor
            timing.alignment = .right
            timing.setContentCompressionResistancePriority(.required, for: .horizontal)
            timing.setContentHuggingPriority(.required, for: .horizontal)
            views.append(timing)
        }

        let resultLine = NSStackView(views: views)
        resultLine.orientation = .horizontal
        resultLine.alignment = .centerY
        resultLine.spacing = 5

        guard showDetails, let decision = item.decision else { return resultLine }

        let detail = NSTextField(labelWithString: decisionLine(decision))
        detail.font = .systemFont(ofSize: 11)
        detail.textColor = .secondaryLabelColor
        detail.toolTip = detail.stringValue
        detail.lineBreakMode = .byTruncatingTail
        detail.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        detail.setContentHuggingPriority(.defaultLow, for: .horizontal)

        var detailViews: [NSView] = [detail]
        if let milliseconds = item.llmMilliseconds, milliseconds > 0 {
            let llm = NSTextField(
                labelWithString: String(format: "+LLM %.1fs", Double(milliseconds) / 1_000)
            )
            llm.font = .monospacedDigitSystemFont(ofSize: 10, weight: .regular)
            llm.textColor = .secondaryLabelColor
            llm.alignment = .right
            llm.setContentCompressionResistancePriority(.required, for: .horizontal)
            llm.setContentHuggingPriority(.required, for: .horizontal)
            detailViews.append(llm)
        }

        let detailLine = NSStackView(views: detailViews)
        detailLine.orientation = .horizontal
        detailLine.alignment = .centerY
        detailLine.spacing = 5

        let row = NSStackView(views: [resultLine, detailLine])
        row.orientation = .vertical
        row.alignment = .leading
        row.spacing = 3
        resultLine.widthAnchor.constraint(equalTo: row.widthAnchor).isActive = true
        detailLine.widthAnchor.constraint(equalTo: row.widthAnchor).isActive = true
        return row
    }

    private func makeConfirmationRow(_ item: ResultItem, confirmationID: String, isLatest: Bool) -> NSView {
        let question = NSTextField(wrappingLabelWithString: item.line)
        question.font = .systemFont(ofSize: isLatest ? 16 : 12, weight: isLatest ? .medium : .regular)
        question.textColor = isLatest ? .labelColor : .secondaryLabelColor
        question.toolTip = item.line
        question.preferredMaxLayoutWidth = contentWidth
        question.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)

        let yes = ConfirmationButton(
            title: viewModel.text(.yes),
            confirmationID: confirmationID,
            confirmed: true,
            target: self,
            action: #selector(answerConfirmation(_:))
        )
        styleSecondaryButton(yes)
        yes.keyEquivalent = "\r"
        let cancel = ConfirmationButton(
            title: viewModel.text(.cancel),
            confirmationID: confirmationID,
            confirmed: false,
            target: self,
            action: #selector(answerConfirmation(_:))
        )
        styleSecondaryButton(cancel)
        cancel.keyEquivalent = "\u{1b}"

        let buttons = NSStackView(views: [yes, cancel])
        buttons.orientation = .horizontal
        buttons.spacing = 8
        let row = NSStackView(views: [question, buttons])
        row.orientation = .vertical
        row.alignment = .leading
        row.spacing = 10
        question.widthAnchor.constraint(equalTo: row.widthAnchor).isActive = true
        return row
    }

    private func decisionLine(_ decision: Decision) -> String {
        let action = decision.actionLabel ?? decision.action ?? "-"
        let track = decision.track ?? "-"
        let step = decision.stepLabel ?? "-"
        let actionConfidence = confidenceText(decision.conf?.action)
        let trackConfidence = confidenceText(decision.conf?.track)
        let confidence = viewModel.interfaceLanguage == .ja
            ? "（\(actionConfidence)・\(trackConfidence)）"
            : " (\(actionConfidence), \(trackConfidence))"
        var line = "Jev: \(action) / \(track) / \(step)\(confidence)"
        if let rewritten = decision.rewritten, !rewritten.isEmpty {
            line += " \(viewModel.text(.paraphrase)): \(rewritten.joined(separator: " / "))"
        }
        return line
    }

    private func confidenceText(_ confidence: Double?) -> String {
        guard let confidence else { return "-" }
        return String(format: "%.2f", confidence)
    }

    private func makeAskRow(_ item: ResultItem, isLatest: Bool) -> NSView {
        let question = NSTextField(wrappingLabelWithString: item.line)
        question.font = .systemFont(ofSize: isLatest ? 16 : 12, weight: isLatest ? .medium : .regular)
        question.textColor = isLatest ? .labelColor : .secondaryLabelColor
        question.toolTip = item.line
        question.preferredMaxLayoutWidth = contentWidth
        question.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)

        let buttons = item.options.map { option in
            let button = CapsuleButton(title: option, target: self, action: #selector(selectOption(_:)))
            styleSecondaryButton(button)
            button.toolTip = option
            return button
        }
        let buttonRow = WrappingButtonRow(buttons: buttons, fallbackWidth: contentWidth)
        let row = NSStackView(views: [question, buttonRow])
        row.orientation = .vertical
        row.alignment = .leading
        row.spacing = 10

        NSLayoutConstraint.activate([
            question.widthAnchor.constraint(equalTo: row.widthAnchor),
            buttonRow.widthAnchor.constraint(equalTo: row.widthAnchor)
        ])
        return row
    }

    private func addResultRow(_ row: NSView) {
        resultsStack.addArrangedSubview(row)
        row.widthAnchor.constraint(equalTo: resultsStack.widthAnchor).isActive = true
    }

    @objc private func undoLast() {
        guard displayedItem?.kind == .result else { return }
        cancelAutoHide()
        viewModel.undoLast()
        window?.makeFirstResponder(inputField)
    }

    @objc private func selectOption(_ sender: NSButton) {
        cancelAutoHide()
        inputField.stringValue = sender.title
        window?.makeFirstResponder(inputField)
        submitInput()
    }

    @objc private func answerConfirmation(_ sender: ConfirmationButton) {
        cancelAutoHide()
        inputField.stringValue = ""
        historyIndex = nil
        viewModel.answerConfirmation(id: sender.confirmationID, confirmed: sender.confirmed)
        window?.makeFirstResponder(inputField)
    }

    private func submitInput() {
        let text = inputField.stringValue
        guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        cancelAutoHide()
        inputField.stringValue = ""
        historyIndex = nil
        displayedItem = nil
        renderContent(animated: false)
        viewModel.submit(text)
        // Return to Live as soon as the command is submitted. Do not show successful results.
        // Reopen only for clarification, confirmation, notices, or errors in render below.
        awaitingHiddenResult = true
        hide()
    }

    private func moveThroughHistory(by delta: Int) {
        cancelAutoHide()
        let history = viewModel.history
        guard !history.isEmpty else { return }
        let current = historyIndex ?? -1
        let next = min(max(current + delta, -1), history.count - 1)
        historyIndex = next == -1 ? nil : next
        inputField.stringValue = next == -1 ? "" : history[next]
        inputField.currentEditor()?.selectedRange = NSRange(location: inputField.stringValue.utf16.count, length: 0)
    }
}
