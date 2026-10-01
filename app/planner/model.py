"""CP-SAT dispatching model (CLAUDE.md §12.2) — single-track segments with two-way automatic block.

Per train and station: arrival, departure, stop flag and the station track (optional intervals, useful
length and platforms checked when the candidate tracks are built). Per segment:
- oncoming trains never share it: occ_ext = [dep, arr + max(tau_cross, direction change)] do not overlap;
- following trains run in a packet: a_first decides who goes first, the other keeps the headway at both
  ends (no overtaking on the line); pairs whose time windows cannot meet get a fixed order, no variable;
- tau_np: at stations without simultaneous reception oncoming trains do not arrive within tau_np.
"""

from __future__ import annotations

import logging
import time

from ortools.sat.python import cp_model

from app.common.config import segment_clear_s
from app.railcore.evaluate import Deadlock, evaluate
from app.railcore.models import PlanEntry
from app.railcore.problem import Solution, Task

log = logging.getLogger("planner.model")
SCALE = 100  # objective coefficients are floats * SCALE
EPS_FINAL = 0.01
WAIT_FREE_S = 1200  # a train may wait this long at a station before the long-wait penalty starts
PIN_TOLERANCE_S = 30
PIN_PENALTY = 10_000  # per second a dispatcher's instruction is missed: hard in practice, never infeasible
LAMBDA_WAIT = 0.5  # per second of waiting beyond that: no train (a freight in particular) starves for hours
WINDOW_SLACK_S = 3 * 3600  # pairs further apart than this keep their natural order without a decision variable


class TrackedSolution(dict):
    """Solution plus the station track chosen for every (train, station)."""

    tracks: dict[tuple[str, str], str]


