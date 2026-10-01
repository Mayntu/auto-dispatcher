"""Independent audit of the dispatching algorithm.

For random disruption situations it (1) re-checks every CP-SAT plan against the railway rules with its own
code — it does not trust the planner — and (2) compares the optimiser with two naive dispatchers:

  keep   — do nothing: keep the order of the current plan (what happens without a decision)
  fifo   — first come, first served by the timetable order
  cp-sat — the planner (strategy "balanced", production settings: 1.5 s, 4 workers)

    python -m tools.audit --cases 30 --procs 3
"""

from __future__ import annotations

import argparse
import random
import statistics
import time
from collections import defaultdict
from multiprocessing import Pool

from app.common.config import load_settings, segment_clear_s
from app.field.incidents import IncidentError
from app.field.sim import FieldSim
from app.planner.pool import forecast_plan, solve_job
from app.planner.snapshot import build_snapshot
from app.railcore.infra import get_world
from app.railcore.models import IncidentType, Plan
from app.railcore.running_time import RunningTimes

TOL = 1.0  # seconds of rounding tolerance


def check_plan(plan: Plan, snap, world, rts, settings) -> list[str]:
    """Railway rules, re-derived from the raw plan entries."""
    errs: list[str] = []
    clear = segment_clear_s(settings)
    now = snap.now
    trains = {t.id: t for t in snap.trains}
    states = snap.states
    by_train = defaultdict(list)
    for e in plan.entries:
        by_train[e.train_id].append(e)

    # 1. single-track segment: one train at a time (+ clearance), both directions
    runs = defaultdict(list)
    for e in plan.entries:
        if e.kind == "run":
            runs[e.segment_id].append(e)
    for seg, rs in runs.items():
        rs.sort(key=lambda e: e.start)
        for a, b in zip(rs, rs[1:]):
            if b.start < a.end + clear - TOL:
                errs.append(f"segment {seg}: {b.train_id} enters at {b.start:.0f} before {a.train_id} clears ({a.end + clear:.0f})")

    # 2. station capacity: never more trains than tracks
    for st in world.station_order:
        ev = []
        for e in plan.entries:
            if e.kind == "dwell" and e.station_id == st:
                ev += [(e.start, 1), (e.end, -1)]
        level = 0
        for _, d in sorted(ev, key=lambda x: (x[0], x[1])):
            level += d
            if level > world.capacity(st):
                errs.append(f"station {st}: {level} trains on {world.capacity(st)} tracks")
                break

    closures = [i for i in snap.incidents if i.type in (IncidentType.OBSTACLE, IncidentType.SEGMENT_CLOSED)]
    for tid, es in by_train.items():
        es.sort(key=lambda e: (e.start, e.kind != "dwell"))
        train = trains[tid]
        cat = world.categories[train.category]
        stops = {s.station_id: s for s in train.stops}
        # 3. continuity: dwell -> run -> dwell ... with matching times
        for a, b in zip(es, es[1:]):
            if abs(b.start - a.end) > TOL:
                errs.append(f"{tid}: gap/overlap between {a.kind} {a.station_id or a.segment_id} and {b.kind}")
        for e in es:
            if e.kind == "run":
                if e.start <= now + TOL and states.get(tid) and states[tid].segment_id == e.segment_id:
                    continue  # already on the segment at the snapshot
                rt = rts.get(cat.id, e.segment_id, train.direction, train.v_max_override_kmh)
                # 4. never faster than the running time; never crawling more than the model allows
                if e.end - e.start < rt.t_pp - TOL:
                    errs.append(f"{tid}: {e.segment_id} in {e.end - e.start:.0f} s < running time {rt.t_pp:.0f} s")
                # easing off instead of stopping: up to 1.3x the running time plus the cost of a stop (§12.5)
                if e.end - e.start > 1.3 * (rt.t_pp + rt.sup_start + rt.sup_end) + rt.sup_start + rt.sup_end + 5 and not any(
                        c.segment_id == e.segment_id for c in closures):
                    errs.append(f"{tid}: {e.segment_id} crawls {e.end - e.start:.0f} s")
                # 5. no departure onto a closed segment before the closure is expected to end
                for c in closures:
                    end = c.started_at + c.est_expected_s
                    if c.segment_id == e.segment_id and e.start < end - TOL:
                        errs.append(f"{tid}: enters closed {e.segment_id} at {e.start:.0f} (closed till {end:.0f})")
            else:
                s = stops[e.station_id]
                at_station = states.get(tid) and states[tid].station_id == e.station_id
                # 6. passenger / express never leave a scheduled stop before the timetable
                if s.stop and cat.id != "freight" and s.dep is not None and e.end < s.dep - TOL:
                    errs.append(f"{tid}: leaves {e.station_id} at {e.end:.0f} before timetable {s.dep:.0f}")
                # 7. minimum dwell at scheduled intermediate stops
                if (s.stop and s.arr is not None and s.dep is not None and not at_station
                        and e.end - e.start < max(s.min_dwell_s, cat.min_dwell_s) - TOL):
                    errs.append(f"{tid}: dwell at {e.station_id} {e.end - e.start:.0f} s < minimum")
    return errs


