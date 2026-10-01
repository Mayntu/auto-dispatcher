"""Generate data/timetable.json — continuous traffic for 48 h (CLAUDE.md §9.3).

1. Desired timetable from the daily template (fixed seed): running time + 5% reserve, dwell by category,
   freight departures jittered ±10 min.
2. The normative timetable is de-conflicted with CP-SAT in 6-hour windows advancing by 4 hours: each window
   pins everything decided earlier and re-solves the 2-hour overlap, so there are no seams at window
   borders. Meets the solver needs become scheduled technical stops — like a real working timetable (ГИД).

    python -m tools.gen_timetable            # ~ a few minutes
"""

from __future__ import annotations

import copy
import json
import random
import time

from app.common.config import get_env, load_settings
from app.planner.model import solve_cpsat
from app.railcore.infra import load_world
from app.railcore.models import Direction, Train
from app.railcore.problem import Snapshot, build_tasks
from app.railcore.running_time import RunningTimes

RESERVE = 1.05
EPOCH_MIN = 7 * 60 + 55
HOURS = 48
GEN_HOURS = HOURS + 4  # the ГИД window looks 3 h ahead: keep it full up to the 48th hour
SEED = 2026
WINDOW_H, STEP_H = 6, 4
WINDOW_TIME_LIMIT_S = 20.0

# category, direction, first departure HH:MM, period min, first number, dwell {station: min}, jitter min
TEMPLATE = [
    ("express", "odd", "08:10", 360, 101, {"STP": 2}, 0),
    ("express", "even", "08:20", 360, 102, {"STP": 2}, 0),
    ("passenger", "odd", "08:30", 180, 341, {"STP": 1, "OZR": 1}, 0),
    ("passenger", "even", "08:45", 180, 342, {"OZR": 1, "STP": 1}, 0),
    ("freight", "odd", "08:05", 90, 2001, {}, 10),
    ("freight", "even", "08:00", 90, 2002, {}, 10),
]


def sim_s(hhmm: str) -> float:
    h, m = map(int, hhmm.split(":"))
    return (h * 60 + m - EPOCH_MIN) * 60.0


def desired(world, rts) -> list[dict]:
    rng = random.Random(SEED)
    trains = []
    for cat_id, direction, first, period, number, dwells, jitter in TEMPLATE:
        d = Direction(direction)
        cat = world.categories[cat_id]
        route = world.station_order if d == Direction.ODD else world.station_order[::-1]
        dep0, k = sim_s(first), 0
        while dep0 + k * period * 60 < GEN_HOURS * 3600:
            t = dep0 + k * period * 60 + (rng.uniform(-jitter, jitter) * 60 if jitter and k else 0)
            t = max(60.0, round(t))
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
            trains.append({"id": str(number + 2 * k), "category": cat_id, "direction": direction, "stops": stops})
            k += 1
    return sorted(trains, key=lambda t: t["stops"][0]["dep"])


def deconflict(world, raw: list[dict]) -> list[dict]:
    settings = copy.deepcopy(load_settings())
    settings["planner"]["horizon_s"] = (GEN_HOURS + 12) * 3600  # every train of a window is in the model
    rts = RunningTimes(world)
    by_id = {t["id"]: t for t in raw}
    pinned: dict[str, list[tuple[int, int, bool]]] = {}
    end_of = {}
    t0 = sim_s("08:00")
    w = 0
    while t0 + w * STEP_H * 3600 < GEN_HOURS * 3600:
        lo = t0 + w * STEP_H * 3600
        hi = lo + WINDOW_H * 3600
        free = [t for t in raw if t["id"] not in pinned and t["stops"][0]["dep"] < hi]
        # pinned trains still on the line during the window constrain it
        fixed = [t for t in raw if t["id"] in pinned and end_of[t["id"]] > lo - 3600]
        trains = [Train.model_validate(t) for t in fixed + free]
        snap = Snapshot(now=0, trains=trains)
        tasks = build_tasks(snap, world, rts, settings)
        sol, status, _ = solve_cpsat(tasks, settings, [], 0, time_limit_s=WINDOW_TIME_LIMIT_S,
                                     fix={t["id"]: pinned[t["id"]] for t in fixed})
        assert sol is not None, f"window {w}: {status}"
        commit_until = lo + STEP_H * 3600 if hi < GEN_HOURS * 3600 else float("inf")
        n_new = 0
        for t in free:
            if t["stops"][0]["dep"] < commit_until:  # the overlap stays free for the next window
                pinned[t["id"]] = sol[t["id"]]
                end_of[t["id"]] = sol[t["id"]][-1][0]
                n_new += 1
        print(f"window {w}: {len(fixed)} pinned + {len(free)} free, {status}, committed {n_new}", flush=True)
        w += 1

    out = []
    for t in raw:
        times = pinned[t["id"]]
        stops = []
        for k, (src, (arr, dep, stop)) in enumerate(zip(t["stops"], times)):
            if k == 0:
                stops.append({**src, "dep": float(dep)})
            elif k == len(times) - 1:
                stops.append({**src, "arr": float(arr)})
            else:  # a meet the solver needed becomes a scheduled technical stop
                stops.append({**src, "arr": float(arr), "dep": float(dep), "stop": bool(stop or src["stop"])})
        out.append({**t, "stops": stops})
    return out


def main() -> None:
    world = load_world(with_timetable=False)
    rts = RunningTimes(world)
    t0 = time.time()
    raw = desired(world, rts)
    print(f"desired timetable: {len(raw)} trains over {GEN_HOURS} h")
    trains = deconflict(world, raw)
    out = get_env().data_dir / "timetable.json"
    out.write_text(json.dumps({"trains": trains}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {out} ({len(trains)} trains) in {time.time() - t0:.0f} s")


if __name__ == "__main__":
    main()
