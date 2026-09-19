import Foundation

struct ResultItem {
    enum Kind { case result, ask, confirm, info, error }

    let id = UUID()
    let createdAt: TimeInterval = ProcessInfo.processInfo.systemUptime
    let kind: Kind
    let requestID: String?
    let line: String
    let options: [String]
    let confirmationID: String?
    let totalMilliseconds: Int?
    let decision: Decision?
    let llmMilliseconds: Int?
}

@MainActor
final class ViewModel {
    var onChange: (() -> Void)?

    private(set) var isLiveConnected = false
    private(set) var statusLine: String
    private(set) var results: [ResultItem] = []
    private(set) var history: [String] = []
    private(set) var language: AppLanguage
    private(set) var canUndoLastSuccess = false

    var interfaceLanguage: InterfaceLanguage { language.resolved }

    var hasPendingConfirmation: Bool {
        results.contains { $0.confirmationID != nil }
    }

    private let client = DaemonClient()
    private var nextID = 1
    private var pendingUndoID: String?

    init() {
        let stored = UserDefaults.standard.string(forKey: "language") ?? AppLanguage.auto.rawValue
        language = AppLanguage(rawValue: stored) ?? .auto
        statusLine = AppText.text(.daemonStarting, language: language.resolved)
        client.language = language.resolved
        client.onMessage = { [weak self] message in
            self?.receive(message)
        }
        client.onConnectionChange = { [weak self] connected, line in
            guard let self else { return }
            self.isLiveConnected = connected
            if let line {
                self.statusLine = line
            }
            self.onChange?()
        }
    }

    func start() {
        client.language = interfaceLanguage
        client.start()
    }

    func stop() {
        client.stop()
    }

    @discardableResult
    func submit(_ text: String, answering: String? = nil) -> String? {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return nil }
        history.removeAll { $0 == trimmed }
        history.insert(trimmed, at: 0)
        history = Array(history.prefix(20))
        let id = makeID()
        return client.send(.text(id: id, text: trimmed, answering: answering)) ? id : nil
    }

    func undoLast() {
        guard pendingUndoID == nil, canUndoLastSuccess else { return }
        let id = makeID()
        if client.send(.undo(id: id)) {
            pendingUndoID = id
            canUndoLastSuccess = false
            onChange?()
        }
    }

    func refresh() {
        statusLine = text(.refreshing)
        onChange?()
        client.send(.refresh(id: makeID()))
    }

    func checkLiveStatus() {
        statusLine = text(.checkingLive)
        onChange?()
        client.send(.status(id: makeID()))
    }

    func answerLatestConfirmation(_ confirmed: Bool) {
        guard let item = results.first(where: { $0.confirmationID != nil }),
              let id = item.confirmationID else { return }
        answerConfirmation(id: id, confirmed: confirmed)
    }

    func answerConfirmation(id: String, confirmed: Bool) {
        guard results.contains(where: { $0.confirmationID == id }) else { return }
        results.removeAll { $0.confirmationID == id }
        client.send(.confirm(id: id, confirmed: confirmed))
        onChange?()
    }

    func expireProposal(_ item: ResultItem) {
        guard results.contains(where: { $0.id == item.id }) else { return }
        results.removeAll { $0.id == item.id }
        client.send(.cancelPending(id: makeID(), target: item.requestID))
        onChange?()
    }

    func setLanguage(_ value: AppLanguage) {
        language = value
        UserDefaults.standard.set(value.rawValue, forKey: "language")
        client.language = value.resolved
        statusLine = text(.checkingLive)
        client.send(.language(id: makeID(), value: value.resolved.rawValue))
        onChange?()
    }

    func text(_ key: AppText.Key) -> String {
        AppText.text(key, language: interfaceLanguage)
    }

    private func makeID() -> String {
        defer { nextID += 1 }
        return String(nextID)
    }

    private func receive(_ message: DaemonMessage) {
        switch message {
        case let .status(status):
            isLiveConnected = status.live
            statusLine = status.line
        case let .result(message):
            if finishPendingUndo(id: message.id) {
                canUndoLastSuccess = false
            } else if pendingUndoID == nil {
                canUndoLastSuccess = true
            }
            addResult(
                kind: .result,
                requestID: message.id,
                line: message.line,
                milliseconds: message.ms?.total,
                decision: message.decision,
                llmMilliseconds: message.ms?.llm
            )
        case let .ask(message):
            addResult(kind: .ask, requestID: message.id, line: message.line, options: message.options)
        case let .confirm(message):
            addResult(kind: .confirm, requestID: message.id, line: message.line, confirmationID: message.id)
        case let .info(message):
            if finishPendingUndo(id: message.id) {
                canUndoLastSuccess = false
            }
            addResult(kind: .info, requestID: message.id, line: message.line, milliseconds: message.ms?.total)
        case let .error(message):
            _ = finishPendingUndo(id: message.id)
            addResult(kind: .error, requestID: message.id, line: message.line, milliseconds: message.ms?.total)
        }
        onChange?()
    }

    private func finishPendingUndo(id: String?) -> Bool {
        guard id == pendingUndoID else { return false }
        pendingUndoID = nil
        return true
    }

    private func addResult(
        kind: ResultItem.Kind,
        requestID: String? = nil,
        line: String,
        options: [String] = [],
        confirmationID: String? = nil,
        milliseconds: Int? = nil,
        decision: Decision? = nil,
        llmMilliseconds: Int? = nil
    ) {
        results.insert(
            ResultItem(
                kind: kind,
                requestID: requestID,
                line: line,
                options: options,
                confirmationID: confirmationID,
                totalMilliseconds: milliseconds,
                decision: decision,
                llmMilliseconds: llmMilliseconds
            ),
            at: 0
        )
        results = Array(results.prefix(5))
    }
}
