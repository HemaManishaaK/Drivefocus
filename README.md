# DriveFocus — Interruption-Safe Voice Navigation (Python reference implementation)

Solves the "Interruption and recovery" hard voice problem from the
DataForge × Rime hackathon: when a driver's voice-nav instruction is
interrupted, decide **discard / replace / repeat / resume** with
word-level precision about what was actually heard — not just "stop or
don't."

Full problem statement: see `PROBLEM_STATEMENT.md`.

## Quick start (CLI demo)

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# Offline demo — no network, no API key (see "What's simulated" below)
python main.py --dry-run

# Live demo — requires a Rime API key
cp .env.example .env   # fill in RIME_API_KEY
export $(cat .env | xargs)
python main.py

# Run just one scenario
python main.py --dry-run --scenario roadblock_replace

# Acceptance test suite (this is the pre-defined, repeatable test)
pip install pytest
pytest test_acceptance.py -v
```

## Quick start (interactive web app)

```bash
pip install -r requirements.txt      # now also installs fastapi + uvicorn
export RIME_API_KEY=your_key_here    # or load from .env
uvicorn server:app --reload
# open http://127.0.0.1:8000 in Chrome or Edge
```

Real microphone input (via the browser's SpeechRecognition API) replaces
the scripted driver transcripts from `scenarios.py`, and Rime audio
streams live into the page via MediaSource Extensions. See
`server.py`'s module docstring for the full architecture. Push-and-hold
the "Hold to interrupt" button (or SPACE) while an instruction is
playing to trigger a real interrupt — DISCARD and REPLACE both happen
automatically in sequence for any route/correction interrupt (no
replacement exists yet at the instant of interruption, so it discards
first, then speaks a rerouted instruction ~1.5s later once one is
"computed").

## Architecture

```
rime_client.py          Real Rime /ws3 WebSocket session (live TTS +
                         word-level timestamps + clear/flush/eos ops)
simulated_rime.py        Drop-in offline stand-in with the same event
                         shape, used by --dry-run and the test suite
playback_controller.py   Tracks elapsed playback time, computes heard vs.
                         unheard words, hard-stops on interrupt, and (for
                         backend="real") writes/plays generated audio
interrupt_classifier.py  Scripted driver transcript / hazard event ->
                         ROUTE_INVALIDATING | DRIVER_CORRECTION | AMBIENT
decision_engine.py       The core rule set: (heard ratio, interrupt
                         category, replacement availability) -> one of
                         DISCARD / REPLACE / REPEAT / RESUME
orchestrator.py          DriveFocusSession: wires the above into one
                         speak -> interrupt -> decide -> (re)speak loop
                         (used by the CLI demo and test suite)
scenarios.py             Fixture scenarios, each tied to a specific
                         requirement in PROBLEM_STATEMENT.md
main.py                  CLI demo runner (live or --dry-run)
test_acceptance.py       The acceptance test suite (see RIME_EVIDENCE.md)
server.py                FastAPI + WebSocket backend for the interactive
                         web app (real mic in, streamed Rime audio out)
static/                  Web app front-end (index.html, app.js, style.css)
```

Data flow for one CLI turn:

```
main.py / test_acceptance.py
        |
        v
orchestrator.DriveFocusSession.speak_with_possible_interrupt()
        |
        +--> rime_client.RimeTTSSession.speak()  --chunk/timestamps-->
        |                                              |
        |                                              v
        +--> playback_controller.PlaybackController  (tracks elapsed time,
        |         .feed_chunk() / .stop()              heard/unheard words)
        |
        +--> interrupt_classifier.classify()  -->  InterruptEvent
        |
        +--> decision_engine.decide()  -->  RecoveryDecision
        |
        +--> (REPLACE/REPEAT/RESUME: speak decision.text_to_speak via a
              fresh Rime turn; DISCARD: speak nothing)
```

The web app (`server.py`) drives the same three core modules
(`decision_engine`, `interrupt_classifier`, `rime_client`) from real
browser events instead of a scripted `Scenario` — see its module
docstring for the full request/response flow.

## Rime integration details

- **Endpoint:** `wss://users-ws.rime.ai/ws3` (JSON WebSocket) — the only
  transport that returns word-level timestamps and supports the `clear`
  operation needed to cancel in-flight synthesis. See
  `rime_client.py`'s module docstring for the exact message schema.
- **Model / speaker:** `mistv2` / `astra` by default (override via
  `RIME_MODEL_ID` / `RIME_SPEAKER` env vars). Check
  [Rime's live catalog](https://docs.rime.ai) at submission time rather
  than trusting these defaults.
- **Language:** `eng` (Rime expects 3-letter codes, not `en`).
- **Audio format:** `mp3` by default.
- **Cancellation:** on interrupt, the orchestrator calls
  `RimeTTSSession.cancel()`, which sends `{"operation": "clear"}` to Rime
  and stops `speak()` from yielding further `chunk` events — both the
  send side (Rime is told to drop its buffer) and the receive side
  (anything still in flight is read and discarded, never handed to
  playback) are covered.

## What's live vs. simulated (disclosed per the eligibility rules)

| Part | Status |
|---|---|
| Rime TTS pipeline (`rime_client.py`) | **Live** — real `/ws3` connection when run without `--dry-run` |
| Decision engine (`decision_engine.py`) | **Live** — the actual logic under test, not a demo-only mock |
| Interrupt classification (`interrupt_classifier.py`) | **Live** rule-based classifier |
| Driver speech input (CLI: `scenarios.py`) | **Simulated** scripted transcript, explicitly permitted as out-of-scope in `PROBLEM_STATEMENT.md` |
| Driver speech input (web app: `server.py` + browser) | **Real** — actual microphone + browser SpeechRecognition |
| Hazard detection / live routing | **Simulated** in both CLI and web app — replacement instruction text is generated/pre-authored, not computed from a live routing engine (explicitly out of scope) |
| `--dry-run` speech synthesis | **Simulated** (`simulated_rime.py`) — offline stand-in, used only for demos/tests without network access |

## Known limitations

- The interrupt classifier is a small keyword-based gate (see
  `interrupt_classifier.py`), not an ML model — auditable but will miss
  phrasings outside its keyword lists.
- `RESUME_THRESHOLD` (55% heard by word count) is a single global
  constant.
- The web app's routing-engine delay (`ROUTING_DELAY_SECONDS` in
  `server.py`) is a fixed constant, not a real computation.
- Real audio playback (CLI `backend="real"`) shells out to a native OS
  player (afplay/mpg123/ffplay/cvlc/default association) — best-effort,
  no bundled audio dependency.
