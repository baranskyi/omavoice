"""Choosing which microphone to listen on and which speaker to answer through.

There is no single right answer, because the desk changes. A laptop lid holds
its microphone a hand's width from the mouth; a display holds one an arm's
length away and correspondingly quieter; earbuds hold one against the cheek.
The same sentence, recorded on this machine, transcribed as twenty-three words
through earbuds and as *"the weather."* through the display — nothing else
about the system differed.

So the choice is made per session, from the devices the system is actually
using, and the echo canceller is switched in or out with it:

  speakers      sound goes into the room, comes back into the microphone, and
                the assistant answers itself unless it is cancelled. Route both
                ends through the canceller.

  headphones    nothing reaches the room, so there is no echo to cancel — and
                the canceller's noise suppression, which costs signal, is pure
                loss here. Use the plain default devices.

Whether headphones are on is read from the sink the system chose: a Bluetooth
audio sink is worn by definition, and a wired one announces itself in the name
of its active port. Anything unrecognised is assumed to be a speaker, because
that assumption is the safe one — an unnecessary canceller costs a little
signal, while a missing one makes the assistant talk to itself.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)

# The nodes created by pipewire/99-omavoice-echo-cancel.conf.
AEC_SOURCE = "echo-cancel-source"
AEC_SINK = "omavoice_playback"

_HEADPHONE_HINTS = ("headphone", "headset", "hands-free", "handsfree")


@dataclass(frozen=True)
class Devices:
    """What this session will record from and play into, and why."""

    input_target: str
    output_target: str
    headphones: bool
    reason: str
    # A short line meant for the settings window rather than the log.
    summary: str = ""
    # Where the microphone should go if the chosen one never speaks. A headset
    # microphone node exists only while its card is in headset profile, and
    # opening it is what asks for the switch — so the first attempt can lose
    # that race, and a device can also disappear mid-session.
    fallback_input: str = ""

    def describe(self) -> str:
        return f"in={self.input_target} out={self.output_target} ({self.reason})"


def short_label(name: str, description: str) -> str:
    """A name that fits in a settings window and still says which microphone.

    PulseAudio descriptions are written for a device tree, not for a person
    choosing between them: "Alder Lake PCH-P High Definition Audio Controller
    Digital Microphone" is sixty-eight characters of which four matter. The
    chipset is not the choice; where the microphone sits is.
    """
    if name.startswith("bluez_input."):
        return description or "Bluetooth headset"
    if name == AEC_SOURCE:
        return "Echo canceller"
    label = re.sub(r"^.*High Definition Audio Controller\s*", "Laptop ", description)
    label = re.sub(r"\s*(Microphone|Mono)$", "", label).strip()
    return label or name


async def _pactl(*args: str) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            "pactl", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=3)
    except (FileNotFoundError, asyncio.TimeoutError, OSError):
        return ""
    return out.decode(errors="replace").strip()


async def _default_sink() -> str:
    return await _pactl("get-default-sink")


async def _default_source() -> str:
    return await _pactl("get-default-source")


async def _active_port_of(sink: str) -> str:
    """The port a sink is currently using, lowercased. Empty when unknown."""
    if not sink:
        return ""
    text = await _pactl("list", "sinks")
    current = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("Name: "):
            current = stripped[6:]
        elif current == sink and stripped.startswith("Active Port: "):
            return stripped[13:].lower()
    return ""


def _is_worn(sink: str, port: str) -> tuple[bool, str]:
    """Does sound from this sink stay out of the room?"""
    if sink.startswith("bluez_output"):
        return True, "bluetooth sink, treated as worn"
    for hint in _HEADPHONE_HINTS:
        if hint in port:
            return True, f"sink port {port!r}"
    return False, f"sink port {port!r}" if port else "no port reported"


async def _node_exists(name: str) -> bool:
    if not name:
        return False
    text = await _pactl("list", "short", "sources")
    text += "\n" + await _pactl("list", "short", "sinks")
    return any(name == line.split("\t")[1] for line in text.splitlines() if "\t" in line)


def _bt_address(node: str) -> str:
    """The MAC out of a bluez node name, with separators normalised.

    Sinks and sources spell the same device differently — `bluez_output.C4_77_
    64_49_C0_D9.1` against `bluez_input.C4:77:64:49:C0:D9` — so neither can be
    matched against the other without this.
    """
    for prefix in ("bluez_output.", "bluez_input."):
        if node.startswith(prefix):
            rest = node[len(prefix) :]
            rest = rest.split(".")[0]
            return rest.replace(":", "_").upper()
    return ""


async def _headset_mic_for(sink: str) -> str:
    """The microphone belonging to the same headset as this sink, if it has one.

    Worth going out of the way for. The system's default source is whatever it
    was before the headphones went on — here, a microphone in a display an
    arm's length away, which transcribed the same sentence as *"the weather."*
    against twenty-three words from the earbuds. The right microphone when
    someone is wearing headphones is almost always the one they are wearing.

    The node is listed even while the card is in A2DP and has no microphone;
    opening it is what makes WirePlumber switch the card to its headset
    profile, and releasing it switches back.
    """
    address = _bt_address(sink)
    if not address:
        return ""
    text = await _pactl("list", "short", "sources")
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        name = parts[1]
        if name.startswith("bluez_input.") and _bt_address(name) == address:
            return name
    return ""


async def resolve(
    configured_input: str,
    configured_output: str,
    avoid: set[str] | None = None,
) -> Devices:
    """Pick devices for one session.

    An explicit `OMAVOICE_INPUT` / `OMAVOICE_OUTPUT` always wins: someone who
    named a device meant it. Everything else is decided here.

    `avoid` holds microphones that already failed to produce audio while this
    daemon has been running. A Bluetooth headset whose HFP transport is broken
    fails the same way every time, and rediscovering that at the start of every
    conversation costs the person ten seconds of talking to nothing.
    """
    avoid = avoid or set()
    if configured_input and configured_output:
        return Devices(
            configured_input,
            configured_output,
            False,
            "set explicitly",
            f"{await _describe_source(configured_input)} · chosen by hand",
        )

    sink = await _default_sink()
    port = await _active_port_of(sink)
    worn, why = _is_worn(sink, port)

    if worn:
        own_mic = await _headset_mic_for(sink)
        if own_mic in avoid:
            log.info("skipping %s — it failed earlier in this session", own_mic)
            own_mic = ""
        source = own_mic or await _default_source()
        detail = "its own microphone" if own_mic else "system default microphone"
        chosen = configured_input or source
        return Devices(
            chosen,
            configured_output or sink,
            True,
            f"headphones — {why}, {detail}, echo canceller not needed",
            f"{await _describe_source(chosen)} · headphones, echo cancellation off",
            await _default_source(),
        )

    # Speakers. The canceller is only useful if it is actually loaded; without
    # the PipeWire config in place its nodes do not exist, and pointing at them
    # would leave the session deaf.
    if await _node_exists(AEC_SOURCE):
        chosen = configured_input or AEC_SOURCE
        if chosen != AEC_SOURCE:
            # The one combination that cannot work, and the one that is easy to
            # assemble by accident: playing into the canceller's sink while
            # recording straight off a device. The canceller dutifully subtracts
            # the echo — into its own source, which nobody is listening to — and
            # the assistant answers its own last sentence. It has to be both
            # ends or neither.
            default = await _default_source()
            if chosen == default:
                log.info(
                    "%s is what the echo canceller is capturing — listening through "
                    "it instead, so the same microphone arrives without the echo",
                    chosen,
                )
                chosen = AEC_SOURCE
            else:
                log.warning(
                    "%s is not the microphone the echo canceller captures (%s), so "
                    "cancellation cannot cover it — playing to the plain sink "
                    "instead. Expect the assistant to hear itself on speakers.",
                    chosen,
                    default or "none",
                )
                return Devices(
                    chosen,
                    configured_output or sink,
                    False,
                    f"speakers — {why}, chosen microphone is outside the canceller",
                    f"{await _describe_source(chosen)} · echo cancellation unavailable",
                    AEC_SOURCE,
                )
        return Devices(
            chosen,
            configured_output or AEC_SINK,
            False,
            f"speakers — {why}, routed through the echo canceller",
            "Speakers · echo cancellation on",
            # No fallback, deliberately. On speakers the canceller's source is
            # the only correct input, because playback goes through its sink:
            # swapping the microphone alone leaves the physical one hearing
            # uncancelled speaker output, which is precisely the mismatched
            # pair that makes the assistant answer its own last sentence. A
            # session that says it cannot hear is a worse afternoon than one
            # that talks to itself is an evening.
            "",
        )

    source = await _default_source()
    log.warning(
        "%s is not loaded — running without echo cancellation, so the assistant "
        "may hear itself through the speakers",
        AEC_SOURCE,
    )
    chosen = configured_input or source
    return Devices(
        chosen,
        configured_output or sink,
        False,
        "speakers, but no echo canceller is loaded",
        f"{await _describe_source(chosen)} · no echo cancellation",
    )

async def _describe_source(name: str) -> str:
    for source in await list_sources():
        if source["name"] == name:
            return source["label"]
    return short_label(name, "")


async def list_sources() -> list[dict]:
    """Every capture device, named the way a person would pick it.

    Monitors are left out: they record what is being played, which is useful to
    a debugging tool and never what anyone means by "microphone".
    """
    text = await _pactl("list", "sources")
    out: list[dict] = []
    current: dict = {}
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("Name: "):
            current = {"name": stripped[6:]}
        elif stripped.startswith("Description: ") and current.get("name"):
            name = current["name"]
            description = stripped[13:]
            if not name.endswith(".monitor"):
                out.append(
                    {
                        "name": name,
                        "description": description,
                        "label": short_label(name, description),
                    }
                )
            current = {}
    return out
