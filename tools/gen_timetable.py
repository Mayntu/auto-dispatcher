"""Generate data/timetable.json (CLAUDE.md §9.3).

1. Desired timetable: minimum running time + 5% reserve, dwell by category.
2. The desired timetable is far over single-track capacity (~5 h total delay), so it is
   de-conflicted once with a long CP-SAT run; the resulting meets become scheduled
   technical stops. Normal operation then starts with the index in "norm".
"""

from __future__ import annotations

import json

from app.common.config import get_env
from app.railcore.infra import load_world
from app.railcore.models import Direction
from app.railcore.running_time import RunningTimes

RESERVE = 1.05
EPOCH_MIN = 7 * 60 + 55

# id, category, direction, departure HH:MM, {station: dwell minutes}
TRAINS = [
    ("2002", "freight", "even", "08:00", {}),
    ("2001", "freight", "odd", "08:05", {}),
    ("101", "express", "odd", "08:10", {"STP": 2}),
    ("102", "express", "even", "08:20", {"STP": 2}),
    ("341", "passenger", "odd", "08:30", {"STP": 1, "OZR": 1}),
    ("342", "passenger", "even", "08:45", {"OZR": 1, "STP": 1}),
    ("2003", "freight", "odd", "08:50", {}),
    ("2004", "freight", "even", "09:00", {}),
    ("2005", "freight", "odd", "09:30", {}),
    ("2006", "freight", "even", "09:40", {}),
]


def sim_s(hhmm: str) -> float:
    h, m = map(int, hhmm.split(":"))
    return (h * 60 + m - EPOCH_MIN) * 60.0


def deconflict(world, raw: list[dict], time_limit_s: float = 30.0) -> list[dict]:
    from app.common.config import load_settings
    from app.planner.model import solve_cpsat
    from app.railcore.models import Train
    from app.railcore.problem import Snapshot, build_tasks

    settings = load_settings()
    trains = [Train.model_validate(t) for t in raw]
    snap = Snapshot(now=0, trains=trains)
    tasks = build_tasks(snap, world, RunningTimes(world), settings)
    sol, status, _ = solve_cpsat(tasks, settings, [], 0, time_limit_s=time_limit_s)
    assert sol is not None, status
    out = []
    for t, task in zip(raw, tasks):
        times = sol[task.train_id]
        stops = []
        for k, (node, (arr, dep, stop)) in enumerate(zip(task.nodes, times)):
            src = t["stops"][k]
            if node.kind == "origin":
                stops.append({**src, "dep": float(dep)})
            elif node.kind == "dest":
                stops.append({**src, "arr": float(arr)})
            else:
                stops.append({**src, "arr": float(arr), "dep": float(dep), "stop": bool(stop or src["stop"])})
        out.append({**t, "stops": stops})
    print(f"de-conflicted with CP-SAT ({status})")
    return out


def main() -> None:
    world = load_world(with_timetable=False)
    rts = RunningTimes(world)
    trains = []
    for tid, cat_id, direction, dep, dwells in TRAINS:
        d = Direction(direction)
        cat = world.categories[cat_id]
        route = world.station_order if d == Direction.ODD else world.station_order[::-1]
        t = sim_s(dep)
        stops = [{"station_id": route[0], "arr": None, "dep": t, "stop": True, "min_dwell_s": 0}]
        for i in range(len(route) - 1):
            a, b = route[i], route[i + 1]
            last = i + 1 == len(route) - 1
            rt = rts.get(cat_id, world.segment_between(a, b).id, d)
            run = rt.t_pp + rt.sup_start * (i == 0 or a in dwells) + rt.sup_end * (last or b in dwells)
            t = round(t + run * RESERVE)
            if last:
                stops.append({"station_id": b, "arr": t, "dep": None, "stop": True, "min_dwell_s": 0})
            elif b in dwells:
                stops.append({"station_id": b, "arr": t, "dep": t + dwells[b] * 60, "stop": True,
                              "min_dwell_s": cat.min_dwell_s})
                t += dwells[b] * 60
            else:
                stops.append({"station_id": b, "arr": t, "dep": t, "stop": False, "min_dwell_s": 0})
        trains.append({"id": tid, "category": cat_id, "direction": direction, "stops": stops})
    trains = deconflict(world, trains)
    out = get_env().data_dir / "timetable.json"
    out.write_text(json.dumps({"trains": trains}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {out} ({len(trains)} trains)")


if __name__ == "__main__":
    main()
