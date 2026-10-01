"""CP-SAT dispatching model (CLAUDE.md §12.2).

MVP simplification: station tracks are a cumulative resource (capacity = number of tracks)
instead of explicit track assignment; no track-length check.
"""

from __future__ import annotations

import time

from ortools.sat.python import cp_model

from app.common.config import segment_clear_s
from app.railcore.infra import get_world
from app.railcore.models import PlanEntry
from app.railcore.evaluate import Deadlock, evaluate
from app.railcore.problem import Solution, Task

SCALE = 100  # objective coefficients are floats * SCALE
EPS_FINAL = 0.01


def solve_cpsat(tasks: list[Task], settings: dict, hint: list[PlanEntry], now: float,
                lambda_stop_mult: float = 1.0, time_limit_s: float | None = None,
                fix: Solution | None = None, keep_order: list[PlanEntry] | None = None
                ) -> tuple[Solution | None, str, int]:
    """`fix` pins some trains to given (arr, dep, stop) — used to stitch timetable windows together.
    `keep_order`: the approved plan — trains already in it keep their order on every segment, only trains
    new to the horizon are placed freely (extending the plan does not change the dispatcher's decision)."""
    p = settings["planner"]
    clear = segment_clear_s(settings)
    m = cp_model.CpModel()

    # warm start: the hint plan's order replayed on the current problem (a full, nearly feasible assignment)
    try:
        warm = evaluate(tasks, hint, clear, now)
    except Deadlock:
        warm = {}
    ub = max([p["horizon_s"] + 7200] + [d + 1800 for times in warm.values() for _, d, _ in times])
    seg_intervals: dict[str, list] = {}
    seg_users: dict[str, list[tuple[str, object, object]]] = {}  # segment -> (train, dep expr, arr expr)
    station_intervals: dict[str, list] = {}
    station_dir_intervals: dict[tuple[str, str], list] = {}
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
            # the field (DC) gives a train its track at the next station when it enters the segment, so the
            # track is held from the departure at the previous station, not from the arrival
            if i > 0:
                held = m.NewIntVar(0, ub + n.dest_dwell, f"h_{tid}_{i}")
                m.Add(d == dep[i - 1] + held)
                iv = m.NewIntervalVar(dep[i - 1], held, d, f"st_{tid}_{i}")
            else:
                iv = m.NewIntervalVar(a, dwell, d, f"st_{tid}_{i}")
            station_intervals.setdefault(n.station_id, []).append(iv)
            station_dir_intervals.setdefault((n.station_id, t.direction.value), []).append(iv)
            if (n.kind != "origin" and n.sched_arr is not None and n.arr_fixed is None
                    and (n.kind == "dest" or (n.sched_stop and t.category != "freight"))):
                late = m.NewIntVar(0, ub, f"late_{tid}_{i}")
                m.Add(late >= a - n.sched_arr)
                objective.append(round(t.weight * SCALE) * late)
            if fix and tid in fix:
                fa, fd, fs = fix[tid][i]
                m.Add(a == fa)
                m.Add(d == fd)
                if not n.stop_fixed:
                    m.Add(s == int(fs))
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
            seg_users.setdefault(t.current.segment_id, []).append((tid, 0, arr[0]))
        for i, leg in enumerate(t.legs):
            run = arr[i + 1] - dep[i]
            m.Add(run >= leg.t_pp + leg.sup_start * stop[i] + leg.sup_end * stop[i + 1])
            m.Add(run <= round(1.3 * (leg.t_pp + leg.sup_start + leg.sup_end)))
            end = m.NewIntVar(0, ub + clear, f"be_{tid}_{i}")
            m.Add(end == arr[i + 1] + clear)
            size = m.NewIntVar(0, ub + clear, f"bs_{tid}_{i}")
            seg_intervals.setdefault(leg.segment_id, []).append(m.NewIntervalVar(dep[i], size, end, f"b_{tid}_{i}"))
            seg_users.setdefault(leg.segment_id, []).append((tid, dep[i], arr[i + 1]))
        vars_by_train[tid] = list(zip(arr, dep, stop))

    for ivs in seg_intervals.values():
        m.AddNoOverlap(ivs)
    if keep_order:
        start = {(e.train_id, e.segment_id): e.start for e in keep_order if e.kind == "run"}
        for seg, users in seg_users.items():
            kept = sorted((u for u in users if (u[0], seg) in start), key=lambda u: start[(u[0], seg)])
            for (_, _, arr_a), (_, dep_b, _) in zip(kept, kept[1:]):
                m.Add(dep_b >= arr_a + clear)
    world = get_world()
    for st_id, ivs in station_intervals.items():
        m.AddCumulative(ivs, [1] * len(ivs), world.capacity(st_id))
    # as the field enforces: one track per station stays free for opposite traffic (deadlock avoidance)
    for (st_id, _), ivs in station_dir_intervals.items():
        m.AddCumulative(ivs, [1] * len(ivs), max(1, world.capacity(st_id) - 1))
    m.Minimize(sum(objective))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_s if time_limit_s is not None else p["time_limit_s"]
    if p.get("deterministic_time") and time_limit_s is None:  # reproducible runs (stress tests)
        solver.parameters.max_deterministic_time = p["deterministic_time"]
        solver.parameters.max_time_in_seconds = 30
    solver.parameters.num_workers = p.get("cpsat_workers", 4)
    solver.parameters.random_seed = 42
    # no repair_hint: OR-Tools 9.15 aborts the whole process in MinimizeL1DistanceWithHint on some models
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


def objective(tasks: list[Task], sol: Solution, settings: dict, lambda_stop_mult: float = 1.0) -> float:
    """The model's objective evaluated on any solution (e.g. the current order re-timed), same units."""
    lam = settings["planner"]["lambda_stop"] * lambda_stop_mult
    total = 0.0
    for t in tasks:
        times = sol[t.train_id]
        for n, (arr, _, stop) in zip(t.nodes, times):
            if (n.kind != "origin" and n.sched_arr is not None and n.arr_fixed is None
                    and (n.kind == "dest" or (n.sched_stop and t.category != "freight"))):
                total += t.weight * max(0, arr - n.sched_arr)
            if n.kind == "mid" and not n.stop_fixed and stop:
                total += lam
        total += EPS_FINAL * times[-1][0]
    return total

