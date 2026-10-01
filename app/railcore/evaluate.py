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


def evaluate(tasks: list[Task], order: list[PlanEntry], clear_s: int, now: float = 0.0) -> Solution:
    """`order` entries are in absolute sim time; task times are relative to `now`."""
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
    times: dict = {}
    for _ in range(10):
        try:
            times = _longest_path(tasks, plan_start, plan_time, stops, clear_s, arr_lb, strict_tracks=True)
        except Deadlock:  # the plan's track use no longer fits reality: keep only time-consistent track edges
            times = _longest_path(tasks, plan_start, plan_time, stops, clear_s, arr_lb, strict_tracks=False)
        changed = False
        for t in tasks:
            for i, n in enumerate(t.nodes):
                key = (t.train_id, i, "a")
                arr, dep = times[key], times[(t.train_id, i, "d")]
                wait = dep - arr - n.pass_threshold
                s = n.stop_fixed
                if not s and n.kind == "mid" and wait > 1 and i > 0:
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
    return round(1.3 * (leg.t_pp + leg.sup_start + leg.sup_end)) + leg.sup_start + leg.sup_end


def _longest_path(tasks: list[Task], plan_start: dict, plan_time: dict, stops: dict, clear_s: int,
                  arr_lb: dict, strict_tracks: bool = True) -> dict:
    lb: dict[tuple, float] = {}
    edges: dict[tuple, list[tuple[tuple, float]]] = defaultdict(list)
    users: dict[str, list[tuple[float, tuple, tuple]]] = defaultdict(list)  # segment -> (key, start node, end node)

    for t in tasks:
        tid = t.train_id
        for i, n in enumerate(t.nodes):
            a, d = (tid, i, "a"), (tid, i, "d")
            pa, pd = plan_time.get((tid, n.station_id), (0.0, 0.0))
            lb[a] = float(n.arr_fixed) if n.arr_fixed is not None else max(0.0, pa, arr_lb.get(a, 0.0))
            lb[d] = max(float(n.dep_min), pd)
            dwell = n.dest_dwell if n.kind == "dest" else n.dwell_min
            edges[a].append((d, dwell))
        if t.current:
            lb[(tid, 0, "a")] = max(lb[(tid, 0, "a")], t.current.remaining + t.current.sup_end * stops[(tid, 0)])
            # trains on the segment now come first, leader before followers (a packet keeps its order)
            users[t.current.segment_id].append((-1e12 - t.current.progress, None, (tid, 0, "a"), float("-inf")))
        for i, leg in enumerate(t.legs):
            run = leg.t_pp + leg.sup_start * stops[(tid, i)] + leg.sup_end * stops[(tid, i + 1)]
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
        for (_, _, end_a, _), (_, start_b, end_b, _) in zip(seg_users, seg_users[1:]):
            if start_b is not None:
                edges[end_a].append((start_b, clear_s))
            else:  # both on the segment now: the follower arrives after the leader
                edges[end_a].append((end_b, 0.0))

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
            if prev is not None and prev[0] != tid and (strict_tracks or prev_end <= start + 1):
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
        raise Deadlock(sorted({v[0] for v, k in indeg.items() if k > 0}))
    return val
