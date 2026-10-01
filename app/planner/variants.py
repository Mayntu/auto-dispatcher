"""Variant generation: 3 strategies solved in parallel, scored, explained (CLAUDE.md §12.4)."""

from __future__ import annotations

import asyncio
import uuid

from app.planner.pool import SolverPool
from app.planner.strategies import STRATEGIES, pick_strategies
from app.railcore.explain import explain_plan
from app.railcore.infra import World
from app.railcore.models import Plan, Variant
from app.railcore.problem import Snapshot


async def generate_variants(pool: SolverPool, world: World, snap: Snapshot, settings: dict,
                            base_version: int, forecast: Plan | None) -> list[Variant]:
    strategies = pick_strategies(snap.incidents, settings)
    payload = snap.model_dump(mode="json")
    results = await asyncio.gather(*(pool.solve(payload, s, settings) for s in strategies))
    plans = [Plan.model_validate(r["plan"]) for r in results]

    base_idx = forecast.index.value if forecast else None
    base_delay = forecast.kpi.total_delay_s if forecast else None
    best = min(range(len(plans)), key=lambda i: plans[i].kpi.weighted_delay_s)
    orders = [_order(p) for p in plans]
    variants = []
    for i, (sid, plan) in enumerate(zip(strategies, plans)):
        compare = None
        if i != best:
            compare = (STRATEGIES[strategies[best]].title, plans[best].kpi.total_delay_s)
        elif len(plans) > 1:
            other = min((j for j in range(len(plans)) if j != i), key=lambda j: plans[j].kpi.total_delay_s)
            compare = (STRATEGIES[strategies[other]].title, plans[other].kpi.total_delay_s)
        variants.append(Variant(
            id=uuid.uuid4().hex[:8], incident_ids=[x.id for x in snap.incidents], base_plan_version=base_version,
            strategy=sid, title=STRATEGIES[sid].title, plan=plan,
            delta_index=round(plan.index.value - base_idx, 1) if base_idx is not None else 0.0,
            delta_delay_s=plan.kpi.total_delay_s - base_delay if base_delay is not None else 0.0,
            explanation=explain_plan(world, plan, snap.incidents, sid, base_idx, compare),
            status="proposed",
        ))
        same = next((j for j in range(i) if orders[j] == orders[i]), None)
        if same is not None:
            variants[-1].explanation.insert(0, f"Порядок движения совпадает с вариантом «{STRATEGIES[strategies[same]].title}»: "
                                               "для этой стратегии решатель не нашёл лучшей очерёдности.")
    return variants


def _order(plan: Plan) -> tuple:
    """Who goes first on every segment — two plans with the same order are the same decision."""
    runs = sorted((e.segment_id, e.start, e.train_id) for e in plan.entries if e.kind == "run")
    return tuple((seg, tid) for seg, _, tid in runs)
