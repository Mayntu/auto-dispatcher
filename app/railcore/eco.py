"""Eco-driving profile for one segment (CLAUDE.md §13).

MVP dynamics (no Davis in the motion equations): constant acceleration by category,
service braking b_service, coasting at -0.03 m/s^2. Energy uses Davis resistance + grade.
Optimal regime structure: accelerate -> cruise -> coast -> brake; search the cruise speed and
the coasting point so the run takes exactly the planned target time.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from app.railcore.models import Direction, ProfilePoint, Segment, TrainCategory
from app.railcore.running_time import resistance_n

DS = 50.0
COAST_DEC = 0.03
V_FLOOR = 1.0  # m/s, coasting never fully stops the train mid-segment
ACCEL = {"express": 0.4, "passenger": 0.4, "freight": 0.15}


@dataclass
class Run:
    time_s: float
    energy_kwh: float
    v: list[float]  # m/s at grid points
    regime: list[str]  # per step


def _grade_at(seg: Segment, s_m: float, direction: Direction) -> float:
    x = s_m if direction == Direction.ODD else seg.length_m - s_m
    for z in seg.grade_zones:
        if z.from_m <= x < z.to_m:
            return z.permille if direction == Direction.ODD else -z.permille
    return 0.0


def simulate(seg: Segment, cat: TrainCategory, direction: Direction, v_lim: float, v_in: float, v_out: float,
             v_cruise: float, s_coast: float) -> Run:
    n = int(math.ceil(seg.length_m / DS))
    a_acc = ACCEL[cat.id]
    target = min(v_lim, v_cruise)
    vf = [v_in]
    reg_f = []
    for k in range(n):
        v = vf[-1]
        if k * DS < s_coast:
            if v < target - 1e-6:
                v2, r = min(target, math.sqrt(v * v + 2 * a_acc * DS)), "accel"
            elif v > target + 1e-6:
                v2, r = max(target, math.sqrt(max(v * v - 2 * COAST_DEC * DS, 0))), "coast"
            else:
                v2, r = target, "cruise"
        else:
            v2, r = max(V_FLOOR, math.sqrt(max(v * v - 2 * COAST_DEC * DS, 0))), "coast"
        vf.append(v2)
        reg_f.append(r)
    vb = [0.0] * (n + 1)
    vb[n] = v_out
    for k in range(n - 1, -1, -1):
        vb[k] = min(v_lim, math.sqrt(vb[k + 1] ** 2 + 2 * cat.b_service * DS))

    m_eff = cat.mass_t * 1000 * (1 + cat.rotating_mass)
    v = [min(a, b) for a, b in zip(vf, vb)]
    regime, t, e = [], 0.0, 0.0
    for k in range(n):
        v1, v2 = v[k], v[k + 1]
        r = "brake" if vb[k + 1] < vf[k + 1] - 1e-6 else reg_f[k]
        regime.append(r)
        t += 2 * DS / max(v1 + v2, 0.2)
        vm_kmh = (v1 + v2) / 2 * 3.6
        res = resistance_n(cat, vm_kmh, _grade_at(seg, k * DS, direction))
        if r == "accel":
            e += (m_eff * a_acc + max(res, 0)) * DS
        elif r == "cruise":
            e += max(res, 0) * DS
    return Run(t, e / 3.6e6, v, regime)


def points(run: Run, every_m: float = 100.0) -> list[ProfilePoint]:
    step = max(1, int(every_m / DS))
    out = []
    for k in range(0, len(run.v), step):
        out.append(ProfilePoint(s_m=k * DS, v_kmh=round(run.v[k] * 3.6, 1),
                                regime=run.regime[min(k, len(run.regime) - 1)]))
    return out


def eco_profile(seg: Segment, cat: TrainCategory, direction: Direction, v_lim_kmh: float,
                stop_start: bool, stop_end: bool, t_target: float) -> tuple[Run, Run]:
    """Returns (eco run, minimum-time run)."""
    v_lim = v_lim_kmh / 3.6
    v_in = 0.0 if stop_start else v_lim
    v_out = 0.0 if stop_end else v_lim
    fastest = simulate(seg, cat, direction, v_lim, v_in, v_out, v_lim, seg.length_m)
    if t_target <= fastest.time_s + 1:
        return fastest, fastest

    best = fastest
    vc = v_lim_kmh
    while vc >= 30:
        v_c = vc / 3.6
        full = simulate(seg, cat, direction, v_lim, v_in, v_out, v_c, seg.length_m)
        if full.time_s > t_target:
            break
        lo, hi = 0.0, seg.length_m
        cand = full
        for _ in range(12):
            mid = (lo + hi) / 2
            r = simulate(seg, cat, direction, v_lim, v_in, v_out, v_c, mid)
            if r.time_s > t_target:
                lo = mid
            else:
                hi, cand = mid, r
        if cand.energy_kwh < best.energy_kwh:
            best = cand
        vc -= 5
    return best, fastest


def train_profile(world, rts, plan, state, train, plan_version: int = 0):
    """Connected DAS: profile for the train's current/next segment, timed to the approved plan (§13)."""
    from app.railcore.models import SpeedProfile, TrainStatus

    if state.status == TrainStatus.FINISHED:
        return None
    route = world.route(train)
    last = len(route) - 1
    if state.segment_id:
        k = route.index(state.next_station_id) - 1
    elif state.station_id:
        k = route.index(state.station_id)
    else:
        k = 0
    if k >= last:
        return None
    seg = world.segment_between(route[k], route[k + 1])
    cat = world.categories[train.category]
    entries = {(e.kind, e.train_id, e.segment_id or e.station_id): e for e in plan.entries} if plan else {}
    run = entries.get(("run", train.id, seg.id))
    d_a = entries.get(("dwell", train.id, route[k]))
    d_b = entries.get(("dwell", train.id, route[k + 1]))
    s_a, s_b = train.stops[k], train.stops[k + 1]
    dep = run.start if run else s_a.dep
    arr = run.end if run else s_b.arr
    stop_start = d_a.stop if d_a else (k == 0 or s_a.stop)
    stop_end = d_b.stop if d_b else s_b.stop
    latest = arr
    if k + 1 < last and d_b is not None:
        min_dwell = max(s_b.min_dwell_s, cat.min_dwell_s) if s_b.stop else 0
        latest = max(arr, d_b.end - min_dwell)
        if cat.id != "freight" and s_b.stop and s_b.arr is not None:
            latest = min(latest, max(arr, s_b.arr))
    t_target = latest - dep
    v_lim = rts.get(cat.id, seg.id, train.direction, train.v_max_override_kmh).v_kmh
    eco, fast = eco_profile(seg, cat, train.direction, v_lim, stop_start, stop_end, t_target)
    a, b = world.segment_km(seg.id)
    saving = (fast.energy_kwh - eco.energy_kwh) / fast.energy_kwh * 100 if fast.energy_kwh > 0 else 0.0
    return SpeedProfile(
        train_id=train.id, plan_version=plan_version, segment_id=seg.id, t_target_s=round(t_target),
        t_profile_s=round(eco.time_s), energy_kwh=round(eco.energy_kwh, 1),
        energy_min_time_kwh=round(fast.energy_kwh, 1), saving_pct=round(saving, 1), points=points(eco),
        min_points=points(fast), t_min_s=round(fast.time_s), v_lim_kmh=round(v_lim, 1),
        direction=train.direction, km_from=a, km_to=b,
    )