def make_case(seed: int):
    rng = random.Random(seed)
    world, settings = get_world(), load_settings()
    sim = FieldSim(world, RunningTimes(world), settings, seed=seed)
    snap0 = build_snapshot(sim.snapshot(), world.timetable, [], None)
    plan = Plan.model_validate(solve_job(snap0.model_dump(mode="json"), "balanced", settings)["plan"])
    sim.set_plan(plan)
    sim.step(rng.uniform(1800, 7200))
    for _ in range(rng.randint(1, 3)):
        kind = rng.choice(["obstacle", "train_failure", "segment_closed"])
        lo = rng.randint(10, 35)
        req = {"type": kind, "est_min_min": lo, "est_max_min": lo + rng.randint(0, 25)}
        if kind == "train_failure":
            req["train_id"] = rng.choice([t.id for t in world.timetable])
        else:
            seg = rng.choice(list(world.segments))
            req["segment_id"] = seg
            if kind == "obstacle":
                a, b = world.segment_km(seg)
                req["km"] = round(rng.uniform(a + 1, b - 1), 2)
        try:
            sim.create_incident(req)
        except IncidentError:
            pass
    return build_snapshot(sim.snapshot(), world.timetable, [fi.incident for fi in sim.active()], plan), settings


def run_case(seed: int) -> dict:
    world = get_world()
    rts = RunningTimes(world)
    snap, settings = make_case(seed)
    keep = forecast_plan(snap, settings)
    fifo = forecast_plan(snap.model_copy(update={"hint": []}), settings)
    t0 = time.perf_counter()
    res = solve_job(snap.model_dump(mode="json"), "balanced", settings)
    ms = (time.perf_counter() - t0) * 1000
    plan = Plan.model_validate(res["plan"])
    return {
        "seed": seed, "incidents": len(snap.incidents), "trains": len({e.train_id for e in plan.entries}),
        "status": res["status"], "ms": ms, "violations": check_plan(plan, snap, world, rts, settings),
        "keep": keep.kpi.weighted_delay_s / 60 if keep else None,
        "fifo": fifo.kpi.weighted_delay_s / 60 if fifo else None,
        "cpsat": plan.kpi.weighted_delay_s / 60,
        "keep_total": keep.kpi.total_delay_s / 60 if keep else None, "cpsat_total": plan.kpi.total_delay_s / 60,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", type=int, default=30)
    ap.add_argument("--procs", type=int, default=3)
    args = ap.parse_args()
    with Pool(args.procs) as pool:
        rows = pool.map(run_case, range(1000, 1000 + args.cases))
    print(f"{'case':>5} {'сбоев':>5} {'поездов':>7} {'статус':>9} {'мс':>6} | взвешенная задержка, мин: "
          f"{'ничего':>7} {'FIFO':>7} {'CP-SAT':>7} | нарушений")
    for r in rows:
        f = lambda x: f"{x:7.0f}" if x is not None else "    n/a"  # noqa: E731
        print(f"{r['seed']:>5} {r['incidents']:>5} {r['trains']:>7} {r['status']:>9} {r['ms']:6.0f} |"
              f"                           {f(r['keep'])} {f(r['fifo'])} {f(r['cpsat'])} | {len(r['violations'])}")
        for v in r["violations"][:5]:
            print("        !", v)
    ok = [r for r in rows if r["keep"]]
    viol = sum(len(r["violations"]) for r in rows)
    worse = [r["seed"] for r in ok if r["cpsat"] > r["keep"] + 1]
    ms = sorted(r["ms"] for r in rows)
    print("\nИтог:")
    print(f"  планов проверено: {len(rows)}, нарушений правил: {viol}")
    print(f"  CP-SAT хуже «ничего не делать»: {len(worse)} {worse}")
    if ok:
        gain = [(r["keep"] - r["cpsat"]) / r["keep"] * 100 for r in ok if r["keep"] > 1]
        print(f"  взвешенная задержка: ничего {statistics.mean(r['keep'] for r in ok):.0f} мин, "
              f"FIFO {statistics.mean(r['fifo'] for r in ok if r['fifo']):.0f} мин, CP-SAT {statistics.mean(r['cpsat'] for r in ok):.0f} мин; "
              f"медианный выигрыш CP-SAT к «ничего» {statistics.median(gain):.0f}%")
    print(f"  время решения: p50 {ms[len(ms) // 2]:.0f} мс, p95 {ms[int(len(ms) * 0.95) - 1]:.0f} мс, max {ms[-1]:.0f} мс; "
          f"OPTIMAL {sum(r['status'] == 'OPTIMAL' for r in rows)}/{len(rows)}")


if __name__ == "__main__":
    main()
