"""Conflict forecast — CDR (§12.7)."""

from __future__ import annotations

from app.planner.conflicts import forecast_conflicts
from app.railcore.running_time import RunningTimes
from tests.test_mvp_smoke import SETTINGS, WORLD, initial_plan, new_sim, snapshot

RTS = RunningTimes(WORLD)


def test_no_conflicts_while_the_plan_holds_and_some_after_a_failure():
    sim = new_sim()
    plan = initial_plan(sim)
    sim.set_plan(plan)
    for _ in range(6):
        sim.step(600)
        assert forecast_conflicts(snapshot(sim, plan), WORLD, RTS, SETTINGS) == []
    tid = next(t.train.id for t in sim.trains.values() if t.loc == "segment")
    sim.create_incident({"type": "train_failure", "train_id": tid, "est_min_min": 40, "est_max_min": 40})
    c = forecast_conflicts(snapshot(sim, plan), WORLD, RTS, SETTINGS)
    assert c and all(tid in x["trains"] or x["kind"] == "track" for x in c[:1])
    assert {x["kind"] for x in c} <= {"head_on", "following", "track"}
