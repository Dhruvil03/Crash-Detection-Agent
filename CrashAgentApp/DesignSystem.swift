// DesignSystem.swift
//
// A small, intentional design token system for the app -- the goal is
// to replace ad-hoc `.font(.caption)` / `Color.blue.opacity(0.08)` calls
// scattered through the views with a named, consistent vocabulary. This
// is what makes the UI read as "designed" rather than "default SwiftUI."
//
// Palette rationale: this is a forensic/diagnostic tool, not a chat app,
// so the personality is precision and calm authority rather than
// playfulness. One accent (a deep indigo) carries all primary actions
// and active states; everything else is near-monochrome until a step's
// *meaning* calls for color (error red, success green, pending amber).
// Built on macOS semantic colors so light/dark mode and accessibility
// settings are respected automatically, with the custom accents layered
// on top only where semantic colors don't carry enough distinction.

import SwiftUI
import AppKit

extension Color {
    /// SwiftUI has no built-in `Color(light:dark:)` initializer -- this
    /// wraps a dynamic NSColor that resolves per-appearance, so values
    /// defined this way automatically adapt when the system switches
    /// between Light and Dark Mode (including Auto).
    init(light: Color, dark: Color) {
        self.init(nsColor: NSColor(name: nil) { appearance in
            let isDark = appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
            return NSColor(isDark ? dark : light)
        })
    }
}

enum DS {
    // MARK: - Color

    enum Color {
        /// Primary accent: actions, active timeline nodes, links.
        /// A deep indigo -- distinct from systemBlue so it doesn't read
        /// as "default macOS blue," while staying calm and precise.
        static let accent = SwiftUI.Color(red: 0.357, green: 0.373, blue: 0.890)

        /// Investigation in progress -- the "thinking" / tool-call states.
        static let inProgress = SwiftUI.Color(red: 0.85, green: 0.55, blue: 0.18)

        /// Crash / error / destructive.
        static let danger = SwiftUI.Color(red: 0.898, green: 0.282, blue: 0.302)

        /// Success / applied / resolved.
        static let success = SwiftUI.Color(red: 0.188, green: 0.643, blue: 0.424)

        /// Fix proposals -- distinct from the accent so "here's a
        /// decision to make" visually separates from "here's progress."
        static let proposal = SwiftUI.Color(red: 0.557, green: 0.353, blue: 0.875)

        static let textPrimary = SwiftUI.Color.primary
        static let textSecondary = SwiftUI.Color.secondary
        static let textTertiary = SwiftUI.Color.secondary.opacity(0.6)

        static let surfaceBase = SwiftUI.Color(nsColor: .windowBackgroundColor)
        static let surfaceRaised = SwiftUI.Color(nsColor: .controlBackgroundColor)
        /// Recessed surface for code/diff blocks. Deliberately NOT
        /// `.underPageBackgroundColor` -- that system color is meant for
        /// large scrollable document backgrounds and renders very dark in
        /// Dark Mode, which crushed contrast against light body text in
        /// testing. A fixed low-opacity tint over the raised surface
        /// behaves predictably in both appearances instead.
        static let surfaceSunken = SwiftUI.Color.primary.opacity(0.045)

        static let hairline = SwiftUI.Color.primary.opacity(0.08)
        static let hairlineStrong = SwiftUI.Color.primary.opacity(0.15)

        // Diff colors use `Color(light:dark:)` so added/removed text stays
        // legible in both appearances -- the previous fixed dark-RGB
        // values (tuned for light backgrounds) were nearly unreadable
        // against a dark sunken surface in Dark Mode.
        static let diffAdded = SwiftUI.Color.green.opacity(0.16)
        static let diffAddedText = SwiftUI.Color(light: SwiftUI.Color(red: 0.10, green: 0.45, blue: 0.28), dark: SwiftUI.Color(red: 0.45, green: 0.85, blue: 0.60))
        static let diffRemoved = SwiftUI.Color.red.opacity(0.14)
        static let diffRemovedText = SwiftUI.Color(light: SwiftUI.Color(red: 0.70, green: 0.18, blue: 0.20), dark: SwiftUI.Color(red: 0.95, green: 0.50, blue: 0.50))
        static let diffHunk = accent.opacity(0.10)
    }

    // MARK: - Typography

    enum Font {
        static let pageTitle = SwiftUI.Font.system(size: 20, weight: .semibold, design: .default)
        static let sectionTitle = SwiftUI.Font.system(size: 13, weight: .semibold, design: .default)
        static let label = SwiftUI.Font.system(size: 12, weight: .medium, design: .default)
        static let body = SwiftUI.Font.system(size: 12.5, weight: .regular, design: .default)
        static let caption = SwiftUI.Font.system(size: 11, weight: .regular, design: .default)
        static let captionEmphasis = SwiftUI.Font.system(size: 11, weight: .semibold, design: .default)

