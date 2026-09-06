/*
app.js
======
Client side of the DriveFocus live demo.

Two things happen concurrently once "Speak instruction" is pressed:

  1. Rime audio streams in over the WebSocket as base64 mp3 chunks and is
     appended to a MediaSource SourceBuffer as it arrives, so playback
     starts before the whole utterance has even finished synthesizing —
     this is what makes `player.currentTime` a real measurement of what
     was heard, not an estimate.

  2. The "Hold to interrupt" button (or holding SPACE) arms the browser's
     native SpeechRecognition API. The instant you press it:
        - player.pause() fires immediately, client-side, zero network
          round-trip — this is the actual hard-stop.
        - the exact playback position at that instant is captured.
     Only once you release the button (and recognition finalizes a
     transcript) does the transcript + captured position get sent to the
     server for classification — but the audio was already stopped
     before any of that happened, matching the original system's
     mark_interrupt_signal() -> cancel() -> *then* classify()/decide()
     ordering.

Known limitation (disclosed, same spirit as the original project's
RIME_EVIDENCE.md): push-to-talk avoids the mic picking up the app's own
TTS output as a false "interrupt" without needing acoustic echo
cancellation. A always-listening version is possible but needs an AEC
step (or headphones) to be reliable — out of scope for this demo.
*/

const ws = new WebSocket(`ws://${location.host}/ws`);
const player = document.getElementById("player");
const speakBtn = document.getElementById("speakBtn");
const interruptBtn = document.getElementById("interruptBtn");
const instructionText = document.getElementById("instructionText");
const logEl = document.getElementById("log");

let mediaSource = null;
let sourceBuffer = null;
let pendingChunks = [];
let recognizing = false;
let recognition = null;
let interruptElapsedAtPress = null;
let awaitingFinalTranscript = false;

function log(msg, cls = "") {
  const line = document.createElement("div");
  line.className = `log-line ${cls}`;
  line.textContent = msg;
  logEl.appendChild(line);
  logEl.scrollTop = logEl.scrollHeight;
}

// ---------- WebSocket wiring ----------

ws.addEventListener("open", () => {
  log("Connected to server.");
  speakBtn.disabled = false;
});

ws.addEventListener("close", () => log("Disconnected.", "error"));
ws.addEventListener("error", () => log("WebSocket error.", "error"));

ws.addEventListener("message", (evt) => {
  const msg = JSON.parse(evt.data);

  switch (msg.type) {
    case "instruction_start":
      log(`▶ Speaking: "${msg.text}"`);
      startNewPlayback();
      interruptBtn.disabled = false;
      break;

    case "timestamps":
      // Available if you want to render live word highlighting; not
      // required for the interrupt flow itself, which relies on
      // player.currentTime rather than these.
      break;

    case "chunk":
      appendChunk(base64ToBytes(msg.audio_b64));
      break;

    case "instruction_done":
      finalizeStream();
      break;

    case "rerouting":
      log(`🔄 Rerouting... (~${msg.eta_seconds}s, simulated routing engine)`);
      break;

    case "decision":
      interruptBtn.disabled = true;
      log(
        `Decision: ${msg.action} — heard ${msg.heard_word_count}/${msg.total_word_count} words`,
        "decision"
      );
      log(`  heard: "${msg.heard_text}"`, "decision");
      log(`  unheard: "${msg.unheard_text}"`, "decision");
      log(`  interrupt category: ${msg.interrupt_category} ("${msg.interrupt_transcript}")`, "decision");
      log(`  reason: ${msg.reason}`, "decision");
      break;
  }
});

// ---------- Live MediaSource playback ----------

function startNewPlayback() {
  pendingChunks = [];
  mediaSource = new MediaSource();
  player.src = URL.createObjectURL(mediaSource);

  mediaSource.addEventListener("sourceopen", () => {
    try {
      sourceBuffer = mediaSource.addSourceBuffer("audio/mpeg");
    } catch (e) {
      log("This browser doesn't support live audio/mpeg streaming via MediaSource (try Chrome/Edge).", "error");
      return;
    }
    sourceBuffer.addEventListener("updateend", flushPendingChunks);
    flushPendingChunks();
  });

  player.play().catch(() => {
    // Autoplay can be blocked until a user gesture has occurred; the
    // "Speak instruction" click itself counts as one, so this normally
    // succeeds. Left here so a blocked promise doesn't throw unhandled.
  });
}

