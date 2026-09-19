import Foundation

struct ResultItem {
    enum Kind { case result, ask, confirm, info, error }

    let id = UUID()
    let kind: Kind
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

    var interfaceLanguage: InterfaceLanguage { language.resolved }

    var hasPendingConfirmation: Bool {
        results.contains { $0.confirmationID != nil }
    }

    private let client = DaemonClient()
    private var nextID = 1

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

    func submit(_ text: String) {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        history.removeAll { $0 == trimmed }
        history.insert(trimmed, at: 0)
        history = Array(history.prefix(20))
        client.send(.text(id: makeID(), text: trimmed))
    }

    func undoLast() {
        client.send(.undo(id: makeID()))
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
            addResult(
                kind: .result,
                line: message.line,
                milliseconds: message.ms?.total,
                decision: message.decision,
                llmMilliseconds: message.ms?.llm
            )
        case let .ask(message):
            addResult(kind: .ask, line: message.line, options: message.options)
        case let .confirm(message):
            addResult(kind: .confirm, line: message.line, confirmationID: message.id)
        case let .info(message):
            addResult(kind: .info, line: message.line, milliseconds: message.ms?.total)
        case let .error(message):
            addResult(kind: .error, line: message.line, milliseconds: message.ms?.total)
        }
        onChange?()
    }

    private func addResult(
        kind: ResultItem.Kind,
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
