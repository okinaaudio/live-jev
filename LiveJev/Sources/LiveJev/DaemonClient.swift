import Darwin
import Foundation

@MainActor
final class DaemonClient: @unchecked Sendable {
    var onMessage: ((DaemonMessage) -> Void)?
    var onConnectionChange: ((Bool, String?) -> Void)?
    var language: InterfaceLanguage = .ja

    private var process: Process?
    private var input: FileHandle?
    private var stdinPipe: Pipe?
    private var stdoutPipe: Pipe?
    private var stderrPipe: Pipe?
    private var stdoutTask: Task<Void, Never>?
    private var stderrTask: Task<Void, Never>?
    private var didRetry = false
    private var isStopping = false
    private var generation = UUID()

    private static let defaultDaemonPath: String = {
        let packageDirectory = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        return packageDirectory
            .appendingPathComponent("../daemon.py")
            .standardizedFileURL.path
    }()

    private static let configuredDaemonPath: String? = {
        let applicationSupport = FileManager.default.urls(
            for: .applicationSupportDirectory,
            in: .userDomainMask
        )[0]
        let configURL = applicationSupport.appendingPathComponent("LiveJev/config.json")
        guard
            let data = try? Data(contentsOf: configURL),
            let config = try? JSONDecoder().decode(Configuration.self, from: data),
            !config.daemonPath.isEmpty
        else {
            return nil
        }
        return config.daemonPath
    }()

    private struct Configuration: Decodable {
        let daemonPath: String

        private enum CodingKeys: String, CodingKey {
            case daemonPath = "daemon_path"
        }
    }

    func start() {
        isStopping = false
        didRetry = false
        launch(isRetry: false)
    }

    @discardableResult
    func send(_ request: DaemonRequest) -> Bool {
        guard let input else {
            onConnectionChange?(false, AppText.text(.daemonUnavailable, language: language))
            return false
        }
        do {
            var data = try JSONEncoder().encode(request)
            data.append(0x0A)
            try input.write(contentsOf: data)
            return true
        } catch {
            Log.shared.write("daemon stdin write failed: \(error.localizedDescription)")
            onConnectionChange?(false, AppText.text(.daemonSendFailed, language: language))
            return false
        }
    }

    func stop() {
        isStopping = true
        guard let process else {
            closePipes()
            return
        }

        if process.isRunning {
            send(.quit)
        }
        try? input?.close()
        input = nil

        Task.detached {
            try? await Task.sleep(for: .seconds(1))
            guard process.isRunning else { return }
            process.terminate()

            try? await Task.sleep(for: .milliseconds(500))
            guard process.isRunning else { return }
            kill(process.processIdentifier, SIGKILL)
        }
    }

    private func launch(isRetry: Bool) {
        let configuredPath = ProcessInfo.processInfo.environment["LIVE_JEV_DAEMON"]
            .flatMap { $0.isEmpty ? nil : $0 }
            ?? Self.configuredDaemonPath
            ?? Self.defaultDaemonPath
        let daemonURL = URL(fileURLWithPath: configuredPath).standardizedFileURL

        guard Self.isRegularAbsoluteFile(daemonURL) else {
            Log.shared.write("daemon not found at resolved path: \(daemonURL.path)")
            onConnectionChange?(false, AppText.text(.daemonMissing, language: language))
            scheduleRetryIfNeeded()
            return
        }

        let process = Process()
        let stdinPipe = Pipe()
        let stdoutPipe = Pipe()
        let stderrPipe = Pipe()
        let launchGeneration = UUID()

        generation = launchGeneration
        process.executableURL = URL(fileURLWithPath: "/opt/homebrew/bin/python3.13")
        process.arguments = [daemonURL.path]
        var environment = ProcessInfo.processInfo.environment
        environment["LIVE_JEV_LANG"] = language.rawValue
        process.environment = environment
        process.standardInput = stdinPipe
        process.standardOutput = stdoutPipe
        process.standardError = stderrPipe

        do {
            try process.run()
            self.process = process
            self.stdinPipe = stdinPipe
            self.stdoutPipe = stdoutPipe
            self.stderrPipe = stderrPipe
            input = stdinPipe.fileHandleForWriting
            send(.language(id: "language", value: language.rawValue))

            let stderrTask = Task.detached {
                Self.readStderr(from: stderrPipe.fileHandleForReading)
            }
            self.stderrTask = stderrTask
            stdoutTask = Task.detached { [weak self] in
                var buffer = Data()
                let output = stdoutPipe.fileHandleForReading

                while true {
                    let data = output.availableData
                    guard !data.isEmpty else { break }
                    buffer.append(data)
                    for lineData in Self.removeCompleteLines(from: &buffer) {
                        guard let message = Self.decodeMessage(lineData) else { continue }
                        await self?.deliver(message, process: process, generation: launchGeneration)
                    }
                }
                if !buffer.isEmpty, let message = Self.decodeMessage(buffer) {
                    await self?.deliver(message, process: process, generation: launchGeneration)
                }

                process.waitUntilExit()
                await stderrTask.value
                await self?.terminated(process, generation: launchGeneration)
            }

            Log.shared.write("daemon started at resolved path: \(daemonURL.path)")
            if isRetry {
                onConnectionChange?(false, AppText.text(.daemonRestarted, language: language))
            }
        } catch {
            Self.close(pipe: stdinPipe)
            Self.close(pipe: stdoutPipe)
            Self.close(pipe: stderrPipe)
            Log.shared.write("daemon launch failed: \(error.localizedDescription)")
            onConnectionChange?(false, AppText.text(.daemonLaunchFailed, language: language))
            scheduleRetryIfNeeded()
        }
    }

