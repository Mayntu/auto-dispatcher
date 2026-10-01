"""Headless fuzz of the dispatching loop: random incidents x random dispatcher behaviour.

Mirrors the planner service decisions (variants on incident/resolve, re-timing on apply, refresh on lag)
with the same railcore/planner functions and checks invariants that a dispatcher would notice:

  - no safety violations (two trains on a segment / station over capacity)
  - every train finishes (no deadlock / stall)
  - while no incident is active, the forecast does not keep growing
  - an applied variant delivers what its card promised (when applied right away)
  - the forecast can always be computed (no deadlock in the plan order)

    python -m tools.stress --runs 60 --procs 8
"""

from __future__ import annotations

import argparse
import copy
import random
import time
import traceback
from multiprocessing import Pool

from app.common.config import load_settings
from app.field.incidents import IncidentError
from app.field.sim import FieldSim
from app.planner.pool import forecast_plan, retime_with_objective, solve_job
from app.planner.snapshot import build_snapshot
from app.planner.strategies import STRATEGIES, durations_for, pick_strategies
from app.railcore.infra import get_world
from app.railcore.models import Plan
from app.railcore.running_time import RunningTimes

POLICIES = ["best", "late", "worst", "ignore", "replan"]
STEP = 60.0
END = 40000.0


def make_incidents(rng: random.Random, world, mass: bool) -> list[tuple[float, dict]]:
    segs = list(world.segments)
    trains = [t.id for t in world.timetable]
    n = 8 if mass else rng.randint(1, 4)
    base = rng.uniform(900, 9000)
    out = []
    for k in range(n):
        at = base + (rng.uniform(0, 120) if mass else rng.uniform(0, 5000))
        kind = rng.choice(["obstacle", "train_failure", "segment_closed"])
        lo = rng.randint(5, 40)
        hi = lo + rng.randint(0, 30)
        req = {"type": kind, "est_min_min": lo, "est_max_min": hi}
        if kind == "train_failure":
            req["train_id"] = rng.choice(trains)
        else:
            seg = world.segments[rng.choice(segs)]
            req["segment_id"] = seg.id
            if kind == "obstacle":
                a, b = world.segment_km(seg.id)
                req["km"] = round(rng.uniform(a + 0.5, b - 0.5), 2)
        out.append((at, req))
    return sorted(out, key=lambda x: x[0])


def run(seed: int) -> dict:
    rng = random.Random(seed)
    world = get_world()
    settings = copy.deepcopy(load_settings())
    settings["planner"]["time_limit_s"] = 0.7
    settings["planner"]["cpsat_workers"] = 1  # deterministic: a failing seed reproduces
    settings["planner"]["deterministic_time"] = 1.0  # work units, not wall time: independent of CPU load
    sim = FieldSim(world, RunningTimes(world), settings, seed=seed)
    policy = POLICIES[seed % len(POLICIES)]
    mass = seed % 7 == 0
    incidents = make_incidents(rng, world, mass)
    res = {"seed": seed, "policy": policy, "mass": mass, "incidents": len(incidents), "errors": [],
           "solves": 0, "max_solve_ms": 0, "promise_gap": 0.0, "stale_applies": 0, "forecast_fail": 0,
           "max_quiet_growth_min": 0.0, "stall": False, "max_fail_streak": 0, "overrides": 0}

    def snap(plan):
        return build_snapshot(sim.snapshot(), world.timetable, [fi.incident for fi in sim.active()], plan)

    def solve(sn, strategy):
        t0 = time.perf_counter()
        p = Plan.model_validate(solve_job(sn.model_dump(mode="json"), strategy, settings)["plan"])
        res["solves"] += 1
        res["max_solve_ms"] = max(res["max_solve_ms"], round((time.perf_counter() - t0) * 1000))
        return p

    try:
        plan = solve(snap(None), "balanced")
        sim.set_plan(plan)
        pending_variants: list[tuple[str, Plan]] | None = None
        apply_at = None
        need_regen = False
        last_replan = 0.0
        quiet_since, quiet_delay = None, None
        last_move, last_km = 0.0, None
        fail_streak = 0

        while sim.now < END and not all(t.loc == "done" for t in sim.trains.values()):
            for topic, _ in sim.step(STEP):
                if topic == "incident.resolved":
                    need_regen = True
            while incidents and incidents[0][0] <= sim.now:
                _, req = incidents.pop(0)
                try:
                    sim.create_incident(req)
                    need_regen = True
                except IncidentError:
                    pass
            if policy == "replan" and sim.now - last_replan > 1800:
                need_regen, last_replan = True, sim.now

            if need_regen:
                need_regen = False
                sn = snap(plan)
                pending_variants = [(s, solve(sn, s)) for s in pick_strategies(sn.incidents, settings)]
                apply_at = sim.now + (rng.uniform(120, 900) if policy == "late" else 0)

            if pending_variants is not None and apply_at is not None and sim.now >= apply_at and policy != "ignore":
                ranked = sorted(pending_variants, key=lambda v: v[1].index.value, reverse=True)
                strategy, chosen = ranked[-1] if policy == "worst" else ranked[0]
                sn = snap(plan).model_copy(update={"hint": chosen.entries})
                durs = durations_for(STRATEGIES[strategy], sn.incidents, settings)
                nv = retime_with_objective(sn, settings, strategy, durs)
                cv = retime_with_objective(snap(plan), settings, strategy, durs)
                new = nv[0] if nv else None
                if nv is not None and cv is not None and nv[1] > cv[1] + 300:
                    new = None  # the service refuses a variant that became worse than the current plan
                if new is None:
                    res["stale_applies"] += 1
                    need_regen = True
                else:
                    if policy != "late":
                        res["promise_gap"] = max(res["promise_gap"], chosen.index.value - new.index.value)
                    plan = new
                    sim.set_plan(plan)
                pending_variants, apply_at = None, None

            fc = forecast_plan(snap(plan), settings)
            if fc is None:
                res["forecast_fail"] += 1
                fail_streak += 1
                res["max_fail_streak"] = max(res["max_fail_streak"], fail_streak)
            else:
                fail_streak = 0
            # as the planner service does: broken plan or stopped field -> new variants from the current state
            if pending_variants is None and (fail_streak == 3 or sim.stalled_s >= 3 * settings["planner"]["replan_deviation_s"]):
                need_regen = True
            if fc is not None:
                lag = max((t["delay_s"] for t in sim.snapshot()["trains"] if t["on_field"]), default=0)
                new_trains = {e.train_id for e in fc.entries} - {e.train_id for e in plan.entries}
                if lag > settings["planner"]["replan_deviation_s"] or new_trains:
                    plan = fc
                    sim.set_plan(plan)
                # quiet window: no active incident -> the forecast must not keep growing
                if not sim.active():
                    if quiet_since is None:
                        quiet_since, quiet_delay = sim.now, fc.kpi.total_delay_s
                    elif sim.now - quiet_since >= 1800:
                        growth = (fc.kpi.total_delay_s - quiet_delay) / 60
                        res["max_quiet_growth_min"] = max(res["max_quiet_growth_min"], round(growth, 1))
                        quiet_since, quiet_delay = sim.now, fc.kpi.total_delay_s
                else:
                    quiet_since = None

            km = sum(t.progress + t.idx for t in sim.trains.values())
            if km != last_km:
                last_move, last_km = sim.now, km
            elif not sim.active() and sim.now - last_move > 3600:
                res["stall"] = True
                break

        res["end"] = round(sim.now)
        res["finished"] = sum(t.loc == "done" for t in sim.trains.values())
        res["violations"] = sim.safety_violations
        res["overrides"] = sim.plan_overrides
        res["stuck"] = [(t.train.id, t.loc, t.route[t.idx] if t.loc != "none" else "") for t in sim.trains.values()
                        if t.loc != "done"]
    except Exception:
        res["errors"].append(traceback.format_exc(limit=6))
    return res


