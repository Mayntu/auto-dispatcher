"""CP-SAT dispatching model (CLAUDE.md §12.2).

MVP simplification: station tracks are a cumulative resource (capacity = number of tracks)
instead of explicit track assignment; no track-length check.
"""

from __future__ import annotations

import time

from ortools.sat.python import cp_model

from app.railcore.infra import get_world
from app.railcore.models import PlanEntry
from app.railcore.evaluate import Deadlock, evaluate
from app.railcore.problem import Solution, Task

SCALE = 100  # objective coefficients are floats * SCALE
EPS_FINAL = 0.01


def solve_cpsat(tasks: list[Task], settings: dict, hint: list[PlanEntry], now: float,
                lambda_stop_mult: float = 1.0, time_limit_s: float | None = None) -> tuple[Solution | None, str, int]:
    p = settings["planner"]
    clear = p["segment_clear_s"]
    m = cp_model.CpModel()

    # warm start: the hint plan's order replayed on the current problem (a full, nearly feasible assignment)
    try:
        warm = evaluate(tasks, hint, clear)
    except Deadlock:
        warm = {}
    ub = max([p["horizon_s"] + 7200] + [d + 1800 for times in warm.values() for _, d, _ in times])
    seg_intervals: dict[str, list] = {}
    station_intervals: dict[str, list] = {}
    objective = []
    vars_by_train: dict[str, list[tuple]] = {}

    for t in tasks:
        tid = t.train_id
        arr, dep, stop = [], [], []
        for i, n in enumerate(t.nodes):
            a = m.NewIntVar(n.arr_fixed if n.arr_fixed is not None else 0, ub if n.arr_fixed is None else n.arr_fixed, f"a_{tid}_{i}")
            d = m.NewIntVar(n.dep_min, ub + n.dest_dwell, f"d_{tid}_{i}")
            s = 1 if n.stop_fixed else m.NewBoolVar(f"s_{tid}_{i}")
            dwell = m.NewIntVar(0, ub + n.dest_dwell, f"w_{tid}_{i}")
            m.Add(d == a + dwell)
            if n.kind == "dest":
                m.Add(dwell == n.dest_dwell)
            else:
                m.Add(dwell >= n.dwell_min)
                if not n.stop_fixed:
                    m.Add(dwell <= n.pass_threshold + ub * s)
                    objective.append(round(p["lambda_stop"] * lambda_stop_mult * SCALE) * s)
            station_intervals.setdefault(n.station_id, []).append(m.NewIntervalVar(a, dwell, d, f"st_{tid}_{i}"))
            if (n.kind != "origin" and n.sched_arr is not None and n.arr_fixed is None
                    and (n.kind == "dest" or (n.sched_stop and t.category != "freight"))):
                late = m.NewIntVar(0, ub, f"late_{tid}_{i}")
                m.Add(late >= a - n.sched_arr)
                objective.append(round(t.weight * SCALE) * late)
            if tid in warm:
                wa, wd, ws = warm[tid][i]
                m.AddHint(a, wa)
                m.AddHint(d, wd)
                if not n.stop_fixed:
                    m.AddHint(s, ws)
            arr.append(a)
            dep.append(d)
            stop.append(s)
        objective.append(round(EPS_FINAL * SCALE) * arr[-1])

        if t.current:
            end = m.NewIntVar(0, ub + clear, f"ce_{tid}")
            m.Add(arr[0] >= t.current.remaining + t.current.sup_end * stop[0])
            m.Add(end == arr[0] + clear)
            size = m.NewIntVar(0, ub + clear, f"cs_{tid}")
            seg_intervals.setdefault(t.current.segment_id, []).append(m.NewIntervalVar(0, size, end, f"cb_{tid}"))
        for i, leg in enumerate(t.legs):
            run = arr[i + 1] - dep[i]
            m.Add(run >= leg.t_pp + leg.sup_start * stop[i] + leg.sup_end * stop[i + 1])
            m.Add(run <= round(1.3 * (leg.t_pp + leg.sup_start + leg.sup_end)))
            end = m.NewIntVar(0, ub + clear, f"be_{tid}_{i}")
            m.Add(end == arr[i + 1] + clear)
            size = m.NewIntVar(0, ub + clear, f"bs_{tid}_{i}")
            seg_intervals.setdefault(leg.segment_id, []).append(m.NewIntervalVar(dep[i], size, end, f"b_{tid}_{i}"))
        vars_by_train[tid] = list(zip(arr, dep, stop))

    for ivs in seg_intervals.values():
        m.AddNoOverlap(ivs)
    world = get_world()
    for st_id, ivs in station_intervals.items():
        m.AddCumulative(ivs, [1] * len(ivs), world.capacity(st_id))
    m.Minimize(sum(objective))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_s if time_limit_s is not None else p["time_limit_s"]
    solver.parameters.num_workers = 4
    solver.parameters.random_seed = 42
    solver.parameters.repair_hint = True
    t0 = time.perf_counter()
    status = solver.Solve(m)
    ms = round((time.perf_counter() - t0) * 1000)
    name = solver.StatusName(status)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None, name, ms
    sol: Solution = {
        tid: [(solver.Value(a), solver.Value(d), bool(s if isinstance(s, int) else solver.Value(s))) for a, d, s in vs]
        for tid, vs in vars_by_train.items()
    }
    return sol, name, ms
