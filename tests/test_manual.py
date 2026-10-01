"""The dispatcher's instructions from the train graph (tasks/02-manual-drag-backend.md §6)."""

from __future__ import annotations

import time

import pytest

from app.planner.manual import ManualError, bounds, make_pin, preview
from app.planner.pool import forecast_plan, solve_job
from app.railcore.models import Plan
from tests.test_mvp_smoke import SETTINGS, WORLD, initial_plan, new_sim, snapshot
from app.railcore.running_time import RunningTimes

RTS = RunningTimes(WORLD)


@pytest.fixture(scope="module")
def ctx():
    sim = new_sim()
    plan = initial_plan(sim)
    sim.set_plan(plan)
    sim.step(1800)
    snap = snapshot(sim, plan)
    return sim, plan, snap, forecast_plan(snap, SETTINGS)


def dwells(plan: Plan, tid: str):
    return [e for e in plan.entries if e.kind == "dwell" and e.train_id == tid]


def future_point(plan: Plan, now: float, tid: str, stop: bool | None = None):
    ds = dwells(plan, tid)
    return next(e for e in ds[1:-1] if e.start > now + 300 and (stop is None or e.stop == stop))


def passenger_with_stop(plan: Plan, now: float):
    for tid in sorted({e.train_id for e in plan.entries}):
        if WORLD.trains[tid].category == "freight":
            continue
        for e in dwells(plan, tid)[1:-1]:
            st = next(s for s in WORLD.trains[tid].stops if s.station_id == e.station_id)
            if st.stop and e.start > now + 300:
                return tid, e, st
    raise AssertionError("no passenger stop ahead")


def test_bounds_rules(ctx):
    sim, plan, snap, _ = ctx
    tid, e, st = passenger_with_stop(plan, snap.now)
    b = bounds(snap, WORLD, RTS, SETTINGS, plan, tid, e.station_id)
    assert b["point_type"] == "stop"
    assert b["dep"]["min"] >= st.dep - 1, "a passenger train never leaves before the timetable"
    assert b["dep"]["min"] >= e.start + WORLD.categories[WORLD.trains[tid].category].min_dwell_s - 1
    prev = dwells(plan, tid)[[d.station_id for d in dwells(plan, tid)].index(e.station_id) - 1]
    assert b["arr"]["min"] >= prev.end + b["info"]["min_run_s"] - 1
    assert b["arr"]["max"] <= prev.end + b["info"]["min_run_s"] * SETTINGS["manual"]["max_run_factor"] + 1
    ds = dwells(plan, "2003")
    origin = bounds(snap, WORLD, RTS, SETTINGS, plan, "2003", ds[0].station_id)
    dest = bounds(snap, WORLD, RTS, SETTINGS, plan, "2003", ds[-1].station_id)
    assert origin["arr"] is None and origin["point_type"] == "origin"
    assert dest["dep"] is None and dest["point_type"] == "destination"


def test_bounds_of_a_train_on_the_line_and_past_events(ctx):
    sim, plan, snap, _ = ctx
    on_line = next(s for s in snap.states.values() if s.segment_id)
    b = bounds(snap, WORLD, RTS, SETTINGS, plan, on_line.train_id, on_line.next_station_id)
    assert b["arr"]["min"] >= snap.now and not b["arr"]["locked"]
    route = [s.station_id for s in WORLD.trains[on_line.train_id].stops]
    behind = route[route.index(on_line.next_station_id) - 1]
    with pytest.raises(ManualError) as e:
        bounds(snap, WORLD, RTS, SETTINGS, plan, on_line.train_id, behind)
    assert e.value.code == 423


def test_preview_departure_right_moves_the_rest_of_the_thread_at_the_same_speed(ctx):
    sim, plan, snap, fc = ctx
    e = future_point(plan, snap.now, "2003")
    r = preview(snap, WORLD, RTS, SETTINGS, plan, fc, "2003", e.station_id, "dep", e.end + 600)
    assert not r["conflicts"]
    new = r["plan"]
    old_runs = {x.segment_id: x.end - x.start for x in plan.entries if x.kind == "run" and x.train_id == "2003"}
    later = False
    for x in new.entries:
        if x.train_id == "2003" and x.kind == "run" and x.start >= r["time"] - 1:
            later = True
            assert x.end - x.start >= old_runs[x.segment_id] - 1, "the train does not run faster after the point"
    assert later
    d = next(x for x in dwells(new, "2003") if x.station_id == e.station_id)
    assert abs(d.end - r["time"]) <= 1 and d.stop
    if not e.stop:
        assert d.unplanned, "a pass turned into a stop is unplanned"
    # nobody else moves earlier than in the current plan
    base = {(x.train_id, x.station_id): x.start for x in fc.entries if x.kind == "dwell"}
    for x in new.entries:
        if x.kind == "dwell" and x.train_id != "2003" and (x.train_id, x.station_id) in base:
            assert x.start >= base[(x.train_id, x.station_id)] - 1
    assert r["compute_ms"] <= 100


