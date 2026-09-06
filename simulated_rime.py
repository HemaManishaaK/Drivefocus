"""
simulated_rime.py
==================
A drop-in stand-in for RimeTTSSession that requires no network access and
no RIME_API_KEY. It reproduces the same event shape (`chunk`,
`timestamps`, `done`) using a deterministic, disclosed word-timing model
(average English speaking rate) instead of a live Rime connection.

This is used for:
    - `python main.py --dry-run`, so the decision engine and playback
      cutoff logic can be demoed without live credentials.
    - `test_acceptance.py`, so the acceptance test is fast and
      reproducible in CI (a live network dependency in a test suite that
      must show "consistent... across multiple trials" would make
      flakiness indistinguishable from a real classification bug).

This is explicitly NOT what runs in the judged demo — RIME_EVIDENCE.md
and README.md both disclose that the judged path uses live
RimeTTSSession (rime_client.py) against wss://users-ws.rime.ai/ws3, and
`main.py` defaults to live mode unless --dry-run is passed.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

from rime_client import WordTimestamps

# ~2.5 words/second is a typical brisk TTS narration rate; used only to
# fabricate plausible word start/end times for offline testing.
WORDS_PER_SECOND = 2.5


def _fabricate_timestamps(text: str) -> WordTimestamps:
    words = text.split()
    start, end = [], []
    t = 0.0
    for w in words:
        dur = max(len(w), 3) / (WORDS_PER_SECOND * 5)  # longer words take a touch longer
        start.append(round(t, 3))
        t += dur
        end.append(round(t, 3))
        t += 0.05  # inter-word gap
    return WordTimestamps(words=words, start=start, end=end)


class SimulatedRimeSession:
    """Same call surface as RimeTTSSession, minus connect()/close() needing
    a real socket, and speak() draining a synthetic timeline instead of a
    live one."""

    def __init__(self, *_, **__):
        pass

    async def connect(self):
        return None

    async def close(self):
        return None

    def cancel(self):
        self._cancelled = True

    async def speak(self, text: str, context_id: str) -> AsyncIterator[dict]:
        self._cancelled = False
        wt = _fabricate_timestamps(text)
        yield {"type": "timestamps", "word_timestamps": wt, "context_id": context_id}
        # Emit one fabricated audio chunk per word, sleeping for the word's
        # own duration (not just the inter-word gap) so that real wall-clock
        # elapsed time tracks the fabricated word-end timestamps. This is
        # what lets a caller's real-time interrupt_at_seconds land at the
        # point in the utterance the timestamps claim it does.
        prev_end = 0.0
        for i, w in enumerate(wt.words):
            if self._cancelled:
                return
            await asyncio.sleep(max(0.0, wt.end[i] - prev_end))
            if self._cancelled:
                return
            yield {"type": "chunk", "audio": w.encode("utf-8"), "context_id": context_id}
            prev_end = wt.end[i]
        if not self._cancelled:
            yield {"type": "done", "context_id": context_id}
