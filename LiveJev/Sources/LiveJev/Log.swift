import Foundation

final class Log: @unchecked Sendable {
    static let shared = Log()

    private let queue = DispatchQueue(label: "LiveJev.Log")
    private let url: URL
    private static let maximumBytes: UInt64 = 1_048_576

    private init() {
        let library = FileManager.default.urls(for: .libraryDirectory, in: .userDomainMask)[0]
        url = library.appendingPathComponent("Logs/LiveJev.log")
        truncateIfNeeded()
    }

    private func truncateIfNeeded() {
        let fileManager = FileManager.default
        guard
            let attributes = try? fileManager.attributesOfItem(atPath: url.path),
            let size = attributes[.size] as? UInt64,
            size > Self.maximumBytes,
            let handle = try? FileHandle(forReadingFrom: url)
        else {
            return
        }

        do {
            try handle.seek(toOffset: size - Self.maximumBytes)
            var tail = try handle.readToEnd() ?? Data()
            try handle.close()
            if let newline = tail.firstIndex(of: 0x0A), newline < tail.index(before: tail.endIndex) {
                tail.removeSubrange(...newline)
            }
            try tail.write(to: url, options: .atomic)
        } catch {
            try? handle.close()
        }
    }

    func write(_ message: String) {
        let safeMessage = redact(message)
        queue.async { [url] in
            let line = "\(ISO8601DateFormatter().string(from: Date())) \(safeMessage)\n"
            guard let data = line.data(using: .utf8) else { return }
            do {
                try FileManager.default.createDirectory(
                    at: url.deletingLastPathComponent(),
                    withIntermediateDirectories: true
                )
                if FileManager.default.fileExists(atPath: url.path) {
                    let handle = try FileHandle(forWritingTo: url)
                    try handle.seekToEnd()
                    try handle.write(contentsOf: data)
                    try handle.close()
                } else {
                    try data.write(to: url, options: .atomic)
                }
            } catch {
                // Logging must never stop the input window.
            }
        }
    }

    private func redact(_ message: String) -> String {
        var output = message
        let patterns = [
            #"(?i)Bearer\s+[^\s\"']+"#,
            #"(?i)((?:TYPESAFE|GEMINI)_API_KEY\s*[=:]\s*)[^\s\"']+"#
        ]
        for pattern in patterns {
            guard let regex = try? NSRegularExpression(pattern: pattern) else { continue }
            let range = NSRange(output.startIndex..<output.endIndex, in: output)
            output = regex.stringByReplacingMatches(
                in: output,
                range: range,
                withTemplate: pattern.contains("Bearer") ? "Bearer [REDACTED]" : "$1[REDACTED]"
            )
        }
        return output
    }
}
