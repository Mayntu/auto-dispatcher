"""Fixed-order evaluation — the "alternative graph" (CLAUDE.md §12.5).

Given the order of trains on every segment (taken from a plan), earliest event times are the
longest path in a DAG of arr/dep events. A cycle means the order is a deadlock.
Used for: current forecast (index), robustness (max durations), fallback when CP-SAT fails.
"""

from __future__ import annotations

from collections import defaultdict, deque

from app.railcore.models import PlanEntry
from app.railcore.problem import Solution, Task


class Deadlock(Exception):
    def __init__(self, trains: list[str]):
        super().__init__(f"deadlock between trains {', '.join(trains)}")
        self.trains = trains
        self.cycle: list[tuple] = []  # one cycle of events (train, node index, a|d) — for diagnostics


def _find_cycle(edges: dict, left: set) -> list[tuple]:
    """A cycle among the events left after the topological pass: each of them still has a predecessor
    there, so walking predecessors must close a loop."""
    pred: dict[tuple, tuple] = {}
    for u, outs in edges.items():
        if u in left:
            for w, _ in outs:
                if w in left:
                    pred.setdefault(w, u)
    v = next(iter(left), None)
    path, pos = [], {}
    while v is not None and v not in pos:
        pos[v] = len(path)
        path.append(v)
        v = pred.get(v)
    return list(reversed(path[pos[v]:])) if v is not None else []


DEFAULT_INTERVALS = {"headway_s": 480, "tau_np_s": 180}


def evaluate(tasks: list[Task], order: list[PlanEntry], clear_s: int, now: float = 0.0,
             intervals: dict | None = None) -> Solution:
    """`order` entries are in absolute sim time; task times are relative to `now`.
    Automatic block (§12.5): oncoming trains in plan order need the segment cleared (arr + clear -> dep);
    following trains keep the headway at both ends; at stations without simultaneous reception oncoming
    arrivals are tau_np apart; station tracks follow the plan's track assignment where known."""
    iv = {**DEFAULT_INTERVALS, **(intervals or {})}
    plan_track = {(e.train_id, e.station_id): e.track_id for e in order if e.kind == "dwell" and e.track_id}
    plan_start = {(e.train_id, e.segment_id): e.start - now for e in order if e.kind == "run"}
    plan_stop = {(e.train_id, e.station_id): e.stop for e in order if e.kind == "dwell"}
    # the field never runs ahead of the plan: planned times are lower bounds, deviations only push later
    plan_time = {(e.train_id, e.station_id): (e.start - now, e.end - now) for e in order if e.kind == "dwell"}
    stops = {
        (t.train_id, i): n.stop_fixed or plan_stop.get((t.train_id, n.station_id), False)
        for t in tasks for i, n in enumerate(t.nodes)
    }
    # "run slower instead of stopping" moves the arrival later; that later arrival must also hold the segment
    # for the next train, so it becomes a lower bound and everything is recomputed until it settles
    arr_lb: dict[tuple, float] = {}
    # tau_np: at a siding oncoming trains arrive tau_np apart, in the order they actually come — applied as lower
    # bounds after each pass (an edge in plan order could contradict the segment order and fake a deadlock)
    np_lb: dict[tuple, float] = {}
    sidings = [(t, i) for t in tasks for i, n in enumerate(t.nodes)
               if not n.simultaneous_reception and n.kind != "origin"]
    times: dict = {}
    for _ in range(12):
        lbs = {k: max(arr_lb.get(k, 0.0), np_lb.get(k, 0.0)) for k in arr_lb.keys() | np_lb.keys()}
        times = None
        # the plan's track use may no longer fit reality: retry keeping only time-consistent track edges, then
        # with the segment order alone (station capacity is then held by the field, not by the forecast)
        for mode in ("strict", "consistent", "segments"):
            try:
                times = _longest_path(tasks, plan_start, plan_time, plan_track, stops, clear_s, iv, lbs, mode)
                break
            except Deadlock:
                if mode == "segments":
                    raise
        changed = False
        by_station: dict[str, list] = defaultdict(list)
        for t, i in sidings:
            by_station[t.nodes[i].station_id].append((times[(t.train_id, i, "a")], t, i))
        for arrs in by_station.values():
            arrs.sort(key=lambda x: x[0])
            for (ta, a, _), (tb, b, ib) in zip(arrs, arrs[1:]):
                if a.direction != b.direction and tb < ta + iv["tau_np_s"] - 0.5 and b.nodes[ib].arr_fixed is None:
                    np_lb[(b.train_id, ib, "a")] = ta + iv["tau_np_s"]
                    changed = True
        for t in tasks:
            for i, n in enumerate(t.nodes):
                key = (t.train_id, i, "a")
                arr, dep = times[key], times[(t.train_id, i, "d")]
                wait = dep - arr - n.pass_threshold
                # a stop the plan decided stays a stop (the order around it — tau_np, crossings — was built on it);
                # only trains the plan lets pass choose between easing off and stopping
                s = n.stop_fixed or plan_stop.get((t.train_id, n.station_id), False)
                pinned = n.pin_arr is not None or n.pin_dep is not None
                if not s and n.kind == "mid" and wait > 1 and i > 0 and not pinned:
                    # like the CP-SAT model: run slower (up to 1.3x) to pass without stopping when possible —
                    # but never slower than that cap; otherwise the train stops at the station
                    latest = times[(t.train_id, i - 1, "d")] + _run_cap(t, i)
                    if dep - n.pass_threshold <= latest:
                        arr_lb[key] = dep - n.pass_threshold
                        changed = True
                    else:
                        s = True
                        arr_lb.pop(key, None)
                elif not s and n.kind == "mid" and wait > 1:
                    s = True
                if s != stops[(t.train_id, i)]:
                    stops[(t.train_id, i)] = s
                    changed = True
        if not changed:
            break
    return {
        t.train_id: [(round(times[(t.train_id, i, "a")]), round(times[(t.train_id, i, "d")]), stops[(t.train_id, i)])
                     for i in range(len(t.nodes))]
        for t in tasks
    }


