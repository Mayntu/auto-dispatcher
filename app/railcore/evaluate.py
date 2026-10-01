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


def evaluate(tasks: list[Task], order: list[PlanEntry], clear_s: int) -> Solution:
    plan_start = {(e.train_id, e.segment_id): e.start for e in order if e.kind == "run"}
    plan_stop = {(e.train_id, e.station_id): e.stop for e in order if e.kind == "dwell"}
    stops = {
        (t.train_id, i): n.stop_fixed or plan_stop.get((t.train_id, n.station_id), False)
        for t in tasks for i, n in enumerate(t.nodes)
    }
    for _ in range(3):
        times = _longest_path(tasks, plan_start, stops, clear_s)
        changed = False
        for t in tasks:
            for i, n in enumerate(t.nodes):
                arr, dep = times[(t.train_id, i, "a")], times[(t.train_id, i, "d")]
                s = n.stop_fixed or (n.kind == "mid" and dep - arr > n.pass_threshold + 1)
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


def _longest_path(tasks: list[Task], plan_start: dict, stops: dict, clear_s: int) -> dict:
    lb: dict[tuple, float] = {}
    edges: dict[tuple, list[tuple[tuple, float]]] = defaultdict(list)
    users: dict[str, list[tuple[float, tuple, tuple]]] = defaultdict(list)  # segment -> (key, start node, end node)

    for t in tasks:
        tid = t.train_id
        for i, n in enumerate(t.nodes):
            a, d = (tid, i, "a"), (tid, i, "d")
            lb[a] = float(n.arr_fixed) if n.arr_fixed is not None else 0.0
            lb[d] = float(n.dep_min)
            dwell = n.dest_dwell if n.kind == "dest" else n.dwell_min
            edges[a].append((d, dwell))
        if t.current:
            lb[(tid, 0, "a")] = max(lb[(tid, 0, "a")], t.current.remaining + t.current.sup_end * stops[(tid, 0)])
            users[t.current.segment_id].append((float("-inf"), None, (tid, 0, "a")))
        for i, leg in enumerate(t.legs):
            run = leg.t_pp + leg.sup_start * stops[(tid, i)] + leg.sup_end * stops[(tid, i + 1)]
            edges[(tid, i, "d")].append(((tid, i + 1, "a"), run))
            key = plan_start.get((tid, leg.segment_id), float("inf"))
            if key == float("inf") and t.nodes[i].sched_dep is not None:
                key = 1e9 + t.nodes[i].sched_dep  # not in the plan: after planned trains, by timetable
            users[leg.segment_id].append((key, (tid, i, "d"), (tid, i + 1, "a")))

    for seg_users in users.values():
        seg_users.sort(key=lambda u: u[0])
        for (_, _, end_a), (_, start_b, _) in zip(seg_users, seg_users[1:]):
            if start_b is not None:
                edges[end_a].append((start_b, clear_s))

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
