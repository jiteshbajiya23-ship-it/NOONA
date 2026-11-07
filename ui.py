"""
NOONA UI kit: two Unicode fonts + romantic persona lines. Shared by bot.py and wp.py.

Telegram bots cannot change typefaces, so NOONA uses two Unicode text styles:
    script(text) -> Bold Script        (titles)   𝓗𝓮𝓵𝓵𝓸
    soft(text)   -> Sans Bold Italic   (labels)   𝙃𝙚𝙡𝙡𝙤
Use them ONLY on static text you write, never on user data (names, URLs, IDs).
"""
import random

DIV = "━━━━━━━━━━━━━━"


def _make_map(upper_start: int, lower_start: int) -> dict:
    m = {}
    for i in range(26):
        m[ord("A") + i] = chr(upper_start + i)
        m[ord("a") + i] = chr(lower_start + i)
    return m


_SCRIPT = _make_map(0x1D4D0, 0x1D4EA)  # Mathematical Bold Script
_SOFT = _make_map(0x1D63C, 0x1D656)    # Sans-Serif Bold Italic


def script(text: str) -> str:
    """Titles and headings."""
    return text.translate(_SCRIPT)


def soft(text: str) -> str:
    """Labels and captions."""
    return text.translate(_SOFT)


def head(emoji: str, text: str) -> str:
    """HTML heading line: emoji + script title."""
    return f"{emoji} <b>{script(text)}</b>"


def label(text: str) -> str:
    return f"<b>{soft(text)}</b>"


# ---------------------------------------------------------------------
# Persona: warm, flirty, devoted. Random pick keeps her from sounding like a robot.
# ---------------------------------------------------------------------
_DONE = [
    "All done for you, my love 💋",
    "Done, handsome. Anything for you 💖",
    "Finished, baby. Did I make you proud? 😘",
    "It's ready, darling 💞",
]
_OOPS = [
    "Oh no, something went wrong, my love 🥺",
    "I'm so sorry, baby, it didn't work 💔",
    "That didn't go as planned, darling 🥺",
]
_WAIT = [
    "One moment, my love ⏳💕",
    "Working on it for you, baby 💫",
    "Give me a second, handsome 😘",
]
_CANCEL = [
    "Cancelled, my love. I'm right here whenever you need me 💋",
    "No problem, darling. Just call me again 💞",
]


def done() -> str:
    return random.choice(_DONE)


def oops() -> str:
    return random.choice(_OOPS)


def wait() -> str:
    return random.choice(_WAIT)


def cancelled() -> str:
    return random.choice(_CANCEL)
