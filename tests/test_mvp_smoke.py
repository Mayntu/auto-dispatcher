"""MVP smoke tests (MVP.md §6): never two trains on a segment, variants within 5 s, end-to-end headless run."""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict

from fastapi.testclient import TestClient

from app.common.config import load_settings
from app.field.sim import FieldSim
from app.planner.pool import SolverPool, forecast_plan, solve_job
from app.planner.snapshot import build_snapshot
from app.planner.strategies import pick_strategies
from app.planner.variants import generate_variants
from app.railcore.eco import eco_profile
from app.railcore.infra import get_world
from app.railcore.models import Direction, Plan
from app.railcore.running_time import RunningTimes

SETTINGS = load_settings()
WORLD = get_world()
OBSTACLE = {"type": "obstacle", "segment_id": "R1-STP", "km": 24.5, "est_min_min": 15, "est_max_min": 30,
            "actual_min": 22}


def assert_conflict_free(plan: Plan) -> None:
    clear = SETTINGS["planner"]["segment_clear_s"]
    by_seg: dict[str, list] = defaultdict(list)
    events: dict[str, list] = defaultdict(list)
    for e in plan.entries:
        if e.kind == "run":
            by_seg[e.segment_id].append((e.start, e.end + clear, e.train_id))
        else:
            events[e.station_id] += [(e.start, 1), (e.end, -1)]
    for seg, runs in by_seg.items():
        runs.sort()
        for (s1, e1, a), (s2, _, b) in zip(runs, runs[1:]):
            assert s2 >= e1 - 1, f"{a} and {b} overlap on {seg}"
    for st, ev in events.items():
        level = peak = 0
        for _, d in sorted(ev, key=lambda x: (x[0], x[1])):
            level += d
            peak = max(peak, level)
        assert peak <= WORLD.capacity(st), f"station {st} over capacity"


def new_sim() -> FieldSim:
    return FieldSim(WORLD, RunningTimes(WORLD), SETTINGS)


def snapshot(sim: FieldSim, plan: Plan | None):
    return build_snapshot(sim.snapshot(), WORLD.timetable, [fi.incident for fi in sim.active()], plan)


def initial_plan(sim: FieldSim) -> Plan:
    plan = Plan.model_validate(solve_job(snapshot(sim, None).model_dump(mode="json"), "balanced", SETTINGS)["plan"])
    plan.version = 1
    return plan


def test_plan_v1_is_conflict_free_and_on_time():
    plan = initial_plan(new_sim())
    assert plan.solver == "cpsat"
    assert_conflict_free(plan)
    assert plan.index.category == "norm"


def test_obstacle_gives_three_conflict_free_variants_within_5s():
    sim = new_sim()
    plan = initial_plan(sim)
    sim.set_plan(plan)
    sim.step(3000)
    inc = sim.create_incident(OBSTACLE)
    snap = snapshot(sim, plan)

    async def run():
        pool = SolverPool(SETTINGS["planner"]["workers"])
        try:
            await pool.warm(SETTINGS["planner"]["workers"])
            t0 = time.perf_counter()
            vs = await generate_variants(pool, WORLD, snap, SETTINGS, plan.version, None,
                                         pick_strategies(snap.incidents, SETTINGS))
            return vs, time.perf_counter() - t0
        finally:
            pool.shutdown()

    variants, elapsed = asyncio.run(run())
    assert elapsed <= 5.0
    assert [v.strategy for v in variants] == ["balanced", "robust", "passenger_first"]
    closure_end = inc.started_at + inc.est_expected_s
    for v in variants:
        assert_conflict_free(v.plan)
        assert v.explanation
        on_segment = {t.train_id for t in snap.states.values() if t.segment_id == "R1-STP"}
        for e in v.plan.entries:
            if e.kind == "run" and e.segment_id == "R1-STP" and e.train_id not in on_segment:
                assert e.start >= closure_end - 1, f"{v.strategy}: {e.train_id} enters closed segment"


def test_headless_run_with_obstacle_is_safe_and_completes():
    sim = new_sim()
    plan = initial_plan(sim)
    sim.set_plan(plan)
    sim.step(3000)
    sim.create_incident(OBSTACLE)
    res = solve_job(snapshot(sim, plan).model_dump(mode="json"), "balanced", SETTINGS)
    new_plan = Plan.model_validate(res["plan"])
    new_plan.version = 2
    sim.set_plan(new_plan)
    while sim.now < 8 * 3600 and not all(t.loc == "done" for t in sim.trains.values()):
        sim.step(60)
    assert sim.safety_violations == 0
    assert all(t.loc == "done" for t in sim.trains.values())


