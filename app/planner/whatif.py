"""What-if sandbox (CLAUDE.md §12.8). MVP modifications: train_speed, incident_duration."""

from __future__ import annotations

import asyncio

from app.planner.pool import SolverPool
from app.railcore.explain import explain_plan, signed_min, train_label
from app.railcore.infra import World
from app.railcore.models import Modification, Plan, WhatIfRequest, WhatIfResult
from app.railcore.problem import Snapshot

SUPPORTED = {"train_speed", "incident_duration"}


def apply_modifications(snap: Snapshot, mods: list[Modification]) -> Snapshot:
    trains = {t.id: t for t in snap.trains}
    overrides = dict(snap.duration_override_s)
    for mod in mods:
        if mod.kind not in SUPPORTED:
            raise ValueError(f"modification '{mod.kind}' is not supported in MVP")
        if mod.kind == "train_speed":
            trains[mod.target_id] = trains[mod.target_id].model_copy(update={"v_max_override_kmh": mod.value})
        elif mod.kind == "incident_duration":
            inc = next((i for i in snap.incidents if i.id == mod.target_id), None) or (snap.incidents or [None])[0]
            if inc is not None:
                overrides[inc.id] = round(mod.value * 60)  # minutes at the API boundary
    return snap.model_copy(update={"trains": [trains[t.id] for t in snap.trains], "duration_override_s": overrides})


def _final_arrivals(plan: Plan) -> dict[str, float]:
    out: dict[str, float] = {}
    for e in plan.entries:
        if e.kind == "dwell":
            out[e.train_id] = max(out.get(e.train_id, 0.0), e.start)
    return out


async def run_whatif(pool: SolverPool, world: World, snap: Snapshot, req: WhatIfRequest, settings: dict) -> WhatIfResult:
    """The baseline is the same re-optimisation without the modifications (solved in parallel), so the
    difference shows the effect of the changed parameter only, not the gain from re-planning itself."""
    mod_snap = apply_modifications(snap, req.modifications)
    p = settings["planner"]
    settings = {**settings, "planner": {**p, "time_limit_s": p.get("whatif_time_limit_s", p["time_limit_s"])}}
    base_res, res = await asyncio.gather(pool.solve(snap.model_dump(mode="json"), "balanced", settings),
                                         pool.solve(mod_snap.model_dump(mode="json"), "balanced", settings))
    base, plan = Plan.model_validate(base_res["plan"]), Plan.model_validate(res["plan"])

    old_arr, new_arr = _final_arrivals(base), _final_arrivals(plan)
    sched = {t.id: t.stops[-1].arr for t in snap.trains}
    per_train = [
        {"train_id": tid, "arr_delta_s": round(new_arr[tid] - old_arr[tid]),
         "final_delay_s": round(max(0.0, new_arr[tid] - (sched[tid] or new_arr[tid])))}
        for tid in new_arr if tid in old_arr
    ]
    per_train.sort(key=lambda x: -abs(x["arr_delta_s"]))
    old_keys = {(m.station_id, m.waiting_train, m.passing_train) for m in base.meetings}
    changed = [m for m in plan.meetings if (m.station_id, m.waiting_train, m.passing_train) not in old_keys]

    explanation = explain_plan(world, plan, mod_snap.incidents, "balanced", base.index.value)
    moved = [p for p in per_train if abs(p["arr_delta_s"]) >= 60]
    if moved:
        top = ", ".join(f"{train_label(world, p['train_id'])} {signed_min(p['arr_delta_s'])}" for p in moved[:3])
        explanation.insert(0, f"Изменение прибытия на конечную: {top}.")
    else:
        explanation.insert(0, "Прибытие поездов на конечные практически не меняется.")
    return WhatIfResult(
        request_id=req.id, plan=plan, delta_index=round(plan.index.value - base.index.value, 1),
        per_train=per_train, changed_meetings=changed, explanation=explanation[:4],
    )
