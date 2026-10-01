"""Conflict forecast — CDR (CLAUDE.md §12.7).

Every train is forecast alone ("free forecast"): from where it is now, with its lag and the active incidents, not
earlier than the approved plan, ignoring all other trains. Where these forecasts intersect, the approved plan no
longer holds: oncoming trains on one segment (`head_on`), a follower closer than the headway or overtaking on the
line (`following`), more trains at a station than tracks (`track`). Shown on the graph before it happens.
"""

from __future__ import annotations

from app.common.config import segment_clear_s
from app.railcore.evaluate import Deadlock, evaluate
from app.railcore.infra import World
from app.railcore.problem import Snapshot, build_tasks
from app.railcore.running_time import RunningTimes


def forecast_conflicts(snap: Snapshot, world: World, rts: RunningTimes, settings: dict) -> list[dict]:
    now = snap.now
    horizon = now + settings["planner"]["horizon_s"]
    clear = segment_clear_s(settings)
    headway = settings["intervals"]["headway_s"]
    # a lag the planner re-times by itself (refresh) is not a conflict to show
    tol = settings["planner"]["replan_deviation_s"]
    runs: dict[str, list[tuple[float, float, str, str]]] = {}
    visits: dict[str, list[tuple[float, float, str]]] = {}
    for task in build_tasks(snap, world, rts, settings):
        try:
            sol = evaluate([task], snap.hint, clear, now, settings["intervals"])[task.train_id]
        except Deadlock:
            continue
        d = task.direction.value
        if task.current:
            runs.setdefault(task.current.segment_id, []).append((now, now + sol[0][0], task.train_id, d))
        for i, (a, dep, _) in enumerate(sol):
            n = task.nodes[i]
            if n.kind == "mid":
                visits.setdefault(n.station_id, []).append((now + a, now + dep, task.train_id))
            if i < len(task.legs):
                runs.setdefault(task.legs[i].segment_id, []).append((now + dep, now + sol[i + 1][0], task.train_id, d))
    out = []
    for seg, rs in runs.items():
        rs.sort()
        for x, (s1, e1, a, da) in enumerate(rs):
            for s2, e2, b, db in rs[x + 1:]:
                if s2 > horizon or s2 > e1 + max(clear, headway):
                    continue
                if da != db and s2 < e1 + clear - tol:
                    out.append({"kind": "head_on", "resource_id": seg, "time_from": s2, "time_to": min(e1, e2),
                                "trains": [a, b]})
                elif da == db and (e2 < e1 + headway - tol or (s1 > now + 1 and s2 < s1 + headway - tol)):
                    # a train already on the line entered before "now": only the arrivals can be compared
                    out.append({"kind": "following", "resource_id": seg, "time_from": s2, "time_to": max(e1, e2),
                                "trains": [a, b]})
    for st, vs in visits.items():
        cap = world.capacity(st)
        events = sorted([(a, 1, t) for a, _, t in vs] + [(d, -1, t) for _, d, t in vs])
        level, inside = 0, set()
        for t, delta, tid in events:
            if delta > 0:
                inside.add(tid)
            else:
                inside.discard(tid)
            level += delta
            if level > cap and t <= horizon:
                out.append({"kind": "track", "resource_id": st, "time_from": t, "time_to": t, "trains": sorted(inside)})
    out.sort(key=lambda c: c["time_from"])
    return out[:50]