    private func deliver(_ message: DaemonMessage, process: Process, generation: UUID) {
        guard self.generation == generation, self.process === process else { return }
        onMessage?(message)
    }

    private func terminated(_ terminatedProcess: Process, generation: UUID) {
        guard self.generation == generation, process === terminatedProcess else { return }
        process = nil
        input = nil
        stdoutTask = nil
        stderrTask = nil
        closePipes()
        onConnectionChange?(false, isStopping ? nil : AppText.text(.daemonStopped, language: language))
        Log.shared.write("daemon exited with status \(terminatedProcess.terminationStatus)")
        if !isStopping {
            scheduleRetryIfNeeded()
        }
    }

    private func closePipes() {
        if let stdinPipe { Self.close(pipe: stdinPipe) }
        if let stdoutPipe { Self.close(pipe: stdoutPipe) }
        if let stderrPipe { Self.close(pipe: stderrPipe) }
        stdinPipe = nil
        stdoutPipe = nil
        stderrPipe = nil
    }

    private func scheduleRetryIfNeeded() {
        guard !didRetry, !isStopping else { return }
        didRetry = true
        Task { @MainActor [weak self] in
            try? await Task.sleep(for: .seconds(3))
            guard let self, !self.isStopping else { return }
            self.launch(isRetry: true)
        }
    }

    private static func isRegularAbsoluteFile(_ url: URL) -> Bool {
        guard (url.path as NSString).isAbsolutePath else { return false }
        guard let attributes = try? FileManager.default.attributesOfItem(atPath: url.path) else {
            return false
        }
        return attributes[.type] as? FileAttributeType == .typeRegular
    }

    private nonisolated static func removeCompleteLines(from buffer: inout Data) -> [Data] {
        var lines: [Data] = []
        while let newline = buffer.firstIndex(of: 0x0A) {
            let line = Data(buffer[..<newline])
            buffer.removeSubrange(...newline)
            if !line.isEmpty {
                lines.append(line)
            }
        }
        return lines
    }

    private nonisolated static func decodeMessage(_ data: Data) -> DaemonMessage? {
        do {
            return try JSONDecoder().decode(DaemonMessage.self, from: data)
        } catch {
            Log.shared.write("invalid daemon JSON line: \(error.localizedDescription)")
            return nil
        }
    }

    private nonisolated static func readStderr(from handle: FileHandle) {
        var buffer = Data()
        while true {
            let data = handle.availableData
            guard !data.isEmpty else { break }
            buffer.append(data)
            for lineData in removeCompleteLines(from: &buffer) {
                logStderr(lineData)
            }
        }
        if !buffer.isEmpty {
            logStderr(buffer)
        }
    }

    private nonisolated static func logStderr(_ data: Data) {
        guard let line = String(data: data, encoding: .utf8) else {
            Log.shared.write("daemon stderr contained invalid UTF-8")
            return
        }
        Log.shared.write("daemon stderr: \(line.trimmingCharacters(in: .whitespacesAndNewlines))")
    }

    private nonisolated static func close(pipe: Pipe) {
        pipe.fileHandleForReading.readabilityHandler = nil
        pipe.fileHandleForWriting.readabilityHandler = nil
        try? pipe.fileHandleForReading.close()
        try? pipe.fileHandleForWriting.close()
    }
}
