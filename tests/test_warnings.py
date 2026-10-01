"""Warnings (temporary speed restrictions) and the dispatcher's direction command (realism step 6)."""

from __future__ import annotations

import pytest

from app.common.config import load_settings
from app.field.incidents import IncidentError
from app.field.sim import FieldSim
from app.railcore.infra import get_world
from app.railcore.models import Direction
from app.railcore.physics import Restriction
from app.planner.snapshot import build_snapshot
from app.railcore.problem import build_tasks
from app.railcore.running_time import RunningTimes

WORLD = get_world()
SETTINGS = load_settings()
WARN = {"type": "speed_restriction", "segment_id": "SEV-R1", "est_min_min": 120, "est_max_min": 120,
        "params": {"km_from": 3, "km_to": 6, "v_kmh": 40}}


def _odd_freight():
    return next(t.id for t in WORLD.timetable if t.direction == Direction.ODD and t.category == "freight")


def _sim(placements):
    sim = FieldSim(WORLD, RunningTimes(WORLD), SETTINGS)
    for tid, loc, idx, progress in placements:
        tr = sim.trains[tid]
        tr.loc, tr.idx, tr.progress, tr.arrived_at, tr.stopped = loc, idx, progress, 0.0, True
    for tr in sim.trains.values():
        if tr.train.id not in {p[0] for p in placements}:
            tr.loc = "done"
    return sim


def test_restriction_slows_the_running_time():
    rts = RunningTimes(WORLD)
    free = rts.get("passenger", "SEV-R1", Direction.ODD)
    slow = rts.get("passenger", "SEV-R1", Direction.ODD, None, (Restriction(3000, 6000, 40),))
    assert slow.t_pp > free.t_pp + 120, "3 km at 40 instead of 120 km/h costs minutes"


def test_bad_warning_is_rejected():
    sim = _sim([])
    with pytest.raises(IncidentError):
        sim.create_incident({**WARN, "params": {"km_from": 3, "km_to": 40, "v_kmh": 40}})  # beyond the segment
    with pytest.raises(IncidentError):
        sim.create_incident({**WARN, "params": {"km_from": 3, "km_to": 6, "v_kmh": 0}})


def test_field_train_never_exceeds_the_warning_and_state_shows_it():
    tid = _odd_freight()
    sim = _sim([(tid, "segment", 0, 0.1)])  # SEV-R1 at 1.5 km, odd
    sim.ab.dirs["SEV-R1"].direction = Direction.ODD
    sim.create_incident(WARN)
    tr, seg = sim.trains[tid], WORLD.segments["SEV-R1"]
    length = WORLD.categories["freight"].length_m
    seen_in_zone = False
    for _ in range(900):
        sim.step(1)
        if tr.loc != "segment":
            break
        head = tr.progress * seg.length_m
        if head > 3000 and head - length < 6000:
            seen_in_zone = True
            assert tr.speed_kmh <= 40 + 1e-6
    assert seen_in_zone
    assert sim.safety_violations == 0
    w = sim.snapshot()["warnings"]
    assert w and w[0]["segment_id"] == "SEV-R1" and w[0]["v_kmh"] == 40 and w[0]["from_m"] == 3000


def test_planner_uses_slower_times_while_the_warning_lasts():
    sim = FieldSim(WORLD, RunningTimes(WORLD), SETTINGS)
    rts = RunningTimes(WORLD)
    base = {t.train_id: t for t in build_tasks(build_snapshot(sim.snapshot(), WORLD.timetable, [], None),
                                               WORLD, rts, SETTINGS)}
    inc = sim.create_incident(WARN)
    warned = {t.train_id: t for t in build_tasks(build_snapshot(sim.snapshot(), WORLD.timetable, [inc], None),
                                                 WORLD, rts, SETTINGS)}
    tid = next(t for t, task in base.items() if task.legs and task.legs[0].segment_id == "SEV-R1")
    assert warned[tid].legs[0].t_pp > base[tid].legs[0].t_pp


def test_dispatcher_direction_command():
    tid = _odd_freight()
    sim = _sim([(tid, "segment", 0, 0.5)])
    sim.ab.dirs["SEV-R1"].direction = Direction.ODD
    with pytest.raises(IncidentError):
        sim.set_direction("SEV-R1", Direction.EVEN)  # occupied
    sim = _sim([])
    sim.ab.dirs["SEV-R1"].direction = Direction.ODD
    sim.set_direction("SEV-R1", Direction.EVEN)
    sim.step(SETTINGS["intervals"]["direction_change_s"] + 5)
    assert sim.ab.dirs["SEV-R1"].direction == Direction.EVEN
    sim.step(300)
    assert sim.ab.dirs["SEV-R1"].direction == Direction.EVEN, "the automatics do not turn it back"
    assert sim.snapshot()["directions"]["SEV-R1"]["manual"] is True
