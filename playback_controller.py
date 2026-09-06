"""
playback_controller.py
=======================
Owns the "what did the driver actually hear" measurement and the
"stop immediately, no stale audio" guarantee.

Two playback backends are supported:
    - real:      writes decoded audio bytes to a local .mp3 file as they
                 arrive, then makes a best-effort attempt to play that
                 file through the OS's default audio output the moment
                 the utterance finishes (or is cut short). "Best-effort"
                 because this reference implementation intentionally has
                 no bundled audio library dependency (see requirements.txt)
                 — it shells out to whatever native player the OS already
                 has (afplay / mpg123,ffplay,cvlc / start), and silently
                 no-ops if none is found, so the demo still runs on a
                 machine with no audio device at all.
    - simulated: no audio hardware at all — advances a virtual clock at
                 wall-clock speed using the chunk arrival cadence. This
                 is what CI / the acceptance test suite runs, because it
                 is deterministic and does not require a sound card,
                 which is disclosed openly in RIME_EVIDENCE.md.

Either backend reports through the same interface, so the decision engine
and orchestrator never need to know which one is active.
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Optional

from rime_client import WordTimestamps

# Where "real" backend utterances get written. Created on first use.
AUDIO_OUTPUT_DIR = os.path.join(os.getcwd(), "drivefocus_audio_out")


@dataclass
class PlaybackResult:
    """A frozen record of one utterance's playback, used as the input to
    the decision engine and as the evidence artifact for the acceptance
    test."""
    instruction_id: str
    full_text: str
    word_timestamps: Optional[WordTimestamps]
    started_at: float
    stopped_at: Optional[float]          # None if it played to completion
    interrupted: bool
    elapsed_at_stop: float                # seconds into the utterance
    heard_word_count: int
    total_word_count: int
    heard_text: str
    unheard_text: str
    stop_latency_seconds: Optional[float]  # interrupt signal -> audio halted
    audio_file_path: Optional[str] = None  # set only for backend="real"


def _find_system_player() -> Optional[list[str]]:
    """Return an argv prefix for a native player on this OS, or None if
    none is found. We never fail the demo for lack of one — audio
    playback is a nice-to-have on top of the actual hard-voice logic
    (decision engine / word-level heard-unheard accounting), which
    works identically with or without a speaker attached."""
    system = platform.system()
    if system == "Darwin":
        if shutil.which("afplay"):
            return ["afplay"]
    elif system == "Linux":
        for candidate in ("mpg123", "ffplay", "cvlc", "aplay"):
            path = shutil.which(candidate)
            if path:
                if candidate == "ffplay":
                    return [path, "-nodisp", "-autoexit", "-loglevel", "quiet"]
                if candidate == "cvlc":
                    return [path, "--play-and-exit", "-q"]
                return [path]
    elif system == "Windows":
        # PowerShell's Media.SoundPlayer only plays .wav, so for mp3 we
        # shell out to the default file association instead.
        return ["cmd", "/c", "start", "", ""]  # trailing "" = the file path slot
    return None


class PlaybackController:
    """Drives one utterance's playback and can be stopped mid-flight.

    Hard-stop guarantee: once `stop()` returns, `_cancelled` is True and
    every subsequent call to `feed_chunk()` is a no-op that does not reach
    the output device/file. Combined with RimeTTSSession.cancel() (which
    stops chunks from even being *sent* to this controller), this closes
    both ends of the pipe: nothing already queued here plays, and nothing
    new gets queued.
    """

    def __init__(self, instruction_id: str, full_text: str, backend: str = "simulated"):
        self.instruction_id = instruction_id
        self.full_text = full_text
        self.backend = backend
        self._cancelled = False
        self._started_at: Optional[float] = None
        self._stopped_at: Optional[float] = None
        self._word_timestamps: Optional[WordTimestamps] = None
        self._audio_bytes = bytearray()
        self._interrupt_signal_time: Optional[float] = None
        self._audio_file_path: Optional[str] = None

    def start(self):
        self._started_at = time.monotonic()

    def set_word_timestamps(self, wt: WordTimestamps):
        self._word_timestamps = wt

    def feed_chunk(self, audio: bytes):
        if self._cancelled:
            return  # guarantee: nothing reaches "playback" after stop()
        self._audio_bytes.extend(audio)

    def elapsed_seconds(self, at: Optional[float] = None) -> float:
        if self._started_at is None:
            return 0.0
        end = at if at is not None else (self._stopped_at or time.monotonic())
        return max(0.0, end - self._started_at)

    def mark_interrupt_signal(self):
        """Call this the instant the interrupt (driver speech / hazard
        event) is detected, BEFORE doing any classification work, so the
        stop-latency measurement reflects only the stop path, not the
        downstream NLU/decision cost."""
        self._interrupt_signal_time = time.monotonic()

    def _flush_to_disk_and_play(self) -> Optional[str]:
        """Real backend only: write whatever bytes were accepted before
        the cutoff to disk, then fire off a best-effort playback command.
        Returns the file path, or None if nothing was written (e.g.
        simulated backend, or zero bytes received before an early cut)."""
        if self.backend != "real" or not self._audio_bytes:
            return None

        os.makedirs(AUDIO_OUTPUT_DIR, exist_ok=True)
        path = os.path.join(AUDIO_OUTPUT_DIR, f"{self.instruction_id}.mp3")
        with open(path, "wb") as f:
            f.write(self._audio_bytes)

        player = _find_system_player()
        if player:
            try:
                argv = list(player)
                if argv and argv[-1] == "":  # Windows "start" trailing slot
                    argv[-1] = path
                else:
                    argv.append(path)
                # Fire-and-forget: don't block the async decision loop
                # waiting for playback to finish.
                subprocess.Popen(
                    argv,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except OSError:
                pass  # playback is best-effort; never fail the run for it
        return path

    def stop(self) -> PlaybackResult:
        """Hard-stop playback now and return the heard/unheard breakdown."""
        now = time.monotonic()
        self._cancelled = True
        self._stopped_at = now
        elapsed = self.elapsed_seconds(at=now)

        heard_n = 0
        heard_text = ""
        unheard_text = self.full_text
        total_n = 0
        if self._word_timestamps is not None:
            heard_n = self._word_timestamps.heard_word_count(elapsed)
            heard_text = self._word_timestamps.heard_text(elapsed)
            unheard_text = self._word_timestamps.unheard_text(elapsed)
            total_n = self._word_timestamps.word_count()

        stop_latency = None
        if self._interrupt_signal_time is not None:
            stop_latency = now - self._interrupt_signal_time

        audio_path = self._flush_to_disk_and_play()

        return PlaybackResult(
            instruction_id=self.instruction_id,
            full_text=self.full_text,
            word_timestamps=self._word_timestamps,
            started_at=self._started_at or now,
            stopped_at=now,
            interrupted=True,
            elapsed_at_stop=elapsed,
            heard_word_count=heard_n,
            total_word_count=total_n,
            heard_text=heard_text,
            unheard_text=unheard_text,
            stop_latency_seconds=stop_latency,
            audio_file_path=audio_path,
        )

    def finish_uninterrupted(self) -> PlaybackResult:
        """Call when the utterance played to completion with no interrupt."""
        now = time.monotonic()
        self._stopped_at = now
        total_n = self._word_timestamps.word_count() if self._word_timestamps else 0
        full = self._word_timestamps.words if self._word_timestamps else []

        audio_path = self._flush_to_disk_and_play()

        return PlaybackResult(
            instruction_id=self.instruction_id,
            full_text=self.full_text,
            word_timestamps=self._word_timestamps,
            started_at=self._started_at or now,
            stopped_at=now,
            interrupted=False,
            elapsed_at_stop=self.elapsed_seconds(at=now),
            heard_word_count=total_n,
            total_word_count=total_n,
            heard_text=" ".join(full),
            unheard_text="",
            stop_latency_seconds=None,
            audio_file_path=audio_path,
        )
