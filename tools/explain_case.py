"""Human-readable walk-through of one dispatching decision — to check the algorithm by eye.

    python -m tools.explain_case --at 08:40 --incident cow
    python -m tools.explain_case --at 09:05 --incident fail --train 2003
    python -m tools.explain_case --at 09:10 --incident close

Runs the day up to --at, creates the incident and prints: where every train is, then each possible
action (do nothing / FIFO / the three planner variants) with every train's arrival delay, who waits for
whom at which station, the totals, an independent rule check — and which action is best and why.
"""

from __future__ import annotations

import argparse

from app.common.config import load_settings
from app.field.sim import FieldSim
from app.planner.pool import forecast_plan, solve_job
from app.planner.snapshot import build_snapshot
from app.planner.strategies import STRATEGIES, pick_strategies
from app.railcore.infra import get_world
from app.railcore.models import Plan
from app.railcore.running_time import RunningTimes
from tools.audit import check_plan

CAT = {"express": "скорый", "passenger": "пасс.", "freight": "груз."}
INCIDENTS = {
    "cow": {"type": "obstacle", "segment_id": "R1-STP", "km": 24.5, "est_min_min": 15, "est_max_min": 30},
    "close": {"type": "segment_closed", "segment_id": "R2-OZR", "est_min_min": 30, "est_max_min": 30},
    "fail": {"type": "train_failure", "est_min_min": 20, "est_max_min": 45},
}


def hhmm(t: float, epoch_min: int = 7 * 60 + 55) -> str:
    m = int(round(t / 60)) + epoch_min
    return f"{m // 60 % 24:02d}:{m % 60:02d}"


def to_sim(s: str) -> float:
    h, m = map(int, s.split(":"))
    return (h * 60 + m - (7 * 60 + 55)) * 60.0


def arrivals(plan: Plan) -> dict[str, float]:
    out: dict[str, float] = {}
    for e in plan.entries:
        if e.kind == "dwell":
            out[e.train_id] = max(out.get(e.train_id, 0.0), e.start)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--at", default="08:40", help="time of the incident, HH:MM")
    ap.add_argument("--incident", choices=INCIDENTS, default="cow")
    ap.add_argument("--train", default="2003", help="train for --incident fail")
    args = ap.parse_args()

    world, settings = get_world(), load_settings()
    rts = RunningTimes(world)
    sim = FieldSim(world, rts, settings)
    names = {s.id: s.name for s in world.infra.stations}

    def snap(plan):
        return build_snapshot(sim.snapshot(), world.timetable, [fi.incident for fi in sim.active()], plan)

    plan = Plan.model_validate(solve_job(snap(None).model_dump(mode="json"), "balanced", settings)["plan"])
    sim.set_plan(plan)
    sim.step(to_sim(args.at))
    req = dict(INCIDENTS[args.incident])
    if args.incident == "fail":
        req["train_id"] = args.train
    inc = sim.create_incident(req)
    sn = snap(plan)

    print(f"\n=== {hhmm(sim.now)} · {inc.description}")
    where = inc.segment_id and f"перегон {names[world.segments[inc.segment_id].from_station]} — {names[world.segments[inc.segment_id].to_station]}"
    print(f"    где: {where or ''}{f', км {inc.km:.1f}' if inc.km else ''}; оценка длительности {inc.est_min_s // 60}–{inc.est_max_s // 60} мин "
          f"(ожидаемо {inc.est_expected_s // 60})\n")

    print("Поезда сейчас:")
    sched = {t.id: t for t in world.timetable}
    for st in sn.states.values():
        t = sched[st.train_id]
        if st.status == "finished":
            continue
        route = f"{names[t.stops[0].station_id]} → {names[t.stops[-1].station_id]}"
        if not st.on_field:
            loc = f"ещё не вышел (отправление {hhmm(t.stops[0].dep)})"
        elif st.segment_id:
            seg = world.segments[st.segment_id]
            loc = f"на перегоне {names[seg.from_station]}—{names[seg.to_station]}, {st.speed_kmh:.0f} км/ч"
        else:
            loc = f"на {names[st.station_id]}, путь {st.track_id}"
        print(f"  {st.train_id:>5} {CAT[t.category]:7} {route:28} {loc}; прибытие по расписанию {hhmm(t.stops[-1].arr)}")

    options: list[tuple[str, Plan]] = []
    keep = forecast_plan(sn, settings)
    if keep:
        options.append(("Ничего не делать", keep))
    fifo = forecast_plan(sn.model_copy(update={"hint": []}), settings)
    if fifo:
        options.append(("По очереди расписания (FIFO)", fifo))
    for sid in pick_strategies(sn.incidents, settings):
        res = solve_job(sn.model_dump(mode="json"), sid, settings)
        options.append((f"Система: {STRATEGIES[sid].title} [{res['status']}]", Plan.model_validate(res["plan"])))

    trains = sorted({tid for _, p in options for tid in arrivals(p)}, key=lambda x: (sched[x].stops[0].dep or 0))
    print("\nОпоздание к конечной станции, мин (0 — вовремя):")
    width = 12
    print("  " + " " * 30 + "".join(f"{tid:>{width}}" for tid in trains))
    for name, p in options:
        arr = arrivals(p)
        cells = []
        for tid in trains:
            d = max(0.0, arr.get(tid, 0) - (sched[tid].stops[-1].arr or 0)) / 60
            cells.append(f"{d:>{width}.0f}" if tid in arr else f"{'—':>{width}}")
        print(f"  {name[:30]:30}" + "".join(cells))

    print("\nИтоги вариантов:")
    print(f"  {'вариант':48} {'сумма':>7} {'взвеш.':>7} {'лишних ост.':>11} {'индекс':>7} {'нарушений':>9}")
    best = None
    for name, p in options:
        viol = check_plan(p, sn, world, rts, settings)
        k = p.kpi
        print(f"  {name[:48]:48} {k.total_delay_s / 60:6.0f}м {k.weighted_delay_s / 60:6.0f}м {k.unplanned_stops:>11} "
              f"{p.index.value:>7.1f} {len(viol):>9}")
        for v in viol[:3]:
            print(f"      ! {v}")
        if not viol and (best is None or k.weighted_delay_s < best[1].kpi.weighted_delay_s):
            best = (name, p)

    print("\nКто кого ждёт (скрещения и обгоны):")
    for name, p in options:
        ms = sorted(p.meetings, key=lambda m: m.time)
        txt = "; ".join(f"{m.waiting_train} ждёт {m.passing_train} на {names[m.station_id]} {m.wait_s / 60:.0f} мин "
                        f"(до {hhmm(m.time)})" for m in ms if m.time >= sim.now) or "—"
        print(f"  {name[:40]:40} {txt}")

    if best:
        k0 = keep.kpi.weighted_delay_s if keep else None
        print(f"\nЛучший по взвешенной задержке (скорый ×5, пасс. ×3, груз. ×1): {best[0]}")
        if k0:
            print(f"  против «ничего не делать»: {k0 / 60:.0f} → {best[1].kpi.weighted_delay_s / 60:.0f} мин "
                  f"({(k0 - best[1].kpi.weighted_delay_s) / max(k0, 1) * 100:.0f}% меньше)")
    print()


if __name__ == "__main__":
    main()
