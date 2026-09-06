"""
main.py
=======
Demo entry point.

    python main.py                    # live Rime (/ws3), requires RIME_API_KEY
    python main.py --dry-run          # simulated Rime, no network/API key needed
    python main.py --scenario roadblock_replace

Prints, for each scenario:
    - which speech provider is active (live Rime vs simulated) — required
      disclosure per the eligibility rules ("make the active speech
      provider observable")
    - the measured stop latency (interrupt signal -> audio halted)
    - the word-level heard/unheard breakdown
    - the chosen recovery action and the rule that produced it
    - the final text spoken (if any)
    - where the generated audio was saved, when running live (backend="real")
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

from decision_engine import RecoveryAction
from orchestrator import DriveFocusSession
from scenarios import SCENARIOS


def _print_header(text: str):
    print("\n" + "=" * 78)
    print(text)
    print("=" * 78)


async def run_scenario(session: DriveFocusSession, scenario) -> bool:
    _print_header(f"SCENARIO: {scenario.name}")
    print(f"Proves:        {scenario.requirement}")
    print(f"Instruction:   \"{scenario.instruction_text}\"")
    print(f"Interrupt at:  {scenario.interrupt_at_seconds:.1f}s into playback")
    print(f"Interrupt src: {scenario.interrupt_source} — \"{scenario.interrupt_transcript}\"")

    result = await session.speak_with_possible_interrupt(
        text=scenario.instruction_text,
        interrupt_at_seconds=scenario.interrupt_at_seconds,
        interrupt_source=scenario.interrupt_source,
        interrupt_transcript=scenario.interrupt_transcript,
        replacement_text=scenario.replacement_text,
    )

    pb = result.playback
    print(f"\nHeard:         {pb.heard_word_count}/{pb.total_word_count} words "
          f"({(pb.heard_word_count / pb.total_word_count * 100) if pb.total_word_count else 0:.0f}%)")
    print(f"  heard text:    \"{pb.heard_text}\"")
    print(f"  unheard text:  \"{pb.unheard_text}\"")
    if pb.audio_file_path:
        print(f"  audio saved:   {pb.audio_file_path}")
    if result.stop_latency_seconds is not None:
        print(f"Stop latency:  {result.stop_latency_seconds * 1000:.1f} ms "
              f"(interrupt signal -> audio halted)")

    decision = result.decision
    passed = True
    if decision is not None:
        print(f"\nDecision:      {decision.action.value}")
        print(f"  reason:        {decision.reason}")
        print(f"Final spoken:  \"{result.final_spoken_text}\"" if result.final_spoken_text
              else "Final spoken:  (nothing — DISCARD)")
        passed = decision.action == scenario.expected_action
        status = "PASS" if passed else "FAIL"
        print(f"\nExpected action: {scenario.expected_action.value}  ->  {status}")
    return passed


async def main_async(args):
    if args.dry_run:
        from simulated_rime import SimulatedRimeSession
        factory = lambda: SimulatedRimeSession()
        backend = "simulated"
        print("Speech provider: SIMULATED (no network call, disclosed per README.md)")
    else:
        from rime_client import RimeTTSSession
        speaker = os.environ.get("RIME_SPEAKER", "astra")
        model_id = os.environ.get("RIME_MODEL_ID", "mistv2")
        factory = lambda: RimeTTSSession(speaker=speaker, model_id=model_id)
        backend = "real"
        print(f"Speech provider: LIVE Rime (/ws3), speaker={speaker}, modelId={model_id}")
        print("Generated audio will be saved under ./drivefocus_audio_out/ "
              "and played automatically if a system player is found.")

    session = DriveFocusSession(rime_session_factory=factory, backend=backend)

    to_run = SCENARIOS
    if args.scenario:
        to_run = [s for s in SCENARIOS if s.name == args.scenario]
        if not to_run:
            print(f"No such scenario: {args.scenario}")
            print("Available: " + ", ".join(s.name for s in SCENARIOS))
            sys.exit(1)

    results = []
    for scenario in to_run:
        results.append(await run_scenario(session, scenario))

    _print_header("SUMMARY")
    passed = sum(results)
    print(f"{passed}/{len(results)} scenarios matched their expected recovery action.")
    if not all(results):
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="DriveFocus demo runner")
    parser.add_argument("--dry-run", action="store_true",
                         help="Use the simulated Rime backend (no network/API key required)")
    parser.add_argument("--scenario", type=str, default=None,
                         help="Run only the named scenario")
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
