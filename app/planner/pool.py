"""Process pool for CPU-heavy solving. Jobs take/return plain dicts (picklable, no CP-SAT objects)."""

from __future__ import annotations

import asyncio
import multiprocessing as mp
from concurrent.futures import ProcessPoolExecutor
from functools import lru_cache

from app.common.config import load_settings
from app.planner.model import solve_cpsat
from app.planner.strategies import STRATEGIES, durations_for
from app.railcore.evaluate import Deadlock, evaluate
from app.railcore.infra import get_world
from app.railcore.models import IncidentType, Plan
from app.railcore.problem import Snapshot, assemble_plan, build_tasks
from app.railcore.running_time import RunningTimes


@lru_cache
def _rts() -> RunningTimes:
    return RunningTimes(get_world())


def _warmup() -> None:
    _rts()
    load_settings()


def solve_job(snapshot: dict, strategy_id: str, settings: dict) -> dict:
    """Solve one strategy: CP-SAT (fallback: fixed order), then robustness at max durations."""
    world, rts = get_world(), _rts()
    snap = Snapshot.model_validate(snapshot)
    strat = STRATEGIES[strategy_id]
    durations = durations_for(strat, snap.incidents, settings)
    tasks = build_tasks(snap, world, rts, settings, durations, strat.weight_mult)
    sol, status, ms = solve_cpsat(tasks, settings, snap.hint, snap.now, strat.lambda_stop_mult)
    solver = "cpsat"
    if sol is None:
        solver = "fallback"
        sol = evaluate(tasks, snap.hint, settings["planner"]["segment_clear_s"])
    plan = assemble_plan(tasks, sol, snap, world, settings, solver=solver, strategy=strategy_id, solve_ms=ms)

    # robustness: keep this plan's order, incidents at their max duration
    # (a rescue locomotive caps a failure at its ETA, so that stays as planned)
    robust_dur = {i.id: i.est_max_s for i in snap.incidents}
    if strat.duration == "rescue":
        robust_dur.update({i.id: durations[i.id] for i in snap.incidents if i.type == IncidentType.TRAIN_FAILURE})
    try:
        rtasks = build_tasks(snap, world, rts, settings, robust_dur, strat.weight_mult)
        rsol = evaluate(rtasks, plan.entries, settings["planner"]["segment_clear_s"])
        rplan = assemble_plan(rtasks, rsol, snap, world, settings, solver="refresh", strategy=strategy_id, solve_ms=0)
        plan.kpi.robust_total_delay_s = rplan.kpi.total_delay_s
    except Deadlock:
        plan.kpi.robust_total_delay_s = None
    return {"plan": plan.model_dump(mode="json"), "status": status}


def forecast_plan(snap: Snapshot, settings: dict) -> Plan | None:
    """Current plan's order replayed from the current state with active incidents (expected durations).
    Cheap (no CP-SAT): runs in the event loop once a second."""
    world, rts = get_world(), _rts()
    tasks = build_tasks(snap, world, rts, settings)
    try:
        sol = evaluate(tasks, snap.hint, settings["planner"]["segment_clear_s"])
    except Deadlock:
        return None
    return assemble_plan(tasks, sol, snap, world, settings, solver="refresh", strategy=None, solve_ms=0)


class SolverPool:
    def __init__(self, workers: int):
        self._pool = ProcessPoolExecutor(max_workers=workers, mp_context=mp.get_context("spawn"), initializer=_warmup)

    async def solve(self, snapshot: dict, strategy_id: str, settings: dict) -> dict:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._pool, solve_job, snapshot, strategy_id, settings)

    async def warm(self, n: int) -> None:
        loop = asyncio.get_running_loop()
        await asyncio.gather(*(loop.run_in_executor(self._pool, _warmup) for _ in range(n)))

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
