"""train_delay, signal_failure, estimate update (backend-issues 3, 5)."""

from __future__ import annotations

import pytest

from app.field.incidents import IncidentError
from app.planner.pool import solve_job
from app.railcore.models import Plan
from tests.test_mvp_smoke import SETTINGS, WORLD, initial_plan, new_sim, snapshot


def dwell(plan: Plan, tid: str, st: str):
    return next(e for e in plan.entries if e.kind == "dwell" and e.train_id == tid and e.station_id == st)


def test_train_delay_holds_the_train_in_field_and_plan():
    sim = new_sim()
    plan = initial_plan(sim)
    sim.set_plan(plan)
    tid = "2003"
    origin = WORLD.trains[tid].stops[0]
    sim.step(origin.dep - 400)  # standing at its origin
    inc = sim.create_incident({"type": "train_delay", "train_id": tid, "est_min_min": 20, "est_max_min": 20,
                               "actual_min": 20})
    assert inc.station_id == origin.station_id
    p = Plan.model_validate(solve_job(snapshot(sim, plan).model_dump(mode="json"), "balanced", SETTINGS)["plan"])
    assert dwell(p, tid, origin.station_id).end >= inc.started_at + 20 * 60 - 1
    sim.set_plan(p)
    sim.step(20 * 60 - 60)
    tr = sim.trains[tid]
    assert tr.loc == "station" and tr.idx == 0, "still held"
    sim.step(1800)
    assert tr.idx > 0 or tr.loc == "segment"
    assert sim.safety_violations == 0


def test_signal_failure_stops_trains_180_s():
    sim = new_sim()
    plan = initial_plan(sim)
    sim.set_plan(plan)
    sim.step(600)
    inc = sim.create_incident({"type": "signal_failure", "station_id": "STP", "direction": "even",
                               "est_min_min": 60, "est_max_min": 60, "actual_min": 60})
    assert next(s for s in sim.signals() if s["id"] == "STP-even-exit")["aspect"] == "invitation"
    p = Plan.model_validate(solve_job(snapshot(sim, plan).model_dump(mode="json"), "balanced", SETTINGS)["plan"])
    affected = [e for e in p.entries if e.kind == "dwell" and e.station_id == "STP"
                and WORLD.trains[e.train_id].direction.value == "even" and e.start < inc.started_at + 3600
                and e.train_id in {t.id for t in WORLD.timetable} and WORLD.trains[e.train_id].stops[-1].station_id != "STP"]
    assert affected and all(e.stop and e.end - e.start >= 179 for e in affected)
    with pytest.raises(IncidentError):
        sim.create_incident({"type": "signal_failure", "station_id": "SEV", "direction": "even", "est_min_min": 10,
                             "est_max_min": 10})


def test_estimate_update_moves_the_truth_into_the_new_range():
    sim = new_sim()
    inc = sim.create_incident({"type": "obstacle", "segment_id": "R1-STP", "est_min_min": 15, "est_max_min": 30,
                               "actual_min": 28})
    upd = sim.update_estimate(inc.id, {"est_min_min": 5, "est_max_min": 10})
    assert (upd.est_min_s, upd.est_max_s) == (300, 600)
    assert sim.incidents[inc.id].actual_s == 600
    with pytest.raises(IncidentError):
        sim.update_estimate("nope", {"est_min_min": 5, "est_max_min": 10})