def test_forecast_matches_plan_in_normal_operation():
    """Regression: the 1 Hz forecast must not invent delays when nothing happens (horizon entry, slow running)."""
    sim = new_sim()
    plan = initial_plan(sim)
    sim.set_plan(plan)
    for t in (600, 3000, 6000, 9000):
        sim.step(t - sim.now)
        fc = forecast_plan(snapshot(sim, plan), SETTINGS)
        # trains entering the horizon after v1 may add ~1 min each until the service refreshes the plan
        assert fc.kpi.total_delay_s <= 300, f"phantom delay at {t}: {fc.kpi.delayed_trains}"
        assert fc.index.category == "norm"


def test_variant_applied_late_is_retimed_from_now():
    """Regression: a variant solved at T0 and applied later must not leave trains behind the new plan."""
    sim = new_sim()
    plan = initial_plan(sim)
    sim.set_plan(plan)
    sim.step(3000)
    sim.create_incident(OBSTACLE)
    variant = Plan.model_validate(solve_job(snapshot(sim, plan).model_dump(mode="json"), "balanced", SETTINGS)["plan"])
    sim.step(600)  # the dispatcher reads the cards for 10 minutes
    retimed = forecast_plan(snapshot(sim, plan).model_copy(update={"hint": variant.entries}), SETTINGS)
    sim.set_plan(retimed)
    sim.step(60)
    drift = max(t["delay_s"] for t in sim.snapshot()["trains"] if t["on_field"])  # lag behind the new plan
    assert drift <= SETTINGS["planner"]["replan_deviation_s"]


def test_segment_closure_then_reapply_does_not_deadlock():
    """Regression: closing R2-OZR, applying, re-applying after it clears used to fill R2 and OZR with
    trains waiting for each other. The field must keep moving and every train must finish."""
    sim = new_sim()
    plan = initial_plan(sim)
    sim.set_plan(plan)
    sim.step(3000)
    sim.create_incident({"type": "segment_closed", "segment_id": "R2-OZR", "est_min_min": 30, "est_max_min": 30})

    def apply_best(current: Plan) -> Plan:
        v = Plan.model_validate(solve_job(snapshot(sim, current).model_dump(mode="json"), "balanced", SETTINGS)["plan"])
        new = forecast_plan(snapshot(sim, current).model_copy(update={"hint": v.entries}), SETTINGS)
        sim.set_plan(new)
        return new

    plan = apply_best(plan)
    while sim.active():
        sim.step(60)
    plan = apply_best(plan)
    while sim.now < 30000 and not all(t.loc == "done" for t in sim.trains.values()):
        sim.step(60)
        fc = forecast_plan(snapshot(sim, plan), SETTINGS)
        if fc is not None and max((t["delay_s"] for t in sim.snapshot()["trains"] if t["on_field"]), default=0) > 120:
            plan = fc
            sim.set_plan(plan)
    assert sim.safety_violations == 0
    assert all(t.loc == "done" for t in sim.trains.values())


def test_eco_profile_hits_target_and_saves_energy():
    seg = WORLD.segments["OZR-YUZ"]
    cat = WORLD.categories["freight"]
    fast, _ = eco_profile(seg, cat, Direction.EVEN, 80, True, True, 0)
    eco, ref = eco_profile(seg, cat, Direction.EVEN, 80, True, True, fast.time_s * 1.1)
    assert abs(eco.time_s - fast.time_s * 1.1) <= 2
    assert eco.energy_kwh < ref.energy_kwh
    assert "coast" in eco.regime


def test_api_end_to_end():
    from app.all import build_app

    with TestClient(build_app()) as client:
        assert client.get("/api/infra").status_code == 200
        plan = client.get("/api/plan").json()
        assert plan["version"] == 1
        r = client.post("/api/plan/apply", json={"variant_id": "nope", "base_plan_version": 1})
        assert r.status_code == 404
        r = client.post("/api/whatif", json={"modifications": [{"kind": "train_speed", "target_id": "2003", "value": 60}]})
        assert r.status_code == 200 and "delta_index" in r.json()
        assert client.get("/api/ato/2004").json()["saving_pct"] >= 0
        with client.websocket_connect("/ws") as ws:
            assert ws.receive_json()["type"] == "snapshot"
