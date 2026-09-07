"""
decision_engine.py
===================
The "third state" the problem statement asks for: the evaluation step
between "stop" and "speak again". Given (1) exactly what the driver heard
and (2) whether the remaining content is still valid, this module picks
one of four recovery behaviors — and nothing scenario-specific is
hardcoded here; every scenario in scenarios.py runs through the same
four rules below.

Decision table
--------------
                          content invalidated?         content still valid?
                          (ROUTE_INVALIDATING /         (AMBIENT interrupt)
                           DRIVER_CORRECTION)
replacement ready         REPLACE                       n/a
replacement NOT ready     DISCARD                        n/a
heard_ratio >= RESUME_THRESHOLD   n/a                    RESUME
heard_ratio <  RESUME_THRESHOLD   n/a                    REPEAT

RESUME_THRESHOLD is the one tunable knob, and it encodes the
"interrupted early vs. interrupted late" distinction from the
non-goals/edge-cases section of the problem statement: resuming a
one-word remainder is fine; resuming after only two words were heard
would be confusing, so that case repeats from the start instead.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from interrupt_classifier import InterruptCategory, InterruptEvent
from playback_controller import PlaybackResult

# Fraction of the instruction that must have been heard, by word count,
# before "finish the remainder" (RESUME) is preferred over "start over"
# (REPEAT). Chosen so a single missed clause (e.g. the street name at the
# end of "turn left onto—") still resumes rather than repeats, while an
# interrupt in the first third of a long instruction repeats.
RESUME_THRESHOLD = 0.55

# Below this many heard words, RESUME is never chosen regardless of
# ratio — resuming a 1-of-2-word instruction is not meaningfully
# different from repeating it, so prefer the clearer REPEAT.
MIN_HEARD_WORDS_FOR_RESUME = 2


class RecoveryAction(str, Enum):
    DISCARD = "DISCARD"
    REPLACE = "REPLACE"
    REPEAT = "REPEAT"
    RESUME = "RESUME"


@dataclass
class RecoveryDecision:
    action: RecoveryAction
    reason: str
    heard_word_count: int
    total_word_count: int
    heard_ratio: float
    text_to_speak: Optional[str]   # None for DISCARD
    interrupt_category: InterruptCategory


def decide(
    playback: PlaybackResult,
    interrupt: InterruptEvent,
    replacement_text: Optional[str] = None,
) -> RecoveryDecision:
    """Pure function: (what was heard, why we were interrupted, whether a
    replacement is ready) -> one of the four recovery actions.

    Kept pure and side-effect-free on purpose, so the acceptance test can
    call it directly, across many trials, without needing a live Rime
    connection or real audio playback — the classification logic itself
    is what must be shown to be *consistent*, per success criterion 3.
    """
    total = playback.total_word_count or len(playback.full_text.split())
    heard = playback.heard_word_count
    ratio = (heard / total) if total else 0.0

    content_invalidated = interrupt.category in (
        InterruptCategory.ROUTE_INVALIDATING,
        InterruptCategory.DRIVER_CORRECTION,
    )

    if content_invalidated:
        if replacement_text:
            return RecoveryDecision(
                action=RecoveryAction.REPLACE,
                reason=(
                    f"Interrupt category {interrupt.category.value} invalidates the "
                    f"remaining content ('{playback.unheard_text}'), and a replacement "
                    f"instruction is ready."
                ),
                heard_word_count=heard,
                total_word_count=total,
                heard_ratio=ratio,
                text_to_speak=replacement_text,
                interrupt_category=interrupt.category,
            )
        return RecoveryDecision(
            action=RecoveryAction.DISCARD,
            reason=(
                f"Interrupt category {interrupt.category.value} invalidates the "
                f"remaining content, but no replacement instruction is ready yet — "
                f"say nothing rather than speak something possibly wrong."
            ),
            heard_word_count=heard,
            total_word_count=total,
            heard_ratio=ratio,
            text_to_speak=None,
            interrupt_category=interrupt.category,
        )

    # AMBIENT: the instruction itself is still true. Decide REPEAT vs RESUME
    # purely from how much of it had already been heard.
    if ratio >= RESUME_THRESHOLD and heard >= MIN_HEARD_WORDS_FOR_RESUME:
        return RecoveryDecision(
            action=RecoveryAction.RESUME,
            reason=(
                f"Ambient interrupt does not invalidate the instruction. "
                f"{heard}/{total} words ({ratio:.0%}) were already heard, which is "
                f">= the {RESUME_THRESHOLD:.0%} resume threshold, so only the "
                f"unheard remainder is spoken."
            ),
            heard_word_count=heard,
            total_word_count=total,
            heard_ratio=ratio,
            text_to_speak=playback.unheard_text,
            interrupt_category=interrupt.category,
        )

    return RecoveryDecision(
        action=RecoveryAction.REPEAT,
        reason=(
            f"Ambient interrupt does not invalidate the instruction, but only "
            f"{heard}/{total} words ({ratio:.0%}) were heard, which is below the "
            f"{RESUME_THRESHOLD:.0%} resume threshold — resuming mid-sentence would "
            f"be confusing, so the full instruction is repeated from the start."
        ),
        heard_word_count=heard,
        total_word_count=total,
        heard_ratio=ratio,
        text_to_speak=playback.full_text,
        interrupt_category=interrupt.category,
    )
