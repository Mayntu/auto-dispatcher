"""Automatic block in the planner and the field (CLAUDE.md §12.2, §12.5; tasks/01-realism step 5 DoD)."""

from __future__ import annotations

from collections import defaultdict

from app.common.config import load_settings
from app.field.sim import FieldSim
from app.railcore.infra import get_world
from app.railcore.models import Direction, Plan, PlanEntry
from app.railcore.running_time import RunningTimes

WORLD = get_world()
SETTINGS = load_settings()


def test_timetable_uses_packets():
    """With automatic block followers share a segment: the normative timetable has such pairs."""
    runs = defaultdict(list)
    for t in WORLD.timetable:
        for a, b in zip(t.stops, t.stops[1:]):
            runs[WORLD.segment_between(a.station_id, b.station_id).id].append((a.dep, b.arr, t.direction))
    packets = sum(1 for rs in runs.values() for x, (s1, e1, d1) in enumerate(rs) for s2, e2, d2 in rs[x + 1:]
                  if d1 == d2 and s2 < e1 and s1 < e2)
    assert packets >= 5, f"only {packets} packet pairs"


def _sim(placements):
    sim = FieldSim(WORLD, RunningTimes(WORLD), SETTINGS)
    for tid, loc, idx, progress in placements:
        tr = sim.trains[tid]
        tr.loc, tr.idx, tr.progress, tr.arrived_at, tr.stopped = loc, idx, progress, 0.0, True
    for tr in sim.trains.values():
        if tr.train.id not in {p[0] for p in placements}:
            tr.loc = "done"
    return sim


def _odd_freight():
    return next(t.id for t in WORLD.timetable if t.direction == Direction.ODD and t.category == "freight")


def test_field_holds_train_at_siding_entry_for_tau_np():
    tid = _odd_freight()
    sim = _sim([(tid, "segment", 0, 0.97)])  # SEV-R1, 450 m before Рзд. 1
    sim.ab.dirs["SEV-R1"].direction = Direction.ODD
    sim._last_arrival[("R1", "even")] = sim.now  # an oncoming train has just been received at R1
    sim.step(60)
    tr = sim.trains[tid]
    assert tr.loc == "segment", "held at the entry signal while tau_np runs"
    sim.step(SETTINGS["intervals"]["tau_np_s"] + 60)
    assert tr.loc == "station" and tr.route[tr.idx] == "R1"
    assert sim.safety_violations == 0


def test_field_puts_train_on_the_track_the_plan_assigned():
    tid = _odd_freight()
    sim = _sim([(tid, "segment", 0, 0.97)])
    sim.ab.dirs["SEV-R1"].direction = Direction.ODD
    plan = Plan.model_construct(version=1, base_version=None, created_at=0, horizon_end=7200, meetings=[], entries=[
        PlanEntry(train_id=tid, kind="run", segment_id="SEV-R1", station_id=None, track_id=None,
                  start=-600, end=100, stop=False),
        PlanEntry(train_id=tid, kind="dwell", segment_id=None, station_id="R1", track_id="2",
                  start=100, end=900, stop=True)])
    sim.set_plan(plan)
    sim.step(300)
    tr = sim.trains[tid]
    assert tr.loc == "station" and tr.track_id == "2"
