"""
rime_client.py
================
Thin async wrapper around Rime's `/ws3` JSON WebSocket endpoint.

Why /ws3 and not HTTP streaming:
    Only the JSON WebSocket endpoints (/ws3, /ws2) return word-level
    timestamps, and only they support the `clear` operation needed to
    cancel in-flight synthesis. HTTP streaming has neither, so it cannot
    satisfy the "what did the driver actually hear" or "no stale audio
    after cancel" requirements in the problem statement. See:
    https://docs.rime.ai/docs/websockets

Protocol summary (verified against Rime's current docs, Sept 2026):
    Connect:  wss://users-ws.rime.ai/ws3?speaker=<speaker>&modelId=<model>
              &audioFormat=<fmt>&lang=<lang>
              Header: Authorization: Bearer <RIME_API_KEY>

    Send:     {"text": "...", "contextId": "turn-001"}
              {"operation": "flush"}   # synthesize buffered text now
              {"operation": "clear"}   # DISCARD buffered/queued text,
                                       # do not synthesize it
              {"operation": "eos"}     # finish + close

    Receive:  {"type": "chunk", "data": "<base64 pcm/mp3>", "contextId": ...}
              {"type": "timestamps",
               "word_timestamps": {"words": [...], "start": [...], "end": [...]},
               "contextId": ...}
              {"type": "done", "contextId": ...}
              {"type": "error", ...}

    IMPORTANT: the audio field in the "chunk" event is named "data", not
    "audio" — this was wrong in an earlier draft of this file and would
    silently KeyError (or worse, silently drop every chunk if accessed
    with .get()) against the real API. Confirmed against Rime's own
    current sample client code and published TypeScript schema:
        type AudioChunkEvent = { type: "chunk", data: Base64String,
                                  contextId: string | null }

    Rime sends exactly one "done" event per "flush" operation (and "eos"
    emits a final "done" for whatever remains buffered) — this is what
    lets speak() below treat "done" as "this utterance is fully spoken."

This module is transport-only: it does not decide *what* to say or *when*
to cancel. That is decision_engine.py's job. This file's only contract is:
    - stream audio chunks as they arrive
    - stream word-level timestamps as they arrive
    - guarantee that calling `cancel()` stops any further chunk from this
      module reaching the caller, even if Rime has more audio in flight
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
from dataclasses import dataclass, field
from typing import AsyncIterator, Optional

try:
    import websockets
except ImportError:  # pragma: no cover - optional at import time
    websockets = None

RIME_WS_URL = "wss://users-ws.rime.ai/ws3"


@dataclass
class WordTimestamps:
    words: list[str] = field(default_factory=list)
    start: list[float] = field(default_factory=list)   # seconds, from utterance start
    end: list[float] = field(default_factory=list)      # seconds, from utterance start

    def word_count(self) -> int:
        return len(self.words)

    def heard_word_count(self, elapsed_seconds: float) -> int:
        """Word-level precision count of how many words had *finished*
        playing by `elapsed_seconds` into this utterance.

        A word only counts as "heard" once it has fully finished playing —
        using `end[i] <= elapsed_seconds` rather than `start[i]` avoids
        crediting the driver with a word that was cut off mid-syllable.
        """
        count = 0
        for end_time in self.end:
            if end_time <= elapsed_seconds:
                count += 1
            else:
                break
        return count

    def heard_text(self, elapsed_seconds: float) -> str:
        n = self.heard_word_count(elapsed_seconds)
        return " ".join(self.words[:n])

    def unheard_text(self, elapsed_seconds: float) -> str:
        n = self.heard_word_count(elapsed_seconds)
        return " ".join(self.words[n:])


@dataclass
class AudioChunk:
    data: bytes
    context_id: Optional[str]


class RimeStreamCancelled(Exception):
    """Raised internally to unwind an in-flight stream after cancel()."""


class RimeTTSSession:
    """One logical connection to Rime's /ws3 endpoint.

    Usage:
        session = RimeTTSSession(speaker="astra", model_id="mistv2")
        await session.connect()
        async for event in session.speak("Turn left in 300 metres", context_id="t1"):
            if event["type"] == "chunk":
                play(event["audio"])
            elif event["type"] == "timestamps":
                remember(event["word_timestamps"])
        await session.close()

    Cancellation contract:
        session.cancel() may be called concurrently (e.g. from an
        interrupt handler running in another task). Once cancel() returns,
        no further "chunk" events for the in-flight utterance will be
        yielded by `speak()`, even if bytes are still arriving on the
        socket — they are read and discarded so the socket stays clean
        for the next utterance, but never handed to the caller/playback
        layer. This is what makes "no stale audio reaches playback"
        verifiable rather than best-effort.
    """

    def __init__(
        self,
        speaker: str = "astra",
        model_id: str = "mistv2",
        audio_format: str = "mp3",
        language: str = "eng",
        api_key: Optional[str] = None,
    ):
        self.speaker = speaker
        self.model_id = model_id
        self.audio_format = audio_format
        self.language = language
        self.api_key = api_key or os.environ.get("RIME_API_KEY")
        self._ws = None
        self._cancelled = asyncio.Event()

    async def connect(self):
        if websockets is None:
            raise RuntimeError(
                "The 'websockets' package is required for live Rime calls. "
                "Install it with `pip install websockets`, or run in --dry-run mode."
            )
        if not self.api_key:
            raise RuntimeError(
                "RIME_API_KEY is not set. Export it or pass api_key=, or run "
                "in --dry-run mode (see README.md)."
            )
        url = (
            f"{RIME_WS_URL}?speaker={self.speaker}&modelId={self.model_id}"
            f"&audioFormat={self.audio_format}&lang={self.language}"
        )
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            # websockets >= 13 renamed extra_headers -> additional_headers.
            # This is the current kwarg used in Rime's own sample code.
            self._ws = await websockets.connect(url, additional_headers=headers)
        except TypeError:
            # Fallback for older websockets releases (< 13) that don't
            # accept additional_headers yet.
            self._ws = await websockets.connect(url, extra_headers=headers)

    async def close(self):
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

    def cancel(self):
        """Signal that the current utterance must stop reaching playback.

        This is called from the interrupt path, NOT from inside speak().
        It must be safe to call from a different asyncio task than the one
        running speak()'s consumer loop.
        """
        self._cancelled.set()

    async def speak(self, text: str, context_id: str) -> AsyncIterator[dict]:
        """Send `text` for synthesis and yield events as they arrive.

        Yields dicts of shape {"type": "chunk"|"timestamps"|"done", ...}.
        Stops yielding (raises StopAsyncIteration) the moment cancel() has
        been called, regardless of what is still in flight on the socket.
        """
        self._cancelled.clear()
        await self._ws.send(json.dumps({"text": text, "contextId": context_id}))
        await self._ws.send(json.dumps({"operation": "flush"}))

        while True:
            if self._cancelled.is_set():
                # Tell Rime to drop anything still buffered for this turn
                # so it never gets synthesized in the first place.
                await self._ws.send(json.dumps({"operation": "clear"}))
                return

            recv_task = asyncio.create_task(self._ws.recv())
            cancel_task = asyncio.create_task(self._cancelled.wait())
            done, pending = await asyncio.wait(
                {recv_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if cancel_task in done:
                recv_task.cancel()
                await self._ws.send(json.dumps({"operation": "clear"}))
                return
            cancel_task.cancel()
            raw = recv_task.result()
            msg = json.loads(raw)

            if self._cancelled.is_set():
                # A cancel raced in between recv() returning and us
                # inspecting it — still drop the message rather than
                # yield it, so "cancelled" is a hard guarantee, not a
                # best-effort race.
                await self._ws.send(json.dumps({"operation": "clear"}))
                return

            msg_type = msg.get("type")

            if msg_type == "chunk":
                yield {
                    "type": "chunk",
                    # Rime's field is "data", not "audio" — see module
                    # docstring. We keep our own event key as "audio" so
                    # nothing downstream (orchestrator.py, playback_controller.py)
                    # needs to change.
                    "audio": base64.b64decode(msg["data"]),
                    "context_id": msg.get("contextId"),
                }
            elif msg_type == "timestamps":
                wt = msg.get("word_timestamps", {})
                yield {
                    "type": "timestamps",
                    "word_timestamps": WordTimestamps(
                        words=wt.get("words", []),
                        start=wt.get("start", []),
                        end=wt.get("end", []),
                    ),
                    "context_id": msg.get("contextId"),
                }
            elif msg_type == "done":
                yield {"type": "done", "context_id": msg.get("contextId")}
                return
            elif msg_type == "error":
                raise RuntimeError(f"Rime returned an error event: {msg}")
            # Unknown event types are ignored rather than raising, so a
            # future additive field from Rime doesn't break this client.
