"""
interrupt_classifier.py
========================
Classifies *why* an interrupt happened, independent of *how much* audio
had played. The decision engine combines this with the heard/unheard
word count from playback_controller to choose discard/replace/repeat/resume.

Explicitly out of scope per PROBLEM_STATEMENT.md: real speech-to-text.
The transcript here can be a scripted/simulated stand-in for a driver
utterance (scenarios.py / the CLI demo), OR a real transcript from a
browser's SpeechRecognition API (the web app in server.py) — this module
doesn't know or care which, but it DOES need to tolerate how real STT
engines actually format text: no commas, no periods, inconsistent
contractions. See _normalize() below — this is the fix for a real bug
(v1 of this file required literal commas like "no, turn", which a live
browser transcript will never contain, so DRIVER_CORRECTION could never
fire against real voice input).

Rule set (deliberately small and explainable, not per-scenario hardcoded):
    ROUTE_INVALIDATING  — hazard/road-closure/reroute events, and driver
                           utterances containing route-changing language
                           ("don't take that", "roadblock", "take a
                           different way", "avoid", "closed", ...).
                           These can invalidate the remaining, unspoken
                           content of the current instruction.
    DRIVER_CORRECTION   — driver explicitly countermands the instruction
                           itself ("no turn right", "not this one").
                           Treated the same as ROUTE_INVALIDATING for
                           decision purposes: both can invalidate content.
    AMBIENT             — anything else: ETA questions, volume/media
                           control, general chit-chat. Does not invalidate
                           the instruction's content.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class InterruptCategory(str, Enum):
    ROUTE_INVALIDATING = "ROUTE_INVALIDATING"
    DRIVER_CORRECTION = "DRIVER_CORRECTION"
    AMBIENT = "AMBIENT"


@dataclass
class InterruptEvent:
    source: str          # "driver_speech" | "hazard_detector" | "route_engine"
    transcript: str      # scripted/simulated OR real STT driver utterance
    category: InterruptCategory


# Keyword lists are intentionally short and legible — this is a rule-based
# gate, not a model, so its behavior is auditable in a code review and its
# false-positive/negative modes are easy to state in the demo.
#
# IMPORTANT: every phrase here must match text AFTER _normalize() has run,
# i.e. lowercase, no punctuation, single spaces. Never include commas,
# apostrophes-as-required, or periods in these lists — a live speech
# transcript will not reliably contain them.
_ROUTE_KEYWORDS = [
    "roadblock", "road block", "closed", "closure", "blocked", "accident",
    "avoid", "different route", "different way", "another way", "reroute",
    "dont take", "do not take", "dont go", "cant go that way",
    "cannot go that way", "traffic jam", "flooded", "flood", "construction",
    "detour", "hazard", "collision", "crash", "stalled vehicle", "police",
    "fire ahead",
]
_CORRECTION_KEYWORDS = [
    "no turn", "not this", "wrong turn", "thats not right", "i meant",
    "actually turn", "go back", "cancel that", "turn around",
    "not that one", "undo that", "no i said", "i said turn",
]


def _normalize(text: str) -> str:
    """Lowercase, strip, and remove punctuation so matching is robust to
    however the transcript was produced — a hand-scripted string with full
    punctuation (scenarios.py) or a live browser STT result (server.py),
    which typically omits commas/periods and can vary on contractions."""
    text = text.lower().strip()
    # Collapse common contractions to a punctuation-free form so "don't"
    # and "dont" (and "can't"/"cant") both match the same keyword.
    text = text.replace("’", "'")
    text = re.sub(r"n't\b", "nt", text)
    text = re.sub(r"[^a-z0-9\s]", " ", text)   # drop commas, periods, apostrophes, etc.
    text = re.sub(r"\s+", " ", text).strip()
    return text


def classify(source: str, transcript: str) -> InterruptEvent:
    text = _normalize(transcript)

    if source in ("hazard_detector", "route_engine"):
        # A detected hazard or a computed route change is, by definition,
        # information the driver did not previously have — always
        # route-invalidating for the currently playing instruction.
        return InterruptEvent(source, transcript, InterruptCategory.ROUTE_INVALIDATING)

    if any(kw in text for kw in _CORRECTION_KEYWORDS):
        return InterruptEvent(source, transcript, InterruptCategory.DRIVER_CORRECTION)

    if any(kw in text for kw in _ROUTE_KEYWORDS):
        return InterruptEvent(source, transcript, InterruptCategory.ROUTE_INVALIDATING)

    return InterruptEvent(source, transcript, InterruptCategory.AMBIENT)
