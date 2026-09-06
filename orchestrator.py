"""
orchestrator.py
================
DriveFocusSession is the glue: it plays an instruction through Rime,
lets an interrupt land at an arbitrary point in playback, and runs the
classify -> decide -> (re)speak loop.

This is the module scenarios are run against, both in main.py (for the
live/dry-run demo) and test_acceptance.py (for the repeatable acceptance
test). Keeping it separate from both callers is what makes "the same
code path is demoed and tested" true rather than aspirational.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Optional

from decision_engine import RecoveryAction, decide
from interrupt_classifier import classify
from playback_controller import PlaybackController, PlaybackResult


@dataclass
class TurnResult:
    """Everything produced by one speak-instruction[-interrupt] cycle,
    kept together so it can be logged, asserted on, and dumped to the
    evidence log in one shot."""
    instruction_id: str
    playback: PlaybackResult
    decision: Optional[object]  # RecoveryDecision, or None if uninterrupted
    final_spoken_text: str
    stop_latency_seconds: Optional[float]


class DriveFocusSession:
    def __init__(self, rime_session_factory, backend: str = "simulated"):
        """
        rime_session_factory: zero-arg callable returning a fresh
            RimeTTSSession-shaped object (real or simulated). A factory
            rather than a shared instance because DISCARD/REPLACE/REPEAT
            all start a genuinely new Rime turn with its own contextId.
        """
        self._factory = rime_session_factory
        self.backend = backend
        self.log: list[TurnResult] = []

    async def speak_with_possible_interrupt(
        self,
        text: str,
        interrupt_at_seconds: Optional[float] = None,
        interrupt_source: Optional[str] = None,
        interrupt_transcript: Optional[str] = None,
        replacement_text: Optional[str] = None,
    ) -> TurnResult:
        """Speak `text`. If interrupt_at_seconds is set, cut playback at
        that point, classify (interrupt_source, interrupt_transcript),
        decide a recovery action, and execute it (which may itself speak
        further audio, e.g. RESUME's remainder or REPLACE's new text).
        """
        instruction_id = str(uuid.uuid4())[:8]
        rime = self._factory()
        await rime.connect()
        controller = PlaybackController(instruction_id, text, backend=self.backend)
        controller.start()

        interrupted = False
        playback_result: Optional[PlaybackResult] = None
        t0 = time.monotonic()

        async for event in rime.speak(text, context_id=instruction_id):
            if event["type"] == "timestamps":
                controller.set_word_timestamps(event["word_timestamps"])
            elif event["type"] == "chunk":
                controller.feed_chunk(event["audio"])
                elapsed = time.monotonic() - t0
                if (
                    not interrupted
                    and interrupt_at_seconds is not None
                    and elapsed >= interrupt_at_seconds
                ):
                    controller.mark_interrupt_signal()
                    rime.cancel()  # stop Rime from sending/synthesizing more
                    playback_result = controller.stop()  # stop local playback
                    interrupted = True
                    break
            elif event["type"] == "done":
                pass

        await rime.close()

        if not interrupted:
            playback_result = controller.finish_uninterrupted()
            result = TurnResult(
                instruction_id=instruction_id,
                playback=playback_result,
                decision=None,
                final_spoken_text=text,
                stop_latency_seconds=None,
            )
            self.log.append(result)
            return result

        interrupt = classify(interrupt_source or "driver_speech", interrupt_transcript or "")
        decision = decide(playback_result, interrupt, replacement_text=replacement_text)

        final_text = ""
        if decision.action in (RecoveryAction.REPLACE, RecoveryAction.REPEAT, RecoveryAction.RESUME):
            final_text = decision.text_to_speak or ""
            follow_up = self._factory()
            await follow_up.connect()
            follow_controller = PlaybackController(
                instruction_id + "-followup", final_text, backend=self.backend
            )
            follow_controller.start()
            async for event in follow_up.speak(final_text, context_id=instruction_id + "-followup"):
                if event["type"] == "timestamps":
                    follow_controller.set_word_timestamps(event["word_timestamps"])
                elif event["type"] == "chunk":
                    follow_controller.feed_chunk(event["audio"])
            follow_controller.finish_uninterrupted()
            await follow_up.close()
        # DISCARD: final_text stays "", nothing further is spoken.

        result = TurnResult(
            instruction_id=instruction_id,
            playback=playback_result,
            decision=decision,
            final_spoken_text=final_text,
            stop_latency_seconds=playback_result.stop_latency_seconds,
        )
        self.log.append(result)
        return result
