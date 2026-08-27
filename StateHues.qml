pragma ComponentBehavior: Bound

// The colours that mean something, in one place.
//
// A theme cannot be asked for "green". It guarantees an accent and nothing
// else, and on a light theme that accent may be nearly white. So the hue is
// fixed here and only the lightness is borrowed — chosen against whatever
// background the mark will be drawn on, which differs between the bar and the
// panel. Measured on both kinds of ground, every hue below lands on the same
// degree of the colour wheel and clears a 4.5 contrast ratio.
//
// Shared rather than copied because a palette that means something is exactly
// the kind of thing that drifts: the figure saying "listening" in one green and
// the bar saying it in another would be two languages for one fact.

import QtQuick
import qs.Commons

QtObject {
  id: hues

  // Lighter than mid on a dark ground, darker than mid on a light one.
  function lightnessFor(background) {
    const luminance = 0.2126 * background.r
                    + 0.7152 * background.g
                    + 0.0722 * background.b
    return luminance < 0.5 ? 0.62 : 0.32
  }

  // `fallback` is used for states with no meaning of their own — speaking is
  // the assistant's turn, and the desk it sits on should stay recognisable.
  function colorFor(state, background, fallback) {
    const l = hues.lightnessFor(background)
    switch (state) {
    // Ready, and hearing you: green, the one colour nobody has to be taught.
    case "listening": return Qt.hsla(0.35, 0.52, l, 1)
    // Away, working. Sea green — related to the ready colour, plainly not it.
    case "thinking":  return Qt.hsla(0.47, 0.55, l, 1)
    case "error":     return Qt.hsla(0.99, 0.62, l, 1)
    case "speaking":  return fallback
    default:          return Qt.hsla(0.35, 0.34, l, 1)
    }
  }

  // Whether the microphone is open, which is not the same as whether the panel
  // is on screen — and is the only thing a privacy light may be wired to.
  // Backgrounding stops nothing, so a hidden panel still means an open
  // microphone, and the bar has to say so.
  function microphoneIsOpen(state) {
    return state === "listening" || state === "thinking" || state === "speaking"
  }
}
