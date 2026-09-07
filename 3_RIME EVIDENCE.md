# RIME_EVIDENCE.md

## Hard voice claim

DriveFocus resolves the "interrupt" ambiguity that most voice-nav systems
leave unsolved: given a mid-instruction interruption, it determines
**with word-level precision** what the driver already heard, and applies
a consistent rule (not a per-scenario hack) to choose one of four
recovery behaviors — **discard, replace, repeat, resume** — such that:

1. No audio generated after the interrupt reaches playback for the
   cancelled instruction (hard stop).
2. The heard/unheard split is measured from Rime's own word-level
   timestamp data, not estimated.
3. The same classification rule set handles the headline REPLACE case
   *and* the two edge cases the problem statement calls out explicitly
   (ambient, non-invalidating interrupts; early- vs. late-stage
   interrupts on the same instruction) — without scenario-specific code.

## Acceptance test

**Defined in:** `test_acceptance.py` (committed before this evidence
file was finalized against results).

**Procedure:**
1. For each of 5 scenarios in `scenarios.py` (one per requirement in
   `PROBLEM_STATEMENT.md` — see each scenario's `requirement` field),
   speak the instruction and inject an interrupt at a fixed point in
   playback.
2. Repeat each scenario 5 times (`TRIALS_PER_SCENARIO = 5`).
3. Assert:
   - the chosen recovery action is identical across all 5 trials, and
     matches the scenario's `expected_action` (success criterion 3),
   - `heard_text + unheard_text` reconstructs the original instruction
     word-for-word, with no duplication or loss (success criterion 2),
   - a stop latency is recorded and stays under a fixed bound (success
     criterion 1),
   - the number of words whose audio reached the playback controller
     never exceeds the number reported as heard at interrupt time, i.e.
     no stale audio slips through after cancellation (success
     criterion 4).
4. Separately assert that every ambient-interrupt scenario resolves to
   REPEAT or RESUME, never REPLACE or DISCARD (non-goal 1).

**Run it yourself:**
```bash
pip install pytest
pytest test_acceptance.py -v
```

## Result (measured on the simulated backend, 5 scenarios × 5 trials = 25 runs)

| Scenario | Requirement proven | Expected | Observed (5/5 trials) |
|---|---|---|---|
| `roadblock_replace` | Headline REPLACE example | REPLACE | REPLACE, consistent |
| `hazard_no_replacement_yet_discard` | Content invalidated, no replacement ready | DISCARD | DISCARD, consistent |
| `ambient_eta_query_early_repeat` | Ambient interrupt + interrupted early | REPEAT | REPEAT, consistent |
| `ambient_volume_late_resume` | Ambient interrupt + interrupted late | RESUME | RESUME, consistent |
| `driver_correction_replace` | Driver correction treated as invalidating | REPLACE | REPLACE, consistent |

Sample run (`python main.py --dry-run`, abridged):

```
SCENARIO: roadblock_replace
Instruction:   "In 300 metres, turn left onto Anna Salai and continue toward the flyover"
Interrupt at:  1.6s into playback
Heard:         5/13 words (38%)
  heard text:    "In 300 metres, turn left"
  unheard text:  "onto Anna Salai and continue toward the flyover"
Stop latency:  0.0 ms
Decision:      REPLACE
Final spoken:  "Okay, rerouting — in 200 metres, continue straight onto Mount Road instead"
Expected action: REPLACE  ->  PASS

SCENARIO: ambient_volume_late_resume
Instruction:   "Continue straight for two kilometres then take the exit toward T Nagar"
Interrupt at:  3.4s into playback
Heard:         7/12 words (58%)
  heard text:    "Continue straight for two kilometres then take"
  unheard text:  "the exit toward T Nagar"
Decision:      RESUME
Final spoken:  "the exit toward T Nagar"
Expected action: RESUME  ->  PASS

SUMMARY: 5/5 scenarios matched their expected recovery action.
```

Word-level heard/unheard reconstruction was verified exact (no dropped
or duplicated words) for all 25 trial runs; stop latency stayed at
effectively 0 ms because the hard-stop check (`mark_interrupt_signal()`
-> `RimeTTSSession.cancel()` -> `PlaybackController.stop()`) runs
synchronously with no intervening I/O, so the only latency floor is
Python call overhead.

## Limitations

- These results were generated with `simulated_rime.py` (see README's
  "What's live vs. simulated" table) so the suite is deterministic and
  runs without a live network dependency in CI. The same test bodies run
  unmodified against live Rime by swapping the session factory to
  `RimeTTSSession` — the decision/classification logic under test does
  not change between the two, only the audio transport does.
- Stop-latency numbers above measure the *decision-to-cancel* path
  (`cancel()` call to `stop()` return), not end-to-end audio-device
  silence, since this reference implementation does not include a
  hardware audio sink (see README limitations).
- The interrupt classifier is intentionally a small, auditable
  keyword-based gate rather than an LLM — its known blind spots are
  documented in `interrupt_classifier.py` and README.md.
