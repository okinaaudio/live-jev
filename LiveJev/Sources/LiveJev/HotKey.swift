import Carbon
import Foundation

final class HotKey: @unchecked Sendable {
    private var hotKeyRef: EventHotKeyRef?
    private var eventHandlerRef: EventHandlerRef?
    private let action: @MainActor @Sendable () -> Void

    init(action: @escaping @MainActor @Sendable () -> Void) {
        self.action = action
    }

    func register() throws {
        var eventType = EventTypeSpec(
            eventClass: OSType(kEventClassKeyboard),
            eventKind: UInt32(kEventHotKeyPressed)
        )
        let pointer = Unmanaged.passUnretained(self).toOpaque()
        let status = InstallEventHandler(
            GetApplicationEventTarget(),
            { _, _, userData in
                guard let userData else { return noErr }
                let hotKey = Unmanaged<HotKey>.fromOpaque(userData).takeUnretainedValue()
                Task { @MainActor in hotKey.action() }
                return noErr
            },
            1,
            &eventType,
            pointer,
            &eventHandlerRef
        )
        guard status == noErr else { throw HotKeyError.install(status) }

        let identifier = EventHotKeyID(signature: fourCharacterCode("LvSy"), id: 1)
        let registerStatus = RegisterEventHotKey(
            UInt32(kVK_Space),
            UInt32(cmdKey | shiftKey),  // ⌘⇧Space。⌃⌥Space は macOS の「入力ソースの切り替え」、⌥Space は Live の「選択範囲を再生」とぶつかる
            identifier,
            GetApplicationEventTarget(),
            0,
            &hotKeyRef
        )
        guard registerStatus == noErr else { throw HotKeyError.register(registerStatus) }
    }

    deinit {
        if let hotKeyRef { UnregisterEventHotKey(hotKeyRef) }
        if let eventHandlerRef { RemoveEventHandler(eventHandlerRef) }
    }

    private func fourCharacterCode(_ string: String) -> FourCharCode {
        string.utf8.reduce(0) { ($0 << 8) + FourCharCode($1) }
    }
}

private enum HotKeyError: LocalizedError {
    case install(OSStatus)
    case register(OSStatus)

    var errorDescription: String? {
        switch self {
        case let .install(status): "ホットキーの受信準備に失敗しました（\(status)）"
        case let .register(status): "ホットキーの登録に失敗しました（\(status)）"
        }
    }
}