function appendChunk(bytes) {
  pendingChunks.push(bytes);
  flushPendingChunks();
}

function flushPendingChunks() {
  if (!sourceBuffer || sourceBuffer.updating || pendingChunks.length === 0) return;
  const next = pendingChunks.shift();
  try {
    sourceBuffer.appendBuffer(next);
  } catch (e) {
    // Buffer full or in a bad state (e.g. right after an interrupt tore
    // it down) — safe to drop, playback is being cancelled anyway.
  }
}

function finalizeStream() {
  const tryEnd = () => {
    if (!mediaSource || mediaSource.readyState !== "open") return;
    if (sourceBuffer && sourceBuffer.updating) {
      sourceBuffer.addEventListener("updateend", tryEnd, { once: true });
      return;
    }
    try {
      mediaSource.endOfStream();
    } catch (e) {
      /* already ended/cancelled */
    }
  };
  tryEnd();
}

function base64ToBytes(b64) {
  const binStr = atob(b64);
  const bytes = new Uint8Array(binStr.length);
  for (let i = 0; i < binStr.length; i++) bytes[i] = binStr.charCodeAt(i);
  return bytes;
}

// ---------- Push-to-talk interrupt ----------

const SpeechRecognitionCtor = window.SpeechRecognition || window.webkitSpeechRecognition;

function armRecognition() {
  if (!SpeechRecognitionCtor) {
    log("SpeechRecognition isn't available in this browser (try Chrome/Edge).", "error");
    return null;
  }
  const r = new SpeechRecognitionCtor();
  r.lang = "en-US";
  r.interimResults = false;
  r.maxAlternatives = 1;
  r.onresult = (event) => {
    const transcript = event.results[0][0].transcript;
    sendInterrupt(transcript);
  };
  r.onerror = (event) => {
    log(`Speech recognition error: ${event.error}`, "error");
    if (awaitingFinalTranscript) sendInterrupt(""); // still send the hard-stop
  };
  r.onend = () => {
    recognizing = false;
  };
  return r;
}

function beginInterrupt() {
  if (interruptBtn.disabled || recognizing) return;
  // Hard stop first, unconditionally, before anything else.
  interruptElapsedAtPress = player.currentTime || 0;
  player.pause();
  try {
    mediaSource && mediaSource.readyState === "open" && mediaSource.endOfStream();
  } catch (e) {
    /* fine either way */
  }

  interruptBtn.classList.add("active");
  awaitingFinalTranscript = true;
  recognition = armRecognition();
  if (recognition) {
    recognizing = true;
    recognition.start();
    log(`Interrupt: audio paused at ${interruptElapsedAtPress.toFixed(2)}s. Listening...`);
  }
}

function endInterrupt() {
  interruptBtn.classList.remove("active");
  if (recognizing && recognition) {
    recognition.stop(); // triggers a final onresult
  }
}

function sendInterrupt(transcript) {
  awaitingFinalTranscript = false;
  ws.send(JSON.stringify({
    type: "interrupt",
    transcript,
    elapsed_seconds: interruptElapsedAtPress,
  }));
  log(`Sent interrupt transcript: "${transcript}"`);
}

interruptBtn.addEventListener("mousedown", beginInterrupt);
interruptBtn.addEventListener("mouseup", endInterrupt);
interruptBtn.addEventListener("mouseleave", () => {
  if (recognizing) endInterrupt();
});

document.addEventListener("keydown", (e) => {
  if (e.code === "Space" && !e.repeat) {
    e.preventDefault();
    beginInterrupt();
  }
});
document.addEventListener("keyup", (e) => {
  if (e.code === "Space") {
    e.preventDefault();
    endInterrupt();
  }
});

// ---------- Speak button ----------

speakBtn.addEventListener("click", () => {
  ws.send(JSON.stringify({
    type: "speak",
    text: instructionText.value.trim(),
  }));
});
