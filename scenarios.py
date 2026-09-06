"""
scenarios.py
============
Fixture scenarios. Each maps directly to a requirement in
PROBLEM_STATEMENT.md so the acceptance test (and the demo) can point at
exactly which requirement each trial proves.

Per the problem statement's own framing: "a system that only correctly
handles the roadblock example above is underspecified" — so this file
does not stop at the headline REPLACE case.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from decision_engine import RecoveryAction


@dataclass
class Scenario:
    name: str
    requirement: str                      # which part of the problem statement this proves
    instruction_text: str
    interrupt_at_seconds: float           # when, in playback, the interrupt lands
    interrupt_source: str                 # "driver_speech" | "hazard_detector" | "route_engine"
    interrupt_transcript: str
    replacement_text: Optional[str]       # None => no replacement ready yet
    expected_action: RecoveryAction


SCENARIOS: list[Scenario] = [
    Scenario(
        name="roadblock_replace",
        requirement="Illustrative example (REPLACE) — headline scenario",
        instruction_text="In 300 metres, turn left onto Anna Salai and continue toward the flyover",
        interrupt_at_seconds=1.6,   # lands after "...turn left onto—"
        interrupt_source="driver_speech",
        interrupt_transcript="There is a roadblock ahead, don't take that route",
        replacement_text="Okay, rerouting — in 200 metres, continue straight onto Mount Road instead",
        expected_action=RecoveryAction.REPLACE,
    ),
    Scenario(
        name="hazard_no_replacement_yet_discard",
        requirement="Non-goals: content invalidated but no replacement instruction is ready",
        instruction_text="In 500 metres, take the second exit at the roundabout onto Cathedral Road",
        interrupt_at_seconds=0.8,
        interrupt_source="hazard_detector",
        interrupt_transcript="hazard: stalled vehicle detected on current route",
        replacement_text=None,   # rerouting still in progress
        expected_action=RecoveryAction.DISCARD,
    ),
    Scenario(
        name="ambient_eta_query_early_repeat",
        requirement="Non-goal 1 (ambient interrupt) + Non-goal 2 (interrupted early -> REPEAT)",
        instruction_text="In 400 metres, merge onto the Chennai bypass and stay in the left lane",
        interrupt_at_seconds=0.5,   # very early — little of the instruction heard
        interrupt_source="driver_speech",
        interrupt_transcript="what's my ETA?",
        replacement_text=None,
        expected_action=RecoveryAction.REPEAT,
    ),
    Scenario(
        name="ambient_volume_late_resume",
        requirement="Non-goal 1 (ambient interrupt) + Non-goal 2 (interrupted late -> RESUME)",
        instruction_text="Continue straight for two kilometres then take the exit toward T Nagar",
        interrupt_at_seconds=3.4,   # most of the instruction already heard
        interrupt_source="driver_speech",
        interrupt_transcript="turn down the volume",
        replacement_text=None,
        expected_action=RecoveryAction.RESUME,
    ),
    Scenario(
        name="driver_correction_replace",
        requirement="DRIVER_CORRECTION category is treated as content-invalidating, like a hazard",
        instruction_text="In 200 metres, turn right onto Nungambakkam High Road",
        interrupt_at_seconds=0.6,
        interrupt_source="driver_speech",
        interrupt_transcript="no, turn left instead, I need to stop by the pharmacy",
        replacement_text="Understood — in 200 metres, turn left onto Nungambakkam High Road for the pharmacy stop",
        expected_action=RecoveryAction.REPLACE,
    ),
]