def test_preview_arrival_right_keeps_departure_while_the_dwell_allows(ctx):
    sim, plan, snap, fc = ctx
    tid, e, _ = passenger_with_stop(plan, snap.now)
    b = bounds(snap, WORLD, RTS, SETTINGS, plan, tid, e.station_id)
    slack = e.end - e.start - b["info"]["min_dwell_s"]
    r = preview(snap, WORLD, RTS, SETTINGS, plan, fc, tid, e.station_id, "arr", min(b["arr"]["max"], e.start + 600))
    d = next(x for x in dwells(r["plan"], tid) if x.station_id == e.station_id)
    assert abs(d.start - r["time"]) <= 1
    if r["time"] - e.start <= slack:
        assert abs(d.end - e.end) <= 1, "departure stays while the dwell is long enough"
    else:
        assert d.end >= r["time"] + b["info"]["min_dwell_s"] - 1
    prev_dep = r["dragged"]["arr"] - r["dragged"]["prev_run_s"]
    assert abs(r["dragged"]["prev_run_s"] - (r["time"] - prev_dep)) <= 1


def test_preview_clamps_to_bounds(ctx):
    sim, plan, snap, fc = ctx
    e = future_point(plan, snap.now, "2003")
    r = preview(snap, WORLD, RTS, SETTINGS, plan, fc, "2003", e.station_id, "dep", snap.now - 3600)
    assert r["clamped"] and r["time"] >= snap.now


def test_cpsat_keeps_a_pin_through_an_incident(ctx):
    sim, plan, snap, _ = ctx
    e = future_point(plan, snap.now, "2003")
    pin = make_pin(WORLD, "2003", e.station_id, "dep", round(e.end + 900), snap.now, e.end)
    s2 = snap.model_copy(update={"pins": [pin], "incidents": [sim.create_incident(
        {"type": "obstacle", "segment_id": "R2-OZR", "est_min_min": 15, "est_max_min": 30})]})
    t0 = time.perf_counter()
    for strategy in ("balanced", "robust", "reoptimize"):
        res = solve_job(s2.model_dump(mode="json"), strategy, SETTINGS)
        assert res["status"] in ("OPTIMAL", "FEASIBLE", "KEPT_CURRENT_ORDER"), f"{strategy}: {res['status']}"
        p = Plan.model_validate(res["plan"])
        assert p.solver == "cpsat", "the pin must not break the model (a fallback keeps the pin only by chance)"
        d = next(x for x in dwells(p, "2003") if x.station_id == e.station_id)
        assert abs(d.end - pin.time) <= 1, strategy
        assert p.pins and p.pins[0].id == pin.id
    assert time.perf_counter() - t0 < 15


def test_pin_is_kept_by_cpsat_when_an_incident_delays_the_crossing_train():
    """Regression (live run): an obstacle delays express 102, which crosses 2003 on R1-STP; keeping the
    instruction means 102 waits at STP — CP-SAT must find that (it used to fail with MODEL_INVALID)."""
    sim = new_sim()
    plan = initial_plan(sim)
    sim.set_plan(plan)
    sim.step(1230)
    snap = snapshot(sim, plan)
    b = bounds(snap, WORLD, RTS, SETTINGS, plan, "2003", "R1")
    r = preview(snap, WORLD, RTS, SETTINGS, plan, forecast_plan(snap, SETTINGS), "2003", "R1", "dep", b["dep"]["current"] + 900)
    sim.set_plan(r["plan"])
    sim.create_incident({"type": "obstacle", "segment_id": "R2-OZR", "est_min_min": 15, "est_max_min": 30})
    s2 = snapshot(sim, r["plan"]).model_copy(update={"pins": [r["pin"]]})
    for strategy in ("balanced", "robust", "passenger_first"):
        res = solve_job(s2.model_dump(mode="json"), strategy, SETTINGS)
        p = Plan.model_validate(res["plan"])
        assert p.solver == "cpsat", f"{strategy}: {res['status']}"
        d = next(x for x in dwells(p, "2003") if x.station_id == "R1")
        assert abs(d.end - r["pin"].time) <= 60, strategy


def test_impossible_pin_gives_a_plan_not_a_failure(ctx):
    sim, plan, snap, _ = ctx
    e = future_point(plan, snap.now, "2003")
    pin = make_pin(WORLD, "2003", e.station_id, "dep", snap.now + 1, snap.now, e.end)  # it cannot be there yet
    p = Plan.model_validate(solve_job(snap.model_copy(update={"pins": [pin]}).model_dump(mode="json"), "balanced", SETTINGS)["plan"])
    d = next(x for x in dwells(p, "2003") if x.station_id == e.station_id)
    assert d.end > pin.time + 60, "the plan deviates; the service then marks the instruction violated"
