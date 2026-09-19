import Foundation

enum InterfaceLanguage: String {
    case ja, en
}

enum AppLanguage: String, CaseIterable {
    case auto, ja, en

    var resolved: InterfaceLanguage {
        switch self {
        case .ja: return .ja
        case .en: return .en
        case .auto:
            return Locale.preferredLanguages.first?.lowercased().hasPrefix("ja") == true ? .ja : .en
        }
    }
}

enum AppText {
    enum Key {
        case show, launchAtLogin, version, quit, showDetails, language, automatic, japanese, english
        case edit, undo, redo, cut, copy, paste, selectAll
        case daemonStarting, refreshing, checkingLive, daemonUnavailable, daemonSendFailed
        case daemonMissing, daemonRestarted, daemonLaunchFailed, daemonStopped
        case inputLabel, inputHelp, undoTooltip, latestResult, yes, cancel, paraphrase
    }

    static func text(_ key: Key, language: InterfaceLanguage) -> String {
        let pair: (ja: String, en: String)
        switch key {
        case .show: pair = ("呼び出す（⌘⇧Space）", "Show (⌘⇧Space)")
        case .launchAtLogin: pair = ("ログイン時に起動", "Launch at Login")
        case .version: pair = ("バージョン", "Version")
        case .quit: pair = ("終了", "Quit")
        case .showDetails: pair = ("詳細を表示", "Show Details")
        case .language: pair = ("言語 / Language", "言語 / Language")
        case .automatic: pair = ("自動", "Automatic")
        case .japanese: pair = ("日本語", "Japanese")
        case .english: pair = ("English", "English")
        case .edit: pair = ("編集", "Edit")
        case .undo: pair = ("取り消す", "Undo")
        case .redo: pair = ("やり直す", "Redo")
        case .cut: pair = ("カット", "Cut")
        case .copy: pair = ("コピー", "Copy")
        case .paste: pair = ("ペースト", "Paste")
        case .selectAll: pair = ("すべてを選択", "Select All")
        case .daemonStarting: pair = ("常駐を起動しています…", "Starting background service…")
        case .refreshing: pair = ("写しを取り直しています…", "Refreshing Live state…")
        case .checkingLive: pair = ("Liveの状態を確認しています…", "Checking Live…")
        case .daemonUnavailable: pair = ("常駐に接続できません", "Can't connect to the background service")
        case .daemonSendFailed: pair = ("常駐への送信に失敗しました", "Failed to send to the background service")
        case .daemonMissing: pair = ("常駐が見つかりません", "Background service not found")
        case .daemonRestarted: pair = ("常駐を再起動しました", "Background service restarted")
        case .daemonLaunchFailed: pair = ("常駐を起動できません", "Could not start the background service")
        case .daemonStopped: pair = ("常駐が終了しました", "Background service stopped")
        case .inputLabel: pair = ("Liveへの指示", "Command for Live")
        case .inputHelp: pair = ("Enterで送信。上下矢印で入力履歴。Escapeで閉じます。", "Press Return to send. Use arrow keys for history. Press Escape to close.")
        case .undoTooltip: pair = ("元に戻す（⌘Z）", "Undo (⌘Z)")
        case .latestResult: pair = ("最新の結果", "Latest result")
        case .yes: pair = ("はい", "Yes")
        case .cancel: pair = ("やめる", "Cancel")
        case .paraphrase: pair = ("言い換え", "Rewritten")
        }
        return language == .ja ? pair.ja : pair.en
    }
}

enum DaemonRequest: Encodable {
    case text(id: String, text: String)
    case refresh(id: String)
    case status(id: String)
    case confirm(id: String, confirmed: Bool)
    case undo(id: String)
    case language(id: String, value: String)
    case quit

    private enum CodingKeys: String, CodingKey {
        case id, text, cmd, confirm, value
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        switch self {
        case let .text(id, text):
            try container.encode(id, forKey: .id)
            try container.encode(text, forKey: .text)
        case let .refresh(id):
            try container.encode(id, forKey: .id)
            try container.encode("refresh", forKey: .cmd)
        case let .status(id):
            try container.encode(id, forKey: .id)
            try container.encode("status", forKey: .cmd)
        case let .confirm(id, confirmed):
            try container.encode(id, forKey: .id)
            try container.encode(confirmed, forKey: .confirm)
        case let .undo(id):
            try container.encode(id, forKey: .id)
            try container.encode("undo", forKey: .cmd)
        case let .language(id, value):
            try container.encode(id, forKey: .id)
            try container.encode("lang", forKey: .cmd)
            try container.encode(value, forKey: .value)
        case .quit:
            try container.encode("quit", forKey: .cmd)
        }
    }
}

struct Timing: Codable, Sendable {
    let jev: Int?
    let llm: Int?
    let bridge: Int?
    let total: Int?
}

struct DecisionConfidence: Codable, Sendable {
    let action: Double?
    let track: Double?
    let param: Double?
}

struct Decision: Codable, Sendable {
    let utterance: String?
    let rewritten: [String]?
    let action: String?
    let actionLabel: String?
    let track: String?
    let param: String?
    let stepLabel: String?
    let number: Double?
    let conf: DecisionConfidence?
    let before: String?
    let after: String?

    private enum CodingKeys: String, CodingKey {
        case utterance, rewritten, action, track, param, number, conf, before, after
        case actionLabel = "action_label"
        case stepLabel = "step_label"
    }
}

struct StatusMessage: Decodable, Sendable {
    let live: Bool
    let jev: Bool
    let tracks: Int
    let tempo: Double
    let line: String
}

struct LineMessage: Codable, Sendable {
    let id: String?
    let line: String
    let ms: Timing?
    let decision: Decision?
}

struct AskMessage: Decodable, Sendable {
    let id: String?
    let line: String
    let options: [String]
}

struct ConfirmMessage: Decodable, Sendable {
    let id: String
    let line: String
}

enum DaemonMessage: Decodable, Sendable {
    case status(StatusMessage)
    case result(LineMessage)
    case ask(AskMessage)
    case confirm(ConfirmMessage)
    case info(LineMessage)
    case error(LineMessage)

    private enum CodingKeys: String, CodingKey {
        case kind
    }

    private enum Kind: String, Decodable {
        case status, result, ask, confirm, info, error
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        switch try container.decode(Kind.self, forKey: .kind) {
        case .status:
            self = .status(try StatusMessage(from: decoder))
        case .result:
            self = .result(try LineMessage(from: decoder))
        case .ask:
            self = .ask(try AskMessage(from: decoder))
        case .confirm:
            self = .confirm(try ConfirmMessage(from: decoder))
        case .info:
            self = .info(try LineMessage(from: decoder))
        case .error:
            self = .error(try LineMessage(from: decoder))
        }
    }
}
