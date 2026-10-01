"""The dispatcher's instructions from the train graph (tasks/02-manual-drag-backend.md).

The dispatcher drags the arrival or departure of a train at a station. This module gives the drag bounds,
a fast preview (fixed order, the current plan's times as lower bounds for everybody else, no CP-SAT) and the
"keep order" plan for commit; "reoptimize" is a CP-SAT solve with the instruction as a pin (`planner.main`).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from app.common.config import get_env, segment_clear_s
from app.railcore.evaluate import Deadlock, evaluate
from app.railcore.explain import train_label
from app.railcore.infra import World
from app.railcore.models import Pin, Plan, TrainStatus
from app.railcore.problem import Snapshot, Task, assemble_plan, build_tasks
from app.railcore.running_time import RunningTimes

CATEGORY_PRIORITY_SCHEDULE = {"express", "passenger"}


class ManualError(Exception):
    """`code`: http status for the API (404 not_found, 409 stale_plan, 422 invalid_kind, 423 locked)."""

    def __init__(self, code: int, error: str, **extra):
        super().__init__(error)
        self.code, self.error, self.extra = code, error, extra

    def body(self) -> dict:
        return {"error": self.error, **self.extra}


def clock(t: float) -> str:
    epoch = get_env().sim_epoch
    s = round(t) + epoch.hour * 3600 + epoch.minute * 60
    return f"{(s // 3600) % 24:02d}:{(s % 3600) // 60:02d}"


def reason(code: str, text: str) -> dict:
    return {"code": code, "text": text}


@dataclass
class Point:
    """One station of one train, as the current plan sees it."""

    task: Task
    i: int  # node index in the task (nodes start at the train's current position)
    k: int  # index on the train's route
    last: int  # route length - 1
    arr: float | None  # planned, absolute sim time
    dep: float | None
    stop: bool


def _tasks(snap: Snapshot, world: World, rts: RunningTimes, settings: dict, skip: tuple[str, str] | None = None):
    pins = [p for p in snap.pins if (p.train_id, p.station_id) != skip]
    return build_tasks(snap.model_copy(update={"pins": pins}), world, rts, settings)


def locate(snap: Snapshot, world: World, rts: RunningTimes, settings: dict, train_id: str, station_id: str,
           plan: Plan) -> Point:
    train = next((t for t in snap.trains if t.id == train_id), None)
    if train is None or station_id not in world.route(train):
        raise ManualError(404, "not_found")
    st = snap.states.get(train_id)
    if train.cancelled or (st is not None and st.status == TrainStatus.FINISHED):
        raise ManualError(423, "locked", reason="Поезд завершил маршрут")
    route = world.route(train)
    k = route.index(station_id)
    task = next((t for t in _tasks(snap, world, rts, settings, (train_id, station_id)) if t.train_id == train_id), None)
    if task is None:
        raise ManualError(423, "locked", reason="Поезд за горизонтом планирования")
    i = next((j for j, n in enumerate(task.nodes) if n.station_id == station_id), None)
    if i is None:
        raise ManualError(423, "locked", reason="Поезд уже отправился")
    dwell = next((e for e in plan.entries if e.kind == "dwell" and e.train_id == train_id and e.station_id == station_id), None)
    if dwell is None:
        raise ManualError(404, "not_found")
    return Point(task, i, k, len(route) - 1, dwell.start, dwell.end, dwell.stop)


def bounds(snap: Snapshot, world: World, rts: RunningTimes, settings: dict, plan: Plan,
           train_id: str, station_id: str) -> dict:
    now = snap.now
    horizon = settings["planner"]["horizon_s"]
    factor = settings["manual"]["max_run_factor"]
    pt = locate(snap, world, rts, settings, train_id, station_id, plan)
    task, i, n = pt.task, pt.i, pt.task.nodes[pt.i]
    cat = world.categories[task.category]
    point_type = "origin" if pt.k == 0 else "destination" if pt.k == pt.last else ("stop" if pt.stop else "pass")
    here = n.arr_fixed is not None and n.arr_fixed <= 0  # the train stands here already
    min_dwell = n.dwell_min if n.kind == "mid" else 0
    other_pins = [p for p in snap.pins if p.train_id == train_id and p.station_id != station_id
                  and p.status in ("active", "violated")]
    info = {"planned_arr": pt.arr if point_type != "origin" else None, "planned_dep": pt.dep if point_type != "destination" else None,
            "sched_arr": None, "sched_dep": None, "min_dwell_s": min_dwell if pt.stop else 0, "t_pass_s": n.pass_threshold,
            "prev_station_id": None, "prev_dep": None, "min_run_s": None, "max_run_s": None,
            "segment_length_m": None, "v_max_kmh": None,
            "pin": next((p.model_dump(mode="json") for p in snap.pins if p.train_id == train_id
                         and p.station_id == station_id and p.status in ("active", "violated")), None)}
    stop = next(s for s in world.trains[train_id].stops if s.station_id == station_id) if train_id in world.trains else None
    if stop is not None:
        info["sched_arr"], info["sched_dep"] = stop.arr, stop.dep

    # ---- departure handle
    dep = None
    if point_type != "destination":
        lo, lo_r = now, reason("now", "Нельзя раньше текущего времени")
        base = (pt.arr if pt.arr is not None else now) + (min_dwell if pt.stop else n.pass_threshold)
        if point_type != "origin" and base > lo:
            lo, lo_r = base, reason("min_dwell", f"Минимальная стоянка {round(min_dwell / 60)} мин" if pt.stop
                                    else "Проход: поезд должен проследовать пункт")
        sched = now + n.dep_min if n.dep_min > 0 else None
        if sched is not None and sched > lo:
            lo, lo_r = sched, reason("schedule", "Не раньше отправления по расписанию")
        hi, hi_r = now + horizon, reason("horizon", "Дальше горизонта планирования")
        run_after = 0.0
        for j in range(i, len(task.legs)):  # a later instruction of this train limits how late it may leave
            run_after += task.legs[j].t_pp
            nxt = task.nodes[j + 1].station_id
            for p in other_pins:
                if p.station_id == nxt and p.time - run_after < hi:
                    hi, hi_r = p.time - run_after, reason("pin", "Ограничено указанием диспетчера по этому поезду")
        locked = pt.dep is not None and pt.dep <= now and not here
        dep = {"current": pt.dep, "min": round(lo), "max": round(max(lo, hi)), "min_reason": lo_r, "max_reason": hi_r,
               "locked": locked, "locked_reason": "Поезд уже отправился" if locked else None}

    # ---- arrival handle
    arr = None
    if point_type != "origin":
        if here:
            arr = {"current": pt.arr, "min": None, "max": None, "min_reason": None, "max_reason": None,
                   "locked": True, "locked_reason": "Поезд уже прибыл"}
        else:
            if task.current is not None and i == 0:  # on the segment into this station now
                leg_in, prev_dep = None, None
                t_min = task.current.remaining + task.current.sup_end * pt.stop
                lo = now + t_min
                hi = now + t_min * factor
                prev_id = None
            else:
                leg_in = task.legs[i - 1]
                prev = task.nodes[i - 1]
                prev_dep = next(e.end for e in plan.entries if e.kind == "dwell" and e.train_id == train_id
                                and e.station_id == prev.station_id)
                prev_stop = next(e.stop for e in plan.entries if e.kind == "dwell" and e.train_id == train_id
                                 and e.station_id == prev.station_id)
                t_min = leg_in.t_pp + leg_in.sup_start * prev_stop + leg_in.sup_end * pt.stop
                lo, hi = max(now, prev_dep + t_min), prev_dep + t_min * factor
                prev_id = prev.station_id
                seg = world.segments[leg_in.segment_id]
                info.update(prev_station_id=prev_id, prev_dep=prev_dep, min_run_s=round(t_min),
                            max_run_s=round(t_min * factor), segment_length_m=seg.length_m,
                            v_max_kmh=min(cat.v_max_kmh, seg.v_max_kmh))
            seg_len = world.segments[leg_in.segment_id].length_m if leg_in else None
            avg = f" (средняя {seg_len / t_min * 3.6:.0f} км/ч)" if seg_len and t_min > 0 else ""
            lo_r = reason("min_run_time", f"Быстрее нельзя: минимальное время хода {round(t_min / 60)} мин{avg}")
            if lo == now:
                lo_r = reason("now", "Нельзя раньше текущего времени")
            prev_name = world.stations[prev_id].name if prev_id else "предыдущего пункта"
            hi_r = reason("max_run_time", f"Медленнее нельзя: задержите отправление с {prev_name}")
            if now + horizon < hi:
                hi, hi_r = now + horizon, reason("horizon", "Дальше горизонта планирования")
            arr = {"current": pt.arr, "min": round(lo), "max": round(max(lo, hi)), "min_reason": lo_r, "max_reason": hi_r,
                   "locked": False, "locked_reason": None}
    return {"base_plan_version": plan.version, "train_id": train_id, "station_id": station_id,
            "point_type": point_type, "arr": arr, "dep": dep, "info": info}


def make_pin(world: World, train_id: str, station_id: str, kind: str, t: float, now: float, from_t: float | None) -> Pin:
    what = "прибытием на" if kind == "arr" else "отправлением с"
    verb = "Задержать" if from_t is None or t >= from_t else "Ускорить"
    delta = f" ({'+' if from_t is None or t >= from_t else '−'}{abs(round((t - (from_t or t)) / 60))} мин)" if from_t is not None else ""
    return Pin(id=uuid.uuid4().hex[:8], train_id=train_id, station_id=station_id, kind=kind, time=t, created_at=now,
               status="active", description=f"{verb} {train_label(world, train_id, acc=True)} {what} "
                                            f"«{world.stations[station_id].name}» до {clock(t)}{delta}")


def preview(snap: Snapshot, world: World, rts: RunningTimes, settings: dict, plan: Plan, current: Plan | None,
            train_id: str, station_id: str, kind: str, t: float) -> dict:
    """Fixed order, everybody else keeps at least their current planned times; the dragged event is pinned."""
    t0 = time.perf_counter()
    if kind not in ("arr", "dep"):
        raise ManualError(422, "invalid_kind")
    b = bounds(snap, world, rts, settings, plan, train_id, station_id)
    h = b[kind]
    if h is None:
        raise ManualError(422, "invalid_kind")
    if h["locked"]:
        raise ManualError(423, "locked", reason=h["locked_reason"])
    snap_s = settings["manual"]["snap_s"]
    want = round(t / snap_s) * snap_s
    tt = min(max(want, h["min"]), h["max"])
    clamped = t < h["min"] - 0.5 or t > h["max"] + 0.5

    pin = make_pin(world, train_id, station_id, kind, tt, snap.now, h["current"])
    pins = [p for p in snap.pins if (p.train_id, p.station_id) != (train_id, station_id)] + [pin]
    psnap = snap.model_copy(update={"pins": pins, "hint": plan.entries})
    tasks = build_tasks(psnap, world, rts, settings)
    task = next(x for x in tasks if x.train_id == train_id)
    i = next(j for j, n in enumerate(task.nodes) if n.station_id == station_id)
    if kind == "dep":  # the train keeps its speed: every later event moves with the departure
        for j in range(i, len(task.nodes)):
            task.nodes[j].free_dep = True
            if j > i:
                task.nodes[j].free_arr = True
        _keep_runs(task, plan, i)
    else:  # only this arrival moves; the run into it takes exactly the dragged time
        task.nodes[i].free_arr = True
        if i > 0:
            prev_dep = next(e.end for e in plan.entries if e.kind == "dwell" and e.train_id == train_id
                            and e.station_id == task.nodes[i - 1].station_id)
            task.legs[i - 1].fixed_run = round(tt - prev_dep)
        _keep_runs(task, plan, i + 1)
    conflicts = []
    try:
        sol = evaluate(tasks, plan.entries, segment_clear_s(settings), snap.now, settings["intervals"])
    except Deadlock as e:
        return {"base_plan_version": plan.version, "kind": kind, "time": tt, "clamped": clamped, "dragged": None,
                "threads": [], "affected": [], "conflicts": [{"kind": "deadlock", "trains": e.trains,
                                                               "text": "При текущем порядке поезда блокируют друг друга"}],
                "total_delay_delta_s": None, "index_forecast": None, "delta_index": None,
                "compute_ms": round((time.perf_counter() - t0) * 1000), "plan": None, "pin": pin}
    new = assemble_plan(tasks, sol, psnap, world, settings, solver="refresh", strategy="keep_order", solve_ms=0)
    got = sol[train_id][i][0 if kind == "arr" else 1] + snap.now
    if abs(got - tt) > 30:
        conflicts.append({"kind": "pin", "trains": [train_id],
                          "text": f"При текущем порядке {clock(tt)} недостижимо: не раньше {clock(got)}"})
    base = current or plan
    out = _diff(world, base, new, train_id, station_id)
    a, d, _ = sol[train_id][i]
    dragged = {"arr": a + snap.now if b["point_type"] != "origin" else None,
               "dep": d + snap.now if b["point_type"] != "destination" else None,
               "dwell_s": d - a if b["point_type"] not in ("origin", "destination") else None,
               "prev_run_s": None, "prev_avg_speed_kmh": None, "v_max_kmh": b["info"]["v_max_kmh"]}
    if i > 0:
        run = a - sol[train_id][i - 1][1]
        seg = world.segments[task.legs[i - 1].segment_id]
        dragged.update(prev_run_s=run, prev_avg_speed_kmh=round(seg.length_m / run * 3.6, 1) if run > 0 else None)
    return {"base_plan_version": plan.version, "kind": kind, "time": tt, "clamped": clamped, "dragged": dragged,
            **out, "conflicts": conflicts,
            "total_delay_delta_s": new.kpi.total_delay_s - base.kpi.total_delay_s,
            "index_forecast": new.index.value, "delta_index": round(new.index.value - base.index.value, 1),
            "compute_ms": round((time.perf_counter() - t0) * 1000), "plan": new, "pin": pin}


def _keep_runs(task: Task, plan: Plan, from_node: int) -> None:
    """Runs of the dragged train after the point keep their planned durations (the same speed)."""
    runs = {e.segment_id: e.end - e.start for e in plan.entries if e.kind == "run" and e.train_id == task.train_id}
    for j in range(max(from_node, 0), len(task.legs)):
        d = runs.get(task.legs[j].segment_id)
        if d is not None:
            task.legs[j].fixed_run = round(d)


def _diff(world: World, base: Plan, new: Plan, train_id: str, station_id: str) -> dict:
    def times(p: Plan) -> dict:
        return {(e.train_id, e.station_id): (e.start, e.end, e.stop) for e in p.entries if e.kind == "dwell"}

    old, cur = times(base), times(new)
    affected, threads = [], []
    by_train: dict[str, list] = {}
    for (tid, st), (a, d, s) in cur.items():
        by_train.setdefault(tid, []).append((st, a, d, s))
    for tid, pts in by_train.items():
        deltas = [(a - old[(tid, st)][0], d - old[(tid, st)][1]) for st, a, d, _ in pts if (tid, st) in old]
        dmax = max((max(abs(x), abs(y)) for x, y in deltas), default=0.0)
        if dmax < 30 and tid != train_id:
            continue
        pts.sort(key=lambda x: x[1])
        last = pts[-1]
        final = last[1] - old[(tid, last[0])][0] if (tid, last[0]) in old else 0.0
        affected.append({"train_id": tid, "delta_final_s": round(final), "delta_max_s": round(dmax)})
        threads.append({"train_id": tid, "category": world.trains[tid].category if tid in world.trains else None,
                        "points": [{"station_id": st, "km": world.stations[st].km, "arr": a, "dep": d, "stop": s}
                                   for st, a, d, s in pts]})
    affected.sort(key=lambda x: (x["train_id"] != train_id, -x["delta_max_s"]))
    return {"threads": threads, "affected": affected}