        static let code = SwiftUI.Font.system(size: 12, weight: .regular, design: .monospaced)
        static let codeSmall = SwiftUI.Font.system(size: 11, weight: .regular, design: .monospaced)
        static let codeEmphasis = SwiftUI.Font.system(size: 12, weight: .semibold, design: .monospaced)
    }

    // MARK: - Spacing (4pt base grid)

    enum Space {
        static let xs: CGFloat = 4
        static let sm: CGFloat = 8
        static let md: CGFloat = 12
        static let lg: CGFloat = 16
        static let xl: CGFloat = 24
        static let xxl: CGFloat = 32
    }

    // MARK: - Radius

    enum Radius {
        static let sm: CGFloat = 6
        static let md: CGFloat = 10
        static let lg: CGFloat = 14
    }

    // MARK: - Shadow

    /// Soft, low-opacity shadow for raised cards -- mimics native macOS
    /// elevation rather than a heavy drop shadow.
    static func cardShadow() -> some ViewModifier { CardShadowModifier() }
}

private struct CardShadowModifier: ViewModifier {
    func body(content: Content) -> some View {
        content
            .shadow(color: .black.opacity(0.06), radius: 1, x: 0, y: 1)
            .shadow(color: .black.opacity(0.04), radius: 8, x: 0, y: 4)
    }
}

// MARK: - Reusable component styles

/// A flat, bordered card -- the base surface for grouped content
/// throughout the app (settings sections, the trace panel, step bodies).
struct CardBackground: ViewModifier {
    var tint: SwiftUI.Color = DS.Color.surfaceRaised
    var borderColor: SwiftUI.Color = DS.Color.hairline

    func body(content: Content) -> some View {
        content
            .background(tint)
            .clipShape(RoundedRectangle(cornerRadius: DS.Radius.md, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: DS.Radius.md, style: .continuous)
                    .stroke(borderColor, lineWidth: 1)
            )
            .modifier(CardShadowModifier())
    }
}

extension View {
    func dsCard(tint: SwiftUI.Color = DS.Color.surfaceRaised, border: SwiftUI.Color = DS.Color.hairline) -> some View {
        modifier(CardBackground(tint: tint, borderColor: border))
    }
}

/// A small rounded status pill (used for severity/status badges).
struct StatusPill: View {
    let text: String
    let color: SwiftUI.Color
    var icon: String? = nil

    var body: some View {
        HStack(spacing: 4) {
            if let icon {
                Image(systemName: icon)
                    .font(.system(size: 9, weight: .bold))
            }
            Text(text)
                .font(DS.Font.captionEmphasis)
        }
        .foregroundStyle(color)
        .padding(.horizontal, 8)
        .padding(.vertical, 3)
        .background(color.opacity(0.12))
        .clipShape(Capsule())
    }
}

/// Primary action button style -- filled with the accent color, used
/// for the one "main" action in a given context (Symbolicate, Diagnose,
/// Approve).
struct DSPrimaryButtonStyle: ButtonStyle {
    var tint: SwiftUI.Color = DS.Color.accent
    var isDisabled: Bool = false

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(DS.Font.label)
            .padding(.horizontal, 14)
            .padding(.vertical, 7)
            .background(isDisabled ? SwiftUI.Color.gray.opacity(0.25) : tint.opacity(configuration.isPressed ? 0.82 : 1))
            .foregroundStyle(isDisabled ? SwiftUI.Color.secondary : .white)
            .clipShape(RoundedRectangle(cornerRadius: DS.Radius.sm, style: .continuous))
            .scaleEffect(configuration.isPressed ? 0.98 : 1)
            .animation(.easeOut(duration: 0.12), value: configuration.isPressed)
    }
}

struct DSSecondaryButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(DS.Font.label)
            .padding(.horizontal, 14)
            .padding(.vertical, 7)
            .background(DS.Color.surfaceSunken)
            .foregroundStyle(DS.Color.textPrimary)
            .clipShape(RoundedRectangle(cornerRadius: DS.Radius.sm, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: DS.Radius.sm, style: .continuous)
                    .stroke(DS.Color.hairlineStrong, lineWidth: 1)
            )
            .scaleEffect(configuration.isPressed ? 0.98 : 1)
            .animation(.easeOut(duration: 0.12), value: configuration.isPressed)
    }
}

extension ButtonStyle where Self == DSPrimaryButtonStyle {
    static func dsPrimary(tint: SwiftUI.Color = DS.Color.accent, disabled: Bool = false) -> DSPrimaryButtonStyle {
        DSPrimaryButtonStyle(tint: tint, isDisabled: disabled)
    }
}
extension ButtonStyle where Self == DSSecondaryButtonStyle {
    static var dsSecondary: DSSecondaryButtonStyle { DSSecondaryButtonStyle() }
}