def solve_cpsat(tasks: list[Task], settings: dict, hint: list[PlanEntry], now: float,
                lambda_stop_mult: float = 1.0, time_limit_s: float | None = None,
                fix: Solution | None = None, keep_order: list[PlanEntry] | None = None
                ) -> tuple[Solution | None, str, int]:
    """`fix` pins some trains to given (arr, dep, stop) — used to stitch timetable windows together.
    `keep_order`: trains already in this plan keep their order on every segment."""
    p = settings["planner"]
    iv = settings["intervals"]
    clear = segment_clear_s(settings)
    headway, tau_np = iv["headway_s"], iv["tau_np_s"]
    m = cp_model.CpModel()

    # warm start: the hint plan's order replayed on the current problem (a full, nearly feasible assignment)
    try:
        warm = evaluate(tasks, hint, clear, now, settings["intervals"])
    except Deadlock:
        warm = {}
    sched = [x for t in tasks for n in t.nodes for x in (n.sched_arr, n.sched_dep, n.dep_min, n.arr_fixed) if x is not None]
    ub = max([p["horizon_s"] + 7200] + [d + 1800 for times in warm.values() for _, d, _ in times]
             + [x + 4 * 3600 for x in sched])
    hint_track = {(e.train_id, e.station_id): e.track_id for e in hint if e.kind == "dwell" and e.track_id}

    seg_users: dict[str, list[dict]] = {}
    track_intervals: dict[tuple[str, str], list] = {}
    station_dir_intervals: dict[tuple[str, str], list] = {}
    arrivals: dict[str, list[tuple[str, object]]] = {}  # stations without simultaneous reception
    objective = []
    vars_by_train: dict[str, list[tuple]] = {}
    present_by: dict[tuple[str, str], dict[str, object]] = {}

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
                if n.kind == "mid":
                    waited = m.NewIntVar(0, ub, f"wt_{tid}_{i}")
                    m.Add(waited >= dwell - n.dwell_min - WAIT_FREE_S)
                    objective.append(round(LAMBDA_WAIT * SCALE) * waited)
                elif n.sched_dep is not None:  # origin: held past its departure time counts as waiting too
                    waited = m.NewIntVar(0, ub, f"wt_{tid}_{i}")
                    m.Add(waited >= d - n.sched_dep - WAIT_FREE_S)
                    objective.append(round(LAMBDA_WAIT * SCALE) * waited)
            # the DC gives a train its track at the next station when it enters the segment: the track is held
            # from the departure at the previous station, not from the arrival
            if i > 0:
                held = m.NewIntVar(0, ub + n.dest_dwell, f"h_{tid}_{i}")
                m.Add(d == dep[i - 1] + held)
                start, size = dep[i - 1], held
            else:
                start, size = a, dwell
            station_dir_intervals.setdefault((n.station_id, t.direction.value), []).append(
                m.NewIntervalVar(start, size, d, f"st_{tid}_{i}"))
            # station track: exactly one of the candidate tracks (useful length / platform already checked)
            cands = [n.track_fixed] if n.track_fixed else n.tracks
            pres = {}
            for tr in cands:
                pr = m.NewBoolVar(f"p_{tid}_{i}_{tr}")
                pres[tr] = pr
                track_intervals.setdefault((n.station_id, tr), []).append(
                    m.NewOptionalIntervalVar(start, size, d, pr, f"ti_{tid}_{i}_{tr}"))
                if hint_track.get((tid, n.station_id)) == tr:
                    m.AddHint(pr, 1)
            m.AddExactlyOne(pres.values())
            present_by[(tid, n.station_id)] = pres
            if not n.stop_fixed and n.kind == "mid":
                for tr in n.side_tracks:  # passing on a side track (40 km/h) counts as a stop
                    if tr in pres:
                        m.Add(s >= pres[tr])
            if not n.simultaneous_reception and n.kind != "origin":
                arrivals.setdefault(n.station_id, []).append((t.direction.value, a))
            if (n.kind != "origin" and n.sched_arr is not None and n.arr_fixed is None
                    and (n.kind == "dest" or (n.sched_stop and t.category != "freight"))):
                late = m.NewIntVar(0, ub, f"late_{tid}_{i}")
                m.Add(late >= a - n.sched_arr)
                objective.append(round(t.weight * SCALE) * late)
            # the dispatcher's instruction (tasks/02 §3.4): exact time, as a penalty so that an instruction a new
            # incident made impossible still gives a plan (the deviation then marks it violated)
            for pin_t, var in ((n.pin_arr, a), (n.pin_dep, d)):
                if pin_t is not None:
                    dev = m.NewIntVar(0, ub + n.dest_dwell, f"pd_{tid}_{i}_{len(objective)}")
                    m.Add(dev >= var - pin_t)
                    m.Add(dev >= pin_t - var)
                    objective.append(PIN_PENALTY * SCALE * dev)
                    # no extra hint here: the warm start already hints this variable, and a duplicate hint makes
                    # the whole model MODEL_INVALID (every solve then silently fell back to the fixed order)
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

        def user(seg_id, dep_e, arr_e, est, current=False, progress=0.0):
            seg_users.setdefault(seg_id, []).append({"train": tid, "dir": t.direction.value, "dep": dep_e, "arr": arr_e,
                                                     "est": est, "current": current, "progress": progress})

        if t.current:
            m.Add(arr[0] >= t.current.remaining + t.current.sup_end * stop[0])
            w_arr = warm[tid][0][0] if tid in warm else t.current.remaining
            user(t.current.segment_id, 0, arr[0], (0, w_arr), current=True, progress=t.current.progress)
        for i, leg in enumerate(t.legs):
            run = arr[i + 1] - dep[i]
            m.Add(run >= leg.t_pp + leg.sup_start * stop[i] + leg.sup_end * stop[i + 1])
            m.Add(run <= round(leg.max_factor * (leg.t_pp + leg.sup_start + leg.sup_end)))
            if tid in warm:
                est = (warm[tid][i][1], warm[tid][i + 1][0])
            else:
                base = t.nodes[i].sched_dep if t.nodes[i].sched_dep is not None else t.nodes[i].dep_min
                est = (base, base + leg.t_pp)
            user(leg.segment_id, dep[i], arr[i + 1], est)
        vars_by_train[tid] = list(zip(arr, dep, stop))

    # ---- segments: oncoming never together, followers in a packet with the headway ------------------------
    for seg_id, users in seg_users.items():
        occ = {}
        for u in users:
            end = m.NewIntVar(0, ub + clear, f"oe_{u['train']}_{seg_id}")
            m.Add(end == u["arr"] + clear)
            size = m.NewIntVar(0, ub + clear, f"os_{u['train']}_{seg_id}")
            occ[u["train"]] = m.NewIntervalVar(u["dep"], size, end, f"oc_{u['train']}_{seg_id}")
        for x in range(len(users)):
            for y in range(x + 1, len(users)):
                a, b = users[x], users[y]
                if a["dir"] != b["dir"]:
                    m.AddNoOverlap([occ[a["train"]], occ[b["train"]]])
                    continue
                _follow(m, a, b, headway, f"{a['train']}_{b['train']}_{seg_id}")
    # ---- stations: one train per track, a track kept for opposite traffic, tau_np -------------------------
    for ivs in track_intervals.values():
        m.AddNoOverlap(ivs)
    from app.railcore.infra import get_world

    world = get_world()
    for (st_id, _), ivs in station_dir_intervals.items():
        m.AddCumulative(ivs, [1] * len(ivs), max(1, world.capacity(st_id) - 1))
    for st_id, arrs in arrivals.items():
        for x in range(len(arrs)):
            for y in range(x + 1, len(arrs)):
                if arrs[x][0] != arrs[y][0]:
                    m.AddNoOverlap([m.NewFixedSizeIntervalVar(arrs[x][1], tau_np, f"np_{st_id}_{x}_{y}a"),
                                    m.NewFixedSizeIntervalVar(arrs[y][1], tau_np, f"np_{st_id}_{x}_{y}b")])
    if keep_order:
        start = {(e.train_id, e.segment_id): e.start for e in keep_order if e.kind == "run"}
        for seg, users in seg_users.items():
            kept = sorted((u for u in users if (u["train"], seg) in start), key=lambda u: start[(u["train"], seg)])
            for a, b in zip(kept, kept[1:]):
                m.Add(b["dep"] >= a["dep"])
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
    if status == cp_model.MODEL_INVALID:  # a bug in the model, never a property of the situation: make it loud
        log.error("CP-SAT model invalid: %s", m.Validate())
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None, name, ms
    sol = TrackedSolution({
        tid: [(solver.Value(a), solver.Value(d), bool(s if isinstance(s, int) else solver.Value(s))) for a, d, s in vs]
        for tid, vs in vars_by_train.items()
    })
    sol.tracks = {key: next(tr for tr, pr in pres.items() if solver.Value(pr)) for key, pres in present_by.items()}
    return sol, name, ms


