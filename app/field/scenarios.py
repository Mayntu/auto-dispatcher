"""Scenarios (CLAUDE.md §9.5): timed incident steps run by the field service, relative to the moment of start.

Steps: `incident` (create), `update` (new estimate of the step `ref`), `resolve` (clear the step `ref`).
`train_id: "auto"` — a train on the line now (freight first); `"auto_station"` — a train standing at a station.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field

from app.common.config import get_env


def load_scenarios() -> list[dict]:
    path = get_env().data_dir / "scenarios.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


@dataclass
class ScenarioRun:
    id: str
    scenario_id: str
    started_at: float
    steps: list[dict]
    done: int = 0
    refs: dict[int, str] = field(default_factory=dict)  # step index -> incident id
    log: list[dict] = field(default_factory=list)


def start(scenario: dict, now: float) -> ScenarioRun:
    steps = sorted(scenario["steps"], key=lambda s: s["at_min"])
    return ScenarioRun(uuid.uuid4().hex[:8], scenario["id"], now, steps)


def pick_train(sim, how: str) -> str | None:
    """A train on the line (freight first) or standing at a station, for scenario steps that need one."""
    want = "segment" if how == "auto" else "station"
    cands = [t for t in sim.trains.values() if t.loc == want and (want == "segment" or t.idx < len(t.route) - 1)]
    cands.sort(key=lambda t: (t.train.category != "freight", t.train.id))
    return cands[0].train.id if cands else None
