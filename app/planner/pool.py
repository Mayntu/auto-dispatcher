"""Process pool for CPU-heavy solving. Jobs take/return plain dicts (picklable, no CP-SAT objects)."""

from __future__ import annotations

import asyncio
import multiprocessing as mp
import logging
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from functools import lru_cache

from app.common.config import load_settings
from app.planner.model import solve_cpsat
from app.planner.strategies import STRATEGIES, durations_for
from app.railcore.evaluate import Deadlock, evaluate
from app.railcore.infra import get_world
from app.railcore.models import IncidentType, Plan
from app.railcore.problem import Snapshot, assemble_plan, build_tasks
from app.railcore.running_time import RunningTimes

log = logging.getLogger("planner.pool")


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
        sol = _fallback(tasks, snap, settings, strat)
    plan = assemble_plan(tasks, sol, snap, world, settings, solver=solver, strategy=strategy_id, solve_ms=ms)

    # robustness: keep this plan's order, incidents at their max duration
    # (a rescue locomotive caps a failure at its ETA, so that stays as planned)
    robust_dur = {i.id: i.est_max_s for i in snap.incidents}
    if strat.duration == "rescue":
        robust_dur.update({i.id: durations[i.id] for i in snap.incidents if i.type == IncidentType.TRAIN_FAILURE})
    try:
        rtasks = build_tasks(snap, world, rts, settings, robust_dur, strat.weight_mult)
        rsol = evaluate(rtasks, plan.entries, settings["planner"]["segment_clear_s"], snap.now)
        rplan = assemble_plan(rtasks, rsol, snap, world, settings, solver="refresh", strategy=strategy_id, solve_ms=0)
        plan.kpi.robust_total_delay_s = rplan.kpi.total_delay_s
    except Deadlock:
        plan.kpi.robust_total_delay_s = None
    return {"plan": plan.model_dump(mode="json"), "status": status}


def _fallback(tasks, snap: Snapshot, settings: dict, strat):
    """CP-SAT found nothing in time: keep the current order; if that order no longer fits, the timetable
    order; as a last resort give CP-SAT a longer budget. Never leaves the dispatcher without a plan."""
    clear = settings["planner"]["segment_clear_s"]
    for order in (snap.hint, []):
        try:
            return evaluate(tasks, order, clear, snap.now)
        except Deadlock:
            continue
    sol, status, _ = solve_cpsat(tasks, settings, [], snap.now, strat.lambda_stop_mult, time_limit_s=10)
    if sol is None:
        raise RuntimeError(f"no plan found ({status})")
    return sol


def forecast_plan(snap: Snapshot, settings: dict, durations: dict[str, int] | None = None) -> Plan | None:
    """The order of `snap.hint` re-timed from the current state with active incidents (expected durations
    unless given). Cheap (no CP-SAT): used for the 1 Hz forecast, refresh and re-timing on apply."""
    world, rts = get_world(), _rts()
    tasks = build_tasks(snap, world, rts, settings, durations)
    try:
        sol = evaluate(tasks, snap.hint, settings["planner"]["segment_clear_s"], snap.now)
    except Deadlock:
        return None
    return assemble_plan(tasks, sol, snap, world, settings, solver="refresh", strategy=None, solve_ms=0)


class SolverPool:
    """CP-SAT runs in worker processes. A native crash in a worker breaks the whole executor, so the pool
    is rebuilt and the job retried once instead of leaving the planner without a solver until restart."""

    def __init__(self, workers: int):
        self.workers = workers
        self._pool = self._new()

    def _new(self) -> ProcessPoolExecutor:
        return ProcessPoolExecutor(max_workers=self.workers, mp_context=mp.get_context("spawn"), initializer=_warmup)

    async def solve(self, snapshot: dict, strategy_id: str, settings: dict) -> dict:
        loop = asyncio.get_running_loop()
        for attempt in (1, 2):
            pool = self._pool
            try:
                return await loop.run_in_executor(pool, solve_job, snapshot, strategy_id, settings)
            except BrokenProcessPool:
                log.error("solver process crashed (attempt %d), rebuilding the pool", attempt)
                if self._pool is pool:
                    pool.shutdown(wait=False, cancel_futures=True)
                    self._pool = self._new()
        raise RuntimeError("solver pool crashed twice")

    async def warm(self, n: int) -> None:
        loop = asyncio.get_running_loop()
        await asyncio.gather(*(loop.run_in_executor(self._pool, _warmup) for _ in range(n)))

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