def _follow(m: cp_model.CpModel, a: dict, b: dict, headway: int, name: str) -> None:
    """Two trains of the same direction on one segment: one after the other with the headway at both ends."""
    if a["current"] and b["current"]:  # both already on the line: their order is given by where they are
        lead, foll = (a, b) if a["progress"] >= b["progress"] else (b, a)
        m.Add(foll["arr"] >= lead["arr"])
        return
    if a["current"] or b["current"]:  # the one on the line goes first
        lead, foll = (a, b) if a["current"] else (b, a)
        m.Add(foll["arr"] >= lead["arr"] + headway)
        return
    if a["est"][1] + WINDOW_SLACK_S < b["est"][0]:  # far apart: natural order, no decision needed
        lead, foll = a, b
    elif b["est"][1] + WINDOW_SLACK_S < a["est"][0]:
        lead, foll = b, a
    else:
        first = m.NewBoolVar(f"af_{name}")
        m.Add(b["dep"] >= a["dep"] + headway).OnlyEnforceIf(first)
        m.Add(b["arr"] >= a["arr"] + headway).OnlyEnforceIf(first)
        m.Add(a["dep"] >= b["dep"] + headway).OnlyEnforceIf(first.Not())
        m.Add(a["arr"] >= b["arr"] + headway).OnlyEnforceIf(first.Not())
        return
    m.Add(foll["dep"] >= lead["dep"] + headway)
    m.Add(foll["arr"] >= lead["arr"] + headway)


def objective(tasks: list[Task], sol: Solution, settings: dict, lambda_stop_mult: float = 1.0) -> float:
    """The model's objective evaluated on any solution (e.g. the current order re-timed), same units."""
    lam = settings["planner"]["lambda_stop"] * lambda_stop_mult
    total = 0.0
    for t in tasks:
        times = sol[t.train_id]
        for n, (arr, dep, stop) in zip(t.nodes, times):
            if (n.kind != "origin" and n.sched_arr is not None and n.arr_fixed is None
                    and (n.kind == "dest" or (n.sched_stop and t.category != "freight"))):
                total += t.weight * max(0, arr - n.sched_arr)
            if n.kind == "mid" and not n.stop_fixed and stop:
                total += lam
            # a re-timed plan lands on whole seconds while "now" may be fractional: a second or two off the
            # instruction is not a deviation (the service calls it violated only beyond a minute)
            if n.pin_arr is not None:
                total += PIN_PENALTY * max(0, abs(arr - n.pin_arr) - PIN_TOLERANCE_S)
            if n.pin_dep is not None:
                total += PIN_PENALTY * max(0, abs(dep - n.pin_dep) - PIN_TOLERANCE_S)
            if n.kind == "mid":
                total += LAMBDA_WAIT * max(0, dep - arr - n.dwell_min - WAIT_FREE_S)
            elif n.kind == "origin" and n.sched_dep is not None:
                total += LAMBDA_WAIT * max(0, dep - n.sched_dep - WAIT_FREE_S)
        total += EPS_FINAL * times[-1][0]
    return total