def verdict(r: dict) -> list[str]:
    bad = []
    if r["errors"]:
        bad.append("EXCEPTION")
    if r.get("violations"):
        bad.append(f"SAFETY x{r['violations']}")
    if r.get("finished", 0) < 10:
        bad.append(f"NOT FINISHED {r.get('stuck')}")
    if r["stall"]:
        bad.append("STALL")
    if r["max_quiet_growth_min"] > 10:
        bad.append(f"FORECAST GROWS {r['max_quiet_growth_min']} min/30min")
    if r["promise_gap"] > 5:
        bad.append(f"PROMISE GAP {r['promise_gap']:.1f}")
    if r["max_fail_streak"] > 8:  # unexecutable plan not recovered within ~8 sim minutes
        bad.append(f"PLAN BROKEN {r['max_fail_streak']} min in a row")
    return bad


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=40)
    ap.add_argument("--procs", type=int, default=8)
    ap.add_argument("--seed0", type=int, default=0)
    ap.add_argument("--timeout", type=float, default=900, help="whole-run budget, seconds")
    args = ap.parse_args()
    t0 = time.time()
    results = []
    failed = 0
    with Pool(args.procs) as pool:
        pending = {seed: pool.apply_async(run, (seed,)) for seed in range(args.seed0, args.seed0 + args.runs)}
        deadline = time.time() + args.timeout
        for seed, fut in pending.items():
            try:
                results.append(fut.get(timeout=max(1, deadline - time.time())))
            except Exception:  # noqa: BLE001 - a hung scenario is a finding, not a crash of the harness
                results.append({"seed": seed, "policy": POLICIES[seed % len(POLICIES)], "mass": seed % 7 == 0,
                                "incidents": "?", "errors": ["TIMEOUT: scenario did not finish"], "solves": 0,
                                "max_solve_ms": 0, "promise_gap": 0.0, "stale_applies": 0, "forecast_fail": 0,
                                "max_quiet_growth_min": 0.0, "stall": False, "max_fail_streak": 0, "overrides": 0})
            r = results[-1]
            bad = verdict(r)
            failed += bool(bad)
            mark = "FAIL" if bad else "ok  "
            print(f"{mark} seed={r['seed']:3} {r['policy']:7} {'MASS' if r['mass'] else '    '} inc={r['incidents']} "
                  f"end={r.get('end')} fin={r.get('finished')} solves={r['solves']} maxsolve={r['max_solve_ms']}ms "
                  f"gap={r['promise_gap']:.1f} stale={r['stale_applies']} quiet+={r['max_quiet_growth_min']} "
                  f"ffail={r['forecast_fail']} ovr={r.get('overrides', 0)} "
                  f"{' | '.join(bad)}", flush=True)
            for e in r["errors"]:
                print(e, flush=True)
        pool.terminate()
    print(f"\n{len(results) - failed}/{len(results)} passed in {time.time() - t0:.0f} s")


if __name__ == "__main__":
    main()
