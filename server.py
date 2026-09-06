"""
server.py
=========
FastAPI + WebSocket bridge that turns the DriveFocus interruption/recovery
engine into an interactive web app:

    - A real microphone, via the browser's SpeechRecognition API, replaces
      the scripted driver transcripts used in scenarios.py. This is the
      one "explicitly out of scope" simplification the original
      PROBLEM_STATEMENT.md permitted (real STT) — the web app closes it.
    - Rime's live /ws3 stream is proxied to the browser as base64 mp3
      chunks over a WebSocket, played back live via MediaSource
      Extensions as they arrive (not buffered-then-played), so playback
      position (audio.currentTime) is a real measurement of what the
      driver actually heard, not an estimate.

Reuses, UNMODIFIED:
    decision_engine.decide()        - the four-way recovery rule
    interrupt_classifier.classify() - ROUTE_INVALIDATING / DRIVER_CORRECTION / AMBIENT
    rime_client.RimeTTSSession      - live Rime /ws3 client
    rime_client.WordTimestamps      - heard/unheard word math
    playback_controller.PlaybackResult - the same frozen record shape
        decision_engine.decide() expects, just built from a client-
        reported elapsed time instead of a local monotonic clock.

Run:
    pip install -r requirements.txt
    export RIME_API_KEY=...   (or load it from .env as before)
    uvicorn server:app --reload
    open http://127.0.0.1:8000
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import random
import time
import uuid
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from decision_engine import decide, RecoveryAction
from interrupt_classifier import classify
from playback_controller import PlaybackResult
from rime_client import RimeTTSSession, WordTimestamps

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

DEFAULT_INSTRUCTION = (
    "In 300 metres, turn left onto Anna Salai and continue toward the flyover"
)

# How long a real routing engine would plausibly take to compute an
# alternative route after a hazard/correction interrupt. This is what
# makes DISCARD happen automatically rather than needing a manual
# "pretend nothing's ready" toggle: at the literal instant of the
# interrupt, no replacement exists yet (computing one takes time), so the
# system correctly says nothing first — then, once this delay elapses,
# it automatically follows up by speaking the (simulated) new route.
ROUTING_DELAY_SECONDS = 1.5


# Same disclosed limitation as scenarios.py: no live routing engine, so
# replacement text isn't actually computed from a real reroute — it's a
# stand-in. Unlike scenarios.py's per-scenario hand-authored text, this
# has to work for whatever the user actually said, so:
#   - the exact default instruction gets the SAME specific reroute the
#     CLI demo uses (roadblock_replace scenario), so a fresh install's
#     demo output matches the documented example exactly.
#   - anything else gets a varied (still fabricated, but less flatly
#     generic) alternate-route line instead of one fixed sentence.
_KNOWN_REPLACEMENTS = {
    "anna salai": (
        "Okay, rerouting — in 200 metres, continue straight onto Mount Road instead"
    ),
}
_FALLBACK_REPLACEMENTS = [
    "Okay, rerouting — in 200 metres, turn right onto GST Road instead",
    "Okay, rerouting — continue straight for 400 metres, then take the next left",
    "Okay, rerouting — take the upcoming exit toward the service road instead",
    "Okay, rerouting — in 300 metres, merge onto the bypass instead",
]


def _fake_replacement(original_text: str) -> str:
    text_lower = original_text.lower()
    for key, replacement in _KNOWN_REPLACEMENTS.items():
        if key in text_lower:
            return replacement
    return random.choice(_FALLBACK_REPLACEMENTS)


class LiveTurn:
    """One in-flight (instruction -> maybe interrupted -> maybe followup)
    cycle for a single websocket connection. Plays the same role as
    orchestrator.DriveFocusSession, but driven by real browser events
    (mic transcript + reported playback position) instead of a scripted
    Scenario object."""

    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.rime: Optional[RimeTTSSession] = None
        self.word_timestamps: Optional[WordTimestamps] = None
        self.full_text: str = ""
        self.instruction_id: str = ""
        self.started_at: Optional[float] = None
        self.cancelled = False

    async def speak(self, text: str):
        """Open a fresh Rime session, stream chunks+timestamps to the
        browser as they arrive, stop the moment self.cancelled is set
        (flipped by handle_interrupt) — same cancel contract as
        orchestrator.py's inner loop."""
        self.full_text = text
        self.instruction_id = str(uuid.uuid4())[:8]
        self.cancelled = False
        self.word_timestamps = None

        speaker = os.environ.get("RIME_SPEAKER", "astra")
        model_id = os.environ.get("RIME_MODEL_ID", "mistv2")
        self.rime = RimeTTSSession(speaker=speaker, model_id=model_id)
        await self.rime.connect()

        await self.ws.send_text(json.dumps({
            "type": "instruction_start",
            "instruction_id": self.instruction_id,
            "text": text,
        }))

        self.started_at = time.monotonic()
        try:
            async for event in self.rime.speak(text, context_id=self.instruction_id):
                if self.cancelled:
                    break
                if event["type"] == "timestamps":
                    self.word_timestamps = event["word_timestamps"]
                    await self.ws.send_text(json.dumps({
                        "type": "timestamps",
                        "words": self.word_timestamps.words,
                        "start": self.word_timestamps.start,
                        "end": self.word_timestamps.end,
                    }))
                elif event["type"] == "chunk":
                    await self.ws.send_text(json.dumps({
                        "type": "chunk",
                        "audio_b64": base64.b64encode(event["audio"]).decode("ascii"),
                    }))
        finally:
            await self.rime.close()

        if not self.cancelled:
            await self.ws.send_text(json.dumps({"type": "instruction_done"}))

    def elapsed(self) -> float:
        if self.started_at is None:
            return 0.0
        return time.monotonic() - self.started_at

    async def handle_interrupt(
        self,
        transcript: str,
        client_elapsed: Optional[float],
    ):
        """Called the moment the browser reports a driver interrupt. The
        browser has already hard-paused its own audio element by the time
        this arrives — this method's job is: stop pulling any further
        audio from Rime, classify, and decide.

        For an AMBIENT interrupt this is a single decision (REPEAT/RESUME),
        spoken immediately.

        For a ROUTE_INVALIDATING/DRIVER_CORRECTION interrupt this is
        automatically TWO decisions in sequence, with no manual toggle
        involved:
          1. The instant the interrupt lands, no replacement route exists
             yet — computing one takes time — so decide() is called with
             replacement_text=None and correctly returns DISCARD. Nothing
             is spoken. This is sent to the browser immediately.
          2. After ROUTING_DELAY_SECONDS (simulating that computation —
             the same disclosed stand-in scenarios.py uses for "no live
             routing engine"), a replacement is now available, decide()
             is called again and returns REPLACE, and the new instruction
             is spoken automatically.
        """
        interrupt_signal_time = time.monotonic()
        self.cancelled = True
        if self.rime:
            self.rime.cancel()  # stop pulling any further audio from Rime

        # Prefer the browser-reported elapsed playback time — it reflects
        # what was actually rendered through the speakers, which is the
        # whole point of the "verifiable, word-level" requirement. Fall
        # back to server-side wall-clock elapsed only if the client
        # couldn't report one.
        elapsed = client_elapsed if client_elapsed is not None else self.elapsed()

        heard_n, total_n, heard_text, unheard_text = 0, 0, "", self.full_text
        if self.word_timestamps is not None:
            heard_n = self.word_timestamps.heard_word_count(elapsed)
            total_n = self.word_timestamps.word_count()
            heard_text = self.word_timestamps.heard_text(elapsed)
            unheard_text = self.word_timestamps.unheard_text(elapsed)

        playback = PlaybackResult(
            instruction_id=self.instruction_id,
            full_text=self.full_text,
            word_timestamps=self.word_timestamps,
            started_at=self.started_at or interrupt_signal_time,
            stopped_at=interrupt_signal_time,
            interrupted=True,
            elapsed_at_stop=elapsed,
            heard_word_count=heard_n,
            total_word_count=total_n,
            heard_text=heard_text,
            unheard_text=unheard_text,
            stop_latency_seconds=time.monotonic() - interrupt_signal_time,
        )

        interrupt = classify("driver_speech", transcript)
        invalidating = interrupt.category.value in ("ROUTE_INVALIDATING", "DRIVER_CORRECTION")

        # Phase 1: decide with whatever is true right now — for an
        # invalidating interrupt, that's always "no replacement yet".
        decision = decide(playback, interrupt, replacement_text=None)

        await self.ws.send_text(json.dumps({
            "type": "decision",
            "action": decision.action.value,
            "reason": decision.reason,
            "heard_text": heard_text,
            "unheard_text": unheard_text,
            "heard_word_count": heard_n,
            "total_word_count": total_n,
            "interrupt_category": interrupt.category.value,
            "interrupt_transcript": transcript,
        }))

        if not invalidating:
            # AMBIENT: REPEAT/RESUME already carries the text to speak.
            if decision.action in (RecoveryAction.REPEAT, RecoveryAction.RESUME):
                await self.speak(decision.text_to_speak)
            return

        # Invalidating interrupt: we just correctly discarded. Now
        # simulate the routing engine computing an alternative, then
        # automatically speak it once it's "ready" — no user action
        # required.
        await self.ws.send_text(json.dumps({
            "type": "rerouting",
            "eta_seconds": ROUTING_DELAY_SECONDS,
        }))
        await asyncio.sleep(ROUTING_DELAY_SECONDS)

        replacement_text = _fake_replacement(self.full_text)
        replace_decision = decide(playback, interrupt, replacement_text=replacement_text)

        await self.ws.send_text(json.dumps({
            "type": "decision",
            "action": replace_decision.action.value,
            "reason": replace_decision.reason,
            "heard_text": heard_text,
            "unheard_text": unheard_text,
            "heard_word_count": heard_n,
            "total_word_count": total_n,
            "interrupt_category": interrupt.category.value,
            "interrupt_transcript": transcript,
        }))
        await self.speak(replace_decision.text_to_speak)


@app.get("/")
async def index():
    return FileResponse("static/index.html")


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    turn = LiveTurn(ws)
    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)

            if msg["type"] == "speak":
                text = msg.get("text") or DEFAULT_INSTRUCTION
                await turn.speak(text)

            elif msg["type"] == "interrupt":
                await turn.handle_interrupt(
                    transcript=msg.get("transcript", ""),
                    client_elapsed=msg.get("elapsed_seconds"),
                )
    except WebSocketDisconnect:
        pass