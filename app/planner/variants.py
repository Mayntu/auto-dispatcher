"""Variant generation: strategies solved in parallel, scored on one yardstick, explained (CLAUDE.md §12.4, §27.5)."""

from __future__ import annotations

import asyncio
import uuid

from app.planner.pool import SolverPool, retime_with_objective
from app.planner.strategies import STRATEGIES
from app.railcore.explain import explain_plan
from app.railcore.infra import World
from app.railcore.models import Plan, Variant
from app.railcore.problem import Snapshot

RETURN_TITLE = "Возврат к графику"


async def generate_variants(pool: SolverPool, world: World, snap: Snapshot, settings: dict, base_version: int,
                            forecast: Plan | None, strategies: list[str], kind: str = "incident") -> list[Variant]:
    payload = snap.model_dump(mode="json")
    results = await asyncio.gather(*(pool.solve(payload, s, settings) for s in strategies))
    variants = [
        Variant(
            id=uuid.uuid4().hex[:8], incident_ids=[x.id for x in snap.incidents], base_plan_version=base_version,
            strategy=sid, title=RETURN_TITLE if kind == "return" else STRATEGIES[sid].title,
            plan=Plan.model_validate(r["plan"]), delta_index=0.0, delta_delay_s=0.0, explanation=[],
            status="proposed", kind=kind, updated_at=snap.now,
        )
        for sid, r in zip(strategies, results)
    ]
    score_variants(world, snap, settings, variants, forecast)
    return variants


def score_variants(world: World, snap: Snapshot, settings: dict, variants: list[Variant], current: Plan | None) -> None:
    """Deltas against the current forecast, the common score (balanced objective with expected durations,
    weighted minutes) and the recommendation, plus the explanation texts — so that every number on a card
    and the badge come from the same state. Called on generation and on every live re-timing."""
    live = [v for v in variants if v.status == "proposed"]
    for v in live:
        scored = retime_with_objective(snap.model_copy(update={"hint": v.plan.entries}), settings, "balanced")
        v.score = round(scored[1] / 60, 1) if scored else None
    ranked = sorted((v for v in live if v.score is not None), key=lambda v: v.score)
    for v in variants:
        v.recommended = bool(ranked) and v is ranked[0]

    base_idx = current.index.value if current else None
    base_delay = current.kpi.total_delay_s if current else None
    best = ranked[0] if ranked else None
    orders = {v.id: _order(v.plan) for v in live}
    for i, v in enumerate(live):
        v.delta_index = round(v.plan.index.value - base_idx, 1) if base_idx is not None else 0.0
        v.delta_delay_s = v.plan.kpi.total_delay_s - base_delay if base_delay is not None else 0.0
        others = [o for o in live if o is not v]
        compare = None
        if best is not None and v is not best:
            compare = (best.title, best.plan.kpi.total_delay_s)
        elif others:
            o = min(others, key=lambda x: x.plan.kpi.total_delay_s)
            compare = (o.title, o.plan.kpi.total_delay_s)
        v.explanation = explain_plan(world, v.plan, snap.incidents, v.strategy, base_idx, compare)
        same = next((o for o in live[:i] if orders[o.id] == orders[v.id]), None)
        if same is not None:
            v.explanation.insert(0, f"Порядок движения совпадает с вариантом «{same.title}»: "
                                    "для этой стратегии решатель не нашёл лучшей очерёдности.")


def _order(plan: Plan) -> tuple:
    """Who goes first on every segment — two plans with the same order are the same decision."""
    runs = sorted((e.segment_id, e.start, e.train_id) for e in plan.entries if e.kind == "run")
    return tuple((seg, tid) for seg, _, tid in runs)
