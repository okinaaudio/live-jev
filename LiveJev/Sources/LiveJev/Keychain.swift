import Foundation
import Security

enum Keychain {
    enum Account: String {
        case typeSafe = "TYPESAFE_API_KEY"
        case gemini = "GEMINI_API_KEY"
    }

    private static func query(_ account: Account) -> [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: "com.okinaaudio.livejev",
         kSecAttrAccount as String: account.rawValue]
    }

    struct Failure: Error {
        let status: OSStatus
    }

    static func read(_ account: Account = .typeSafe) throws -> String? {
        var request = query(account)
        request[kSecReturnData as String] = true
        request[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        let status = SecItemCopyMatching(request as CFDictionary, &result)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess else { throw Failure(status: status) }
        guard let data = result as? Data, let key = String(data: data, encoding: .utf8), !key.isEmpty else { return nil }
        return key
    }

    static func save(_ key: String, account: Account = .typeSafe) throws {
        let attributes = [kSecValueData as String: Data(key.utf8)]
        let accountQuery = query(account)
        var status = SecItemUpdate(accountQuery as CFDictionary, attributes as CFDictionary)
        if status == errSecItemNotFound {
            var item = accountQuery
            item.merge(attributes) { _, new in new }
            status = SecItemAdd(item as CFDictionary, nil)
        }
        guard status == errSecSuccess else { throw Failure(status: status) }
    }

    static func delete(_ account: Account = .typeSafe) throws {
        let status = SecItemDelete(query(account) as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else { throw Failure(status: status) }
    }
}
