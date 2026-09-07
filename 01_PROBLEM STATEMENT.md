# PROBLEM_STATEMENT.md

## Title

**DriveFocus: Interruption-Safe Voice Navigation for Drivers**

## Background

Voice navigation is one of the most mature voice-AI product categories in
existence, yet the core failure mode described below remains largely
unsolved in consumer systems: when something changes mid-instruction, most
systems handle the *stop* correctly but not the *recovery*. They either keep
speaking over the interruption, go silent and drop the instruction entirely,
repeat the full instruction from the start regardless of how much was
already heard, or resume a fragment that no longer makes grammatical or
contextual sense. Each of these failure modes adds cognitive load at exactly
the moment a driver's attention is most constrained.

## The user and the situation

**User:** A driver actively operating a vehicle, using turn-by-turn voice
guidance as the primary source of route information.

**Situation constraints that make this a voice-only problem:**
- The driver cannot safely divert visual attention to a screen while the
  vehicle is in motion.
- The driver's hands are occupied with vehicle control.
- Route-relevant information (a closed road, a new hazard, a driver's own
  verbal correction) can arrive at any moment, including mid-sentence during
  an active spoken instruction.
- There is no acceptable fallback modality. If voice fails here, the product
  has no secondary channel to fall back on — this is what makes the
  hackathon's "voice must be essential" requirement trivially satisfied by
  this problem, rather than something that has to be argued for.

## The problem, precisely stated

Existing voice navigation systems treat "interrupt" as a binary operation:
speech is either playing or stopped. What's missing is a **third state** —
an evaluation step between "stop" and "speak again" that answers:

1. **What did the driver actually hear** before the interruption occurred?
   Not "was audio stopped," but specifically *which words* reached the
   driver's ears before playback ended.
2. **Is the remaining, unheard content of the original instruction still
   true?** A route change, a driver's own verbal correction, or a detected
   hazard can invalidate part or all of an instruction that was still being
   spoken.
3. **Given (1) and (2), what should happen next?** The system must choose
   between four distinct recovery behaviors, not just "silence" or "restart":
   - **Discard** — the instruction is fully invalidated and nothing further
     should be said about it (a replacement isn't ready yet).
   - **Replace** — the instruction's intent still applies, but its content
     must change (e.g., a rerouted turn instead of the original one).
   - **Repeat** — the instruction is still valid, but so little of it was
     heard that resuming mid-sentence would be confusing or incomplete; say
     it again from the start.
   - **Resume** — the instruction is still valid and most of it was already
     heard; finish only the unheard remainder rather than repeating
     everything.

Making this decision correctly, in real time, using only Rime as the spoken
output, is the hard voice engineering problem this project solves.

## Illustrative example (from the original brief)

> **Assistant:** "In 300 metres, turn left onto—"
> **Driver:** "There is a roadblock ahead. Don't take that route."
> **System:** Immediately stops the current speech → determines the driver
> heard only "In 300 metres, turn left onto—" and nothing further →
> classifies the interruption as one that invalidates the instruction →
> generates a replacement route → speaks the new instruction via Rime.

This is the **REPLACE** case. It is the headline scenario, but it is not the
only case the system must handle correctly — see "Non-goals and edge cases"
below for why a system that only handles this one case would be
underspecified.

## Core objective

Build a voice-first navigation system in which:

- Rime provides the sole spoken output for every navigation instruction and
  every interruption-recovery response.
- A high-priority interrupt (driver speech or a detected hazard/route
  change) stops any currently playing Rime audio immediately — no audio
  generated after the interrupt should reach playback for the cancelled
  instruction.
- The system can determine, with word-level precision, what portion of the
  interrupted instruction the driver actually heard.
- The system applies a consistent, explainable rule set to choose between
  discard, replace, repeat, and resume — not an ad hoc or hardcoded response
  per scenario.
- The chosen recovery behavior is demonstrably correct and repeatable,
  measured via an acceptance test defined before the demo, not asserted
  after the fact.

## Non-goals and edge cases that must still be handled

A system that only correctly handles the roadblock example above is
underspecified, because it leaves two scenario types unaddressed that any
credible implementation must also get right:

1. **Ambient, non-route-relevant interruptions.** A driver asking "what's my
   ETA?" or "turn down the volume" is still an interruption, but it does not
   invalidate the in-progress instruction. A system that treats every
   interruption as REPLACE-worthy will unnecessarily discard or repeat valid
   navigation information, adding cognitive load rather than reducing it —
   the opposite of the product's goal.
2. **Late-stage interruptions on long instructions.** If an instruction is
   almost finished when interrupted, restarting it from the beginning
   (REPEAT) is worse than finishing the small remainder (RESUME). The
   decision logic must distinguish "interrupted early" from "interrupted
   late," not just "interrupted vs. not."

Explicitly out of scope for this build:
- Real speech-to-text accuracy for driver commands (a scripted/simulated
  input stands in for this, disclosed openly — see the project README).
- Real hazard detection or live routing/map computation (replacement
  instruction text is pre-authored per scenario, not computed from a live
  routing engine).
- General-purpose conversational ability beyond the navigation domain.

## Success criteria

A submission is successful if, for a defined and reproducible test
scenario, it can show:

1. A measured, low-latency stop of Rime audio playback the moment a
   high-priority interrupt occurs.
2. A verifiable, word-level account of what was heard vs. not heard before
   the interrupt.
3. Consistent, correct classification into discard / replace / repeat /
   resume across multiple trials — not just the single headline example.
4. No instance, across repeated trials, of stale or outdated audio reaching
   playback after a cancellation.
5. Honest disclosure of which parts of the system are live (the Rime
   pipeline and decision engine) versus simulated (driver speech input and
   hazard/route data).

## Relationship to the hackathon's "hard voice problems" list

This problem statement is a direct, elaborated instance of the officially
listed **"Interruption and recovery"** category: stopping queued TTS
promptly, fencing obsolete results so they cannot re-enter the conversation,
and keeping application state consistent with what the user actually heard.
DriveFocus adds one layer of precision beyond the category's baseline
description — using Rime's word-level timestamp data to make the
heard/unheard distinction exact rather than approximate — which is the
project's specific technical contribution within that category.