def _run_cap(t: Task, i: int) -> float:
    """Longest run into node i that still beats stopping there: the model's 1.3x of the full running time,
    plus what a stop would cost anyway (braking + restart). A train a few seconds late eases off, it does
    not stop — otherwise a 10 s lag turns into a 2 min stop that ripples through every crossing."""
    leg = t.legs[i - 1]
    return round(leg.max_factor * (leg.t_pp + leg.sup_start + leg.sup_end)) + leg.sup_start + leg.sup_end


def _longest_path(tasks: list[Task], plan_start: dict, plan_time: dict, plan_track: dict, stops: dict,
                  clear_s: int, iv: dict, arr_lb: dict, mode: str = "strict") -> dict:
    headway, tau_np = iv["headway_s"], iv["tau_np_s"]
    direction = {t.train_id: t.direction.value for t in tasks}
    lb: dict[tuple, float] = {}
    edges: dict[tuple, list[tuple[tuple, float]]] = defaultdict(list)
    users: dict[str, list[tuple[float, tuple, tuple]]] = defaultdict(list)  # segment -> (key, start node, end node)

    for t in tasks:
        tid = t.train_id
        for i, n in enumerate(t.nodes):
            a, d = (tid, i, "a"), (tid, i, "d")
            pa, pd = plan_time.get((tid, n.station_id), (0.0, 0.0))
            if n.free_arr:
                pa = 0.0
            if n.free_dep:
                pd = 0.0
            lb[a] = float(n.arr_fixed) if n.arr_fixed is not None else max(0.0, pa, arr_lb.get(a, 0.0))
            lb[d] = max(float(n.dep_min), pd)
            if n.pin_arr is not None:  # the dispatcher's instruction: not earlier than that
                lb[a] = max(lb[a], float(n.pin_arr))
            if n.pin_dep is not None:
                lb[d] = max(lb[d], float(n.pin_dep))
            dwell = n.dest_dwell if n.kind == "dest" else n.dwell_min
            edges[a].append((d, dwell))
        if t.current:
            lb[(tid, 0, "a")] = max(lb[(tid, 0, "a")], t.current.remaining + t.current.sup_end * stops[(tid, 0)])
            # trains on the segment now come first, leader before followers (a packet keeps its order)
            users[t.current.segment_id].append((-1e12 - t.current.progress, None, (tid, 0, "a"), float("-inf")))
        for i, leg in enumerate(t.legs):
            run = leg.t_pp + leg.sup_start * stops[(tid, i)] + leg.sup_end * stops[(tid, i + 1)]
            if leg.fixed_run is not None:
                run = max(run, leg.fixed_run)
            edges[(tid, i, "d")].append(((tid, i + 1, "a"), run))
            key = plan_start.get((tid, leg.segment_id))
            sched = t.nodes[i].sched_dep if t.nodes[i].sched_dep is not None else float("inf")
            users[leg.segment_id].append((key, (tid, i, "d"), (tid, i + 1, "a"), sched))

    for seg, seg_users in users.items():
        planned = [u for u in seg_users if u[0] is not None]
        ordered = []
        for u in seg_users:
            if u[0] is not None:
                ordered.append((u[0], 0, u))
                continue
            # a train new to the horizon is queued by its timetable time, but never ahead of a planned train
            # that the timetable sent onto this segment earlier — otherwise late trains starve behind every
            # newcomer (a late freight waiting hours while passenger trains keep entering the horizon)
            floor = max((p[0] for p in planned if p[3] <= u[3]), default=float("-inf"))
            ordered.append((max(u[3], floor), 1, u))
        ordered.sort(key=lambda x: (x[0], x[1]))
        seg_users[:] = [u for _, _, u in ordered]
        for (_, start_a, end_a, _), (_, start_b, end_b, _) in zip(seg_users, seg_users[1:]):
            if direction[end_a[0]] != direction[end_b[0]]:  # oncoming: the segment must be cleared first
                if start_b is not None:
                    edges[end_a].append((start_b, clear_s))
            elif start_b is None:  # both on the segment now: the follower arrives after the leader
                edges[end_a].append((end_b, 0.0))
            else:  # following in a packet: the headway at both ends, no overtaking on the line
                if start_a is not None:
                    edges[start_a].append((start_b, headway))
                edges[end_a].append((end_b, headway))

    # station tracks assigned by the plan: on each track the next train comes only after the previous left
    # (only where the plan's own times agree: a train standing on another track than the plan said — the plan
    # being re-timed is older than reality — must not create a false deadlock; station capacity still holds)
    by_track: dict[tuple[str, str], list[tuple[float, float, str, int]]] = defaultdict(list)
    for t in tasks:
        for i, n in enumerate(t.nodes):
            tr = n.track_fixed or plan_track.get((t.train_id, n.station_id))
            pt = plan_time.get((t.train_id, n.station_id))
            if tr is None or pt is None or mode == "segments":
                continue
            here = (n.arr_fixed is not None and n.arr_fixed <= 0) or (i == 0 and t.current)
            by_track[(n.station_id, tr)].append((float("-inf") if here else pt[0], pt[1], t.train_id, i))
    for seq in by_track.values():
        seq.sort(key=lambda x: x[0])
        for (_, end_a, ta, ia), (kb, _, tb, ib) in zip(seq, seq[1:]):
            if ta != tb and end_a <= kb + 1:
                target = (tb, ib - 1, "d") if ib > 0 else (tb, ib, "a")
                edges[(ta, ia, "d")].append((target, 0.0))

    # station tracks (§12.5): spread visits over the tracks in plan order; on each track the next
    # train arrives only after the previous one has left
    visits: dict[tuple[str, str], list[tuple[float, float, str, int]]] = defaultdict(list)
    capacity: dict[tuple[str, str], int] = {}
    for t in tasks:
        for i, n in enumerate(t.nodes):
            # all trains share the station's tracks; one direction alone gets at most capacity-1 of them
            # (the field keeps a track for opposite traffic) — the same rules as CP-SAT and the field
            capacity[(n.station_id, "*")] = n.capacity
            capacity[(n.station_id, t.direction.value)] = max(1, n.capacity - 1)
            if (n.arr_fixed is not None and n.arr_fixed <= 0) or (i == 0 and t.current):  # there or heading in
                start, end = float("-inf"), plan_time.get((t.train_id, n.station_id), (0.0, 0.0))[1]
            elif (t.train_id, n.station_id) in plan_time:
                start, end = plan_time[(t.train_id, n.station_id)]
                if i > 0 and (t.train_id, t.nodes[i - 1].station_id) in plan_time:
                    start = plan_time[(t.train_id, t.nodes[i - 1].station_id)][1]  # held from the departure
            else:
                start = float(n.sched_arr if n.sched_arr is not None else (n.arr_fixed or 0))
                end = float(n.sched_dep if n.sched_dep is not None else start + n.dest_dwell)
            visits[(n.station_id, "*")].append((start, end, t.train_id, i))
            visits[(n.station_id, t.direction.value)].append((start, end, t.train_id, i))
    for st, vs in visits.items():
        vs.sort(key=lambda v: (v[0], v[1]))
        free: list[tuple[float, tuple | None]] = [(float("-inf"), None)] * capacity[st]
        for start, end, tid, i in vs:
            k = min(range(len(free)), key=lambda j: free[j][0])
            prev_end, prev = free[k]
            # only edges that agree with the plan's own times: a greedy track that is still busy at this
            # arrival would add a backwards edge (a false deadlock cycle)
            if mode == "segments":
                break
            if prev is not None and prev[0] != tid and (mode == "strict" or prev_end <= start + 1):
                # the track is taken when the train leaves the previous station (DC sets the arrival route then)
                target = (tid, i - 1, "d") if i > 0 else (tid, i, "a")
                edges[(prev[0], prev[1], "d")].append((target, 0.0))
            free[k] = (end, (tid, i))

    indeg = {v: 0 for v in lb}
    for u in edges:
        for v, _ in edges[u]:
            indeg[v] += 1
    queue = deque(v for v, k in indeg.items() if k == 0)
    val = dict(lb)
    seen = 0
    while queue:
        u = queue.popleft()
        seen += 1
        for v, w in edges[u]:
            val[v] = max(val[v], val[u] + w)
            indeg[v] -= 1
            if indeg[v] == 0:
                queue.append(v)
    if seen < len(lb):
        err = Deadlock(sorted({v[0] for v, k in indeg.items() if k > 0}))
        err.cycle = _find_cycle(edges, {v for v, k in indeg.items() if k > 0})
        raise err
    return val
