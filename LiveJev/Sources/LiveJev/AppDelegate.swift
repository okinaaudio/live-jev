import AppKit
import ServiceManagement

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    private let viewModel = ViewModel()
    private var panelController: PanelController?
    private var hotKey: HotKey?
    private var statusItem: NSStatusItem?
    private var onboarding: Onboarding?
    private var loginItem: NSMenuItem?

    func applicationDidFinishLaunching(_ notification: Notification) {
        UserDefaults.standard.register(defaults: ["language": AppLanguage.auto.rawValue])
        let panelController = PanelController(viewModel: viewModel)
        self.panelController = panelController
        NSApp.mainMenu = makeMainMenu()
        configureMenuBar()

        let hotKey = HotKey { [weak panelController] in
            panelController?.toggle()
        }
        self.hotKey = hotKey
        do {
            try hotKey.register()
        } catch {
            Log.shared.write(error.localizedDescription)
        }

        viewModel.start()
        if !UserDefaults.standard.bool(forKey: "LiveJevSetupDone") { showSetup() }
        // Do not show the on-demand UI at launch, which would display the pill after every login. Open it with Cmd-Shift-Space or the menu.
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        panelController?.showAndFocus()
        return true
    }

    func applicationWillTerminate(_ notification: Notification) {
        viewModel.stop()
    }

    private func configureMenuBar() {
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        item.button?.image = NSImage(
            systemSymbolName: "waveform",
            accessibilityDescription: "Live Jev"
        )
        item.menu = makeStatusMenu()
        statusItem = item
    }

    private func makeStatusMenu() -> NSMenu {
        let menu = NSMenu()
        menu.addItem(withTitle: viewModel.text(.show), action: #selector(showPanel), keyEquivalent: "")
        menu.addItem(withTitle: viewModel.text(.setup), action: #selector(showSetup), keyEquivalent: "")
        let loginItem = menu.addItem(
            withTitle: viewModel.text(.launchAtLogin),
            action: #selector(toggleLoginItem),
            keyEquivalent: ""
        )
        self.loginItem = loginItem
        updateLoginItemState()
        menu.addItem(.separator())
        let languageItem = NSMenuItem(title: viewModel.text(.language), action: nil, keyEquivalent: "")
        let languageMenu = NSMenu(title: viewModel.text(.language))
        for (index, value) in AppLanguage.allCases.enumerated() {
            let key: AppText.Key = value == .auto ? .automatic : value == .ja ? .japanese : .english
            let item = languageMenu.addItem(withTitle: viewModel.text(key), action: #selector(changeLanguage(_:)), keyEquivalent: "")
            item.tag = index
            item.target = self
            item.state = value == viewModel.language ? .on : .off
        }
        languageItem.submenu = languageMenu
        menu.addItem(languageItem)
        let detailsItem = menu.addItem(withTitle: viewModel.text(.showDetails), action: #selector(PanelController.toggleDetails(_:)), keyEquivalent: "")
        detailsItem.target = panelController
        detailsItem.state = UserDefaults.standard.bool(forKey: "showDetails") ? .on : .off
        menu.addItem(.separator())
        let version = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "?"
        let versionItem = menu.addItem(withTitle: "\(viewModel.text(.version)) \(version)", action: nil, keyEquivalent: "")
        versionItem.isEnabled = false
        menu.addItem(.separator())
        menu.addItem(withTitle: viewModel.text(.quit), action: #selector(quit), keyEquivalent: "q")
        menu.items.forEach { $0.target = self }
        detailsItem.target = panelController
        return menu
    }

    // Even a menu-bar-only app needs an Edit menu for Cmd-C/V/A/Z to reach its window.
    private func makeMainMenu() -> NSMenu {
        let main = NSMenu()
        let appItem = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "Live Jev \(viewModel.text(.quit))", action: #selector(quit), keyEquivalent: "q")
        appItem.submenu = appMenu
        main.addItem(appItem)
        let editItem = NSMenuItem()
        let edit = NSMenu(title: viewModel.text(.edit))
        edit.addItem(withTitle: viewModel.text(.undo), action: Selector(("undo:")), keyEquivalent: "z")
        let redo = edit.addItem(withTitle: viewModel.text(.redo), action: Selector(("redo:")), keyEquivalent: "z")
        redo.keyEquivalentModifierMask = [.command, .shift]
        edit.addItem(.separator())
        edit.addItem(withTitle: viewModel.text(.cut), action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: viewModel.text(.copy), action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: viewModel.text(.paste), action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: viewModel.text(.selectAll), action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = edit
        main.addItem(editItem)
        return main
    }

    @objc private func showSetup() {
        if onboarding == nil { onboarding = Onboarding(viewModel: viewModel) }
        onboarding?.show()
    }

    @objc private func showPanel() {
        panelController?.showAndFocus()
    }

    @objc private func toggleLoginItem() {
        do {
            if SMAppService.mainApp.status == .enabled {
                try SMAppService.mainApp.unregister()
            } else {
                try SMAppService.mainApp.register()
            }
        } catch {
            Log.shared.write("login item update failed: \(error.localizedDescription)")
        }
        updateLoginItemState()
    }

    @objc private func changeLanguage(_ sender: NSMenuItem) {
        guard AppLanguage.allCases.indices.contains(sender.tag) else { return }
        viewModel.setLanguage(AppLanguage.allCases[sender.tag])
        NSApp.mainMenu = makeMainMenu()
        statusItem?.menu = makeStatusMenu()
        panelController?.updateLocalizedText()
        onboarding?.updateLocalizedText()
    }

    private func updateLoginItemState() {
        loginItem?.state = SMAppService.mainApp.status == .enabled ? .on : .off
    }

    @objc private func quit() {
        NSApplication.shared.terminate(nil)
    }
}
