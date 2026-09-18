// PaperBoatIntents.swift — the App Intent behind "Launch PaperBoat".
//
// The one Swift file in this port. It exists because App Intents has no
// Objective-C surface: the framework is Swift-only, and the alternative
// (a SiriKit INIntent with an .intentdefinition and an app-extension target)
// is a great deal of build machinery for a verb that means "open the app".
//
// It deliberately does NOT talk to the engine. `perform()` writes the same
// paperboat:// URL the deep-link path already handles into UserDefaults and
// lets `openAppWhenRun` bring the app up; PaperBoatIosShell.m consumes
// `paperboat_pending_link` at +load and again on every scene activation, so an
// intent run against a cold app and one run against a live app take the same
// route. That is the q2repro pattern (app/Sources/AppIntents.swift there), and
// the reason is that a Shortcut can fire at any lifecycle point — including
// while the engine is still extracting pm64.o2r — and the queue already knows
// how to wait.
//
// Compiled in by overlay 0010.

import AppIntents
import Foundation

@available(iOS 16.0, *)
struct LaunchPaperBoatIntent: AppIntent {
    static var title: LocalizedStringResource = "Launch PaperBoat"
    static var description = IntentDescription("Open PaperBoat (the Paper Mario 64 port).")

    // Foreground the app: the game is the point, so an intent that ran
    // silently in the background would do nothing a user could see.
    static var openAppWhenRun = true

    func perform() async throws -> some IntentResult {
        UserDefaults.standard.set("paperboat://launch", forKey: "paperboat_pending_link")
        UserDefaults.standard.synchronize()
        return .result()
    }
}

@available(iOS 16.0, *)
struct PaperBoatShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(intent: LaunchPaperBoatIntent(),
                    phrases: ["Launch \(.applicationName)",
                              "Play \(.applicationName)"],
                    shortTitle: "Launch PaperBoat",
                    systemImageName: "gamecontroller")
    }
}
