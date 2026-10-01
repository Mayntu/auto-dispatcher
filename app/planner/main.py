"""Planner service: plan, variants and their lifecycle, decision journal, what-if, index stream.

Variant lifecycle (§27.5): proposed -> applied / rejected / stale. Cards stay while their incidents are
active; once all incidents are cleared and nothing awaits a decision they go to the journal and the panel
shows "no active decisions". After an incident clears the plan is quietly re-timed; a "return to schedule"
variant is offered only when re-ordering gains >= `return_gain_s` weighted seconds (§12.1).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid

from app.bus.base import EventBus
from app.bus.envelope import Envelope
from app.planner.pool import SolverPool, forecast_plan, retime_with_objective
from app.planner.snapshot import build_snapshot
from app.planner.strategies import STRATEGIES, durations_for, pick_strategies
from app.planner.variants import generate_variants, score_variants
from app.planner.whatif import run_whatif
from app.railcore.explain import mins
from app.railcore.infra import World
from app.railcore.models import Incident, JournalEntry, Plan, Variant, WhatIfRequest
from app.railcore.problem import Snapshot

log = logging.getLogger("planner")
GUARD_BROKEN_S = 600  # sim seconds a plan may stay unexecutable without a decision before the guard re-plans
STALE_MARGIN_S = 300  # objective units (weighted s): a variant this much worse than keeping the current plan is stale
JOURNAL_MAX = 500


class PlannerService:
    def __init__(self, bus: EventBus, world: World, settings: dict, pool: SolverPool):
        self.bus = bus
        self.world = world
        self.settings = settings
        self.pool = pool
        self.plan: Plan | None = None
        self.version = 0
        self.variants: dict[str, Variant] = {}
        self.incidents: dict[str, Incident] = {}
        self.field: dict | None = None
        self.forecast: Plan | None = None
        self.journal: list[JournalEntry] = []
        self.last_solve_ms = 0
        self._first_state = asyncio.Event()
        self._reasons: set[str] = set()
        self._gen_task: asyncio.Task | None = None
        self._tasks: list[asyncio.Task] = []
        self._last_refresh = 0.0
        self._forecast_fails = 0
        self._hold = False
        self._last_gain = 0.0
        self.guard_triggers = 0  # deadlock_guard_triggered_total: guards must not hide planning bugs
        self._guard_pending: str | None = None
        self._broken_since: float | None = None

    async def start(self) -> None:
        await self.bus.subscribe("field.state", self._on_field)
        await self.bus.subscribe("incident.*", self._on_incident)
        await self.bus.subscribe("cmd.planner.*", self._on_cmd)
        await self.bus.subscribe("field.guard", self._on_guard)
        await self.bus.subscribe("field.train_event", self._on_train_event)
        await asyncio.wait_for(self._first_state.wait(), timeout=10)
        t0 = time.perf_counter()
        res = await self.pool.solve(self.snapshot().model_dump(mode="json"), "balanced", self.settings)
        plan = Plan.model_validate(res["plan"])
        self.last_solve_ms = round((time.perf_counter() - t0) * 1000)
        await self._approve(plan, None)
        log.info("plan v1 built in %d ms (%s), index %.1f", self.last_solve_ms, res["status"], plan.index.value)
        self._tasks.append(asyncio.create_task(self._index_loop()))

    async def stop(self) -> None:
        for t in self._tasks + ([self._gen_task] if self._gen_task else []):
            t.cancel()

    def snapshot(self) -> Snapshot:
        return build_snapshot(self.field, self.world.timetable, list(self.incidents.values()), self.plan)

    @property
    def now(self) -> float:
        return self.field["sim_time"] if self.field else 0.0

    def proposed(self) -> list[Variant]:
        return [v for v in self.variants.values() if v.status == "proposed"]

    # ---- journal ----------------------------------------------------------------------------
    async def _journal(self, kind: str, text: str, **extra) -> None:
        entry = JournalEntry(id=uuid.uuid4().hex[:8], time=self.now, kind=kind, text=text, **extra)
        self.journal.append(entry)
        del self.journal[:-JOURNAL_MAX]
        await self.bus.publish("journal.entry", entry, source="planner", sim_time=self.now)

    # ---- events -----------------------------------------------------------------------------
    async def _on_field(self, env: Envelope) -> None:
        self.field = env.payload
        self._first_state.set()

    async def _on_incident(self, env: Envelope) -> None:
        inc = Incident.model_validate(env.payload)
        if inc.status == "active":
            self.incidents[inc.id] = inc
            await self._journal("incident_created", f"Сбой: {inc.description}", incident_ids=[inc.id])
            self._request("incident")
        else:
            self.incidents.pop(inc.id, None)
            await self._journal("incident_resolved", f"Сбой снят: {inc.description}", incident_ids=[inc.id])
            self._request("incident" if self.incidents else "resolved")

    async def _on_train_event(self, env: Envelope) -> None:
        e = env.payload
        if e.get("kind") == "held_at_signal":
            await self._journal("signal_stop", f"{e['train_id']}: стоянка у проходного {e['signal_id']} — {e['reason']}")

    async def _on_guard(self, env: Envelope) -> None:
        g = env.payload
        self.guard_triggers += 1
        self._guard_pending = (f"защита от блокировки пропустила {g['train_id']} с {self.world.stations[g['station_id']].name} "
                               f"вне плана ({'поле стояло' if g['reason'] == 'stalled' else 'поезд ждал'} "
                               f"{mins(max(g['stalled_s'], g['held_s']))} мин)")

    async def _guard_replan(self, why: str) -> None:
        """The approved order has already been broken (a guard let a train go, or the plan has not been
        executable for a while with no decision): rebuild the plan from the current state automatically.
        Journaled and counted — this is a safety net, not a way to plan."""
        snap = self.snapshot()
        res = await self.pool.solve(snap.model_dump(mode="json"), "balanced", self.settings)
        plan = Plan.model_validate(res["plan"])
        plan.strategy = "guard"
        await self._approve(plan, self.version)
        for v in self.proposed():
            v.status = "stale"
        await self._journal("guard_replan", f"План перестроен автоматически: {why}. Новый план v{self.version}.")
        await self._publish_variants()
        await self._sync_hold()
        log.warning("guard re-plan #%d: %s", self.guard_triggers, why)

    def _request(self, reason: str) -> None:
        self._reasons.add(reason)
        if self._gen_task is None or self._gen_task.done():
            self._gen_task = asyncio.create_task(self._generate_loop())

    async def _generate_loop(self) -> None:
        """Debounce: events arriving within `debounce_ms` (or during a solve) are planned together."""
        while self._reasons:
            await asyncio.sleep(self.settings["planner"]["debounce_ms"] / 1000)
            reasons, self._reasons = self._reasons, set()
            try:
                await self._generate(reasons)
            except Exception:
                log.exception("variant generation failed")

    async def _generate(self, reasons: set[str]) -> None:
        if self.plan is None:
            return
        t0 = time.perf_counter()
        snap = self.snapshot()
        forecast = forecast_plan(snap, self.settings) or self.forecast
        if snap.incidents or reasons & {"incident", "replan", "broken"}:
            kind = "incident" if snap.incidents else ("broken" if "broken" in reasons else "replan")
            variants = await generate_variants(self.pool, self.world, snap, self.settings, self.version, forecast,
                                               pick_strategies(snap.incidents, self.settings), kind)
        else:
            variants = await self._after_resolution(snap, forecast)
        self.last_solve_ms = round((time.perf_counter() - t0) * 1000)
        for v in self.variants.values():
            if v.status == "proposed":
                v.status = "stale"
        self.variants = {v.id: v for v in variants}
        if variants:
            kind = variants[0].kind
            if kind == "return":
                await self._journal("return_offered", f"Предложен «{variants[0].title}»: перестановка сокращает "
                                    f"взвешенное опоздание на {mins(self._last_gain)} мин")
            else:
                await self._journal("variants_proposed", f"Предложено вариантов: {len(variants)} "
                                    f"(посчитано за {self.last_solve_ms / 1000:.1f} с)",
                                    incident_ids=[i.id for i in snap.incidents])
            await self.bus.publish("planner.metrics", {"solve_ms": self.last_solve_ms, "kind": kind,
                                                       "strategies": [v.strategy for v in variants]},
                                   source="planner", sim_time=snap.now)
        await self._publish_variants()
        await self._sync_hold()
        log.info("%d variants in %d ms (%s)", len(variants), self.last_solve_ms, ",".join(sorted(reasons)))

    async def _after_resolution(self, snap: Snapshot, forecast: Plan | None) -> list[Variant]:
        """All incidents cleared: re-time quietly; offer "return to schedule" only if re-ordering really pays."""
        if forecast is not None:
            await self._refresh(forecast, "incidents cleared")
        cand = await generate_variants(self.pool, self.world, snap, self.settings, self.version, forecast,
                                       ["balanced"], "return")
        cur = retime_with_objective(snap, self.settings, "balanced")
        new = retime_with_objective(snap.model_copy(update={"hint": cand[0].plan.entries}), self.settings, "balanced")
        self._last_gain = (cur[1] - new[1]) if cur and new else 0.0
        if self._last_gain >= self.settings["planner"]["return_gain_s"]:
            return cand
        await self._journal("no_decision_needed", "Сбои сняты, план уточнён по времени — решений не требуется"
                            f" (перестановка дала бы {mins(max(0.0, self._last_gain))} мин)")
        return []

    async def _publish_variants(self) -> None:
        await self.bus.publish("planner.variants", {
            "incident_ids": sorted(self.incidents), "base_plan_version": self.version,
            "solve_ms": self.last_solve_ms, "variants": [v.model_dump(mode="json") for v in self.variants.values()],
        }, source="planner", sim_time=self.now)

    async def _archive_if_done(self) -> None:
        """No active incidents and nothing awaiting a decision: the cards go to the journal (§27.5)."""
        if self.incidents or self.proposed() or not self.variants:
            return
        await self._journal("decisions_archived", "Активных решений нет: карточки перенесены в журнал")
        self.variants = {}
        await self._publish_variants()

    async def _sync_hold(self) -> None:
        """While a decision is pending the line runs in real time (×1): at ×60 two seconds of thinking are two
        minutes of trains moving under the old plan, and the cards would go stale under the dispatcher's eyes."""
        want = bool(self.proposed())
        if want == self._hold:
            return
        self._hold = want
        await self.bus.publish("cmd.field.clock", {"decision_hold": want}, source="planner")
        await self._journal("decision_hold", "Ждём решения диспетчера — время идёт в реальном масштабе ×1" if want
                            else "Решение принято — прежняя скорость симуляции")

    async def _approve(self, plan: Plan, base_version: int | None) -> None:
        self.version += 1
        plan.version, plan.base_version = self.version, base_version
        self.plan = plan
        await self.bus.publish("plan.approved", plan, source="planner", sim_time=plan.created_at)

    async def _refresh(self, fc: Plan, why: str) -> None:
        self._last_refresh = time.monotonic()
        plan = fc.model_copy(update={"version": self.version, "base_version": self.plan.base_version,
                                     "strategy": self.plan.strategy, "solver": "refresh"})
        self.plan = plan
        await self.bus.publish("plan.refreshed", plan, source="planner", sim_time=plan.created_at)
        log.info("plan v%d re-timed: %s", self.version, why)

    # ---- 1 Hz loop: forecast, refresh, live cards, index ------------------------------------
    async def _index_loop(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            if self.plan is None or self.field is None:
                continue
            try:
                await self._tick()
            except Exception:
                log.exception("planner tick failed")

    async def _tick(self) -> None:
        snap = self.snapshot()
        fc = forecast_plan(snap, self.settings)
        if fc is not None:
            self.forecast = fc
            self._forecast_fails = 0
            await self._maybe_refresh(fc)
        else:
            self._forecast_fails += 1
            # keep the index on screen: forecast by the timetable order until the dispatcher picks a new plan
            self.forecast = forecast_plan(snap.model_copy(update={"hint": []}), self.settings) or self.forecast
        if self._guard_pending and (self._gen_task is None or self._gen_task.done()):
            why, self._guard_pending = self._guard_pending, None
            await self._guard_replan(why)
            return
        self._broken_since = (self._broken_since or snap.now) if self._forecast_fails else None
        if self._broken_since is not None and snap.now - self._broken_since >= GUARD_BROKEN_S \
                and (self._gen_task is None or self._gen_task.done()):
            self.guard_triggers += 1
            self._broken_since = None
            await self._guard_replan(f"план неисполним {mins(GUARD_BROKEN_S)} мин, а решение не принято")
            return
        # the approved plan no longer fits where the trains are (its order cycles, or the field has stopped
        # moving): re-plan from the current state with CP-SAT
        stalled = self.field.get("stalled_s", 0) >= self.settings["planner"]["replan_deviation_s"] * 3
        if (self._forecast_fails == 3 or stalled) and not self.proposed() \
                and (self._gen_task is None or self._gen_task.done()):
            log.warning("current plan is no longer executable, generating variants")
            await self._journal("plan_broken", "Текущий план перестал выполняться — считаю новые варианты")
            self._request("broken")
        if self.proposed() and (self._gen_task is None or self._gen_task.done()):
            await self._live_cards(snap)
        await self._archive_if_done()
        await self._sync_hold()
        src = self.forecast or self.plan
        await self.bus.publish("kpi.index", {
            "index": src.index.model_dump(mode="json"), "kpi": src.kpi.model_dump(mode="json"),
            "plan_version": self.version, "last_solve_ms": self.last_solve_ms, "forecast_ok": fc is not None,
            "plan_broken": self._forecast_fails >= 3, "deadlock_guard_triggered_total": self.guard_triggers,
        }, source="planner", sim_time=self.now)

    async def _live_cards(self, snap: Snapshot) -> None:
        """Re-time every proposed variant from "now", so the card shows what applying it would do at this
        moment; a variant that became unexecutable or worse than keeping the current plan goes stale."""
        went_stale = []
        for v in self.proposed():
            durations = durations_for(STRATEGIES[v.strategy], snap.incidents, self.settings)
            new = retime_with_objective(snap.model_copy(update={"hint": v.plan.entries}), self.settings,
                                        v.strategy, durations)
            cur = retime_with_objective(snap, self.settings, v.strategy, durations)
            if new is None or (cur is not None and new[1] > cur[1] + STALE_MARGIN_S):
                v.status = "stale"
                went_stale.append(v)
                continue
            robust = v.plan.kpi.robust_total_delay_s
            v.plan = new[0].model_copy(update={"strategy": v.strategy, "solver": v.plan.solver,
                                               "solve_ms": v.plan.solve_ms, "created_at": v.plan.created_at})
            v.plan.kpi.robust_total_delay_s = robust
            v.updated_at = snap.now
        score_variants(self.world, snap, self.settings, list(self.variants.values()), self.forecast)
        if went_stale:
            await self._journal("variants_stale", "Устарели варианты: " + ", ".join(f"«{v.title}»" for v in went_stale)
                                + " — положение поездов изменилось")
            if not self.proposed():
                self._request("broken" if not self.incidents else "incident")
        await self._publish_variants()

    async def _maybe_refresh(self, fc: Plan) -> None:
        """Refresh (§12.6): trains drifted from the plan but the order holds -> re-time the plan automatically.
        The version is kept: the order (what the dispatcher approved) did not change, so variants stay valid."""
        limit = self.settings["planner"]["replan_deviation_s"]
        # only lagging counts: a train held before an obstacle looks "ahead" until the plan lets it go
        drift = max((t["delay_s"] for t in self.field["trains"] if t["on_field"]), default=0)
        new_trains = {e.train_id for e in fc.entries} - {e.train_id for e in self.plan.entries}
        if (drift <= limit and not new_trains) or time.monotonic() - self._last_refresh < 5 \
                or (self._gen_task and not self._gen_task.done()):
            return
        await self._refresh(fc, f"max lag {drift:.0f} s, entered horizon {sorted(new_trains)}")

    # ---- commands ---------------------------------------------------------------------------
    async def _on_cmd(self, env: Envelope) -> None:
        cmd = env.type.rsplit(".", 1)[-1]
        if cmd == "apply":
            await self._apply(env)
        elif cmd == "reject":
            await self._reject(env)
        elif cmd == "replan":
            self._request("replan")
            await self._reply(env, ok=True, code=202)
        elif cmd == "whatif":
            await self._whatif(env)
        else:
            await self._reply(env, ok=False, code=400, reason=f"unknown command {cmd}")

    async def _reply(self, env: Envelope, **result) -> None:
        if env.corr_id:
            await self.bus.publish("planner.command_result", result, corr_id=env.corr_id, source="planner")

    async def _reject(self, env: Envelope) -> None:
        v = self.variants.get(env.payload.get("variant_id"))
        if v is None:
            return await self._reply(env, ok=False, code=404, reason="Вариант не найден")
        if v.status != "proposed":
            return await self._reply(env, ok=False, code=409, reason=f"Вариант уже не активен ({v.status})")
        v.status = "rejected"
        await self._journal("variant_rejected", f"Диспетчер отклонил «{v.title}»", variant_id=v.id, strategy=v.strategy)
        await self._publish_variants()
        await self._archive_if_done()
        await self._sync_hold()
        await self._reply(env, ok=True, code=200)

    async def _apply(self, env: Envelope) -> None:
        vid, base = env.payload.get("variant_id"), env.payload.get("base_plan_version")
        v = self.variants.get(vid)
        if v is None:
            return await self._reply(env, ok=False, code=404, reason="Вариант не найден")
        if v.status != "proposed" or base != self.version or v.base_plan_version != self.version:
            if v.status == "proposed":
                v.status = "stale"
            return await self._reply(env, ok=False, code=409,
                                     reason=f"Вариант устарел: текущая версия плана {self.version}")
        # keep the variant's order (the decision) and re-time it from now; compare like for like with keeping
        # the current plan, both scored with the objective the solver optimised for this strategy
        snap = self.snapshot()
        durations = durations_for(STRATEGIES[v.strategy], snap.incidents, self.settings)
        new = retime_with_objective(snap.model_copy(update={"hint": v.plan.entries}), self.settings, v.strategy, durations)
        cur = retime_with_objective(snap, self.settings, v.strategy, durations)
        reason = None
        if new is None:
            reason = "Поезда уже разъехались не так, как предполагал вариант — пересчитываю варианты"
        elif cur is not None and new[1] > cur[1] + STALE_MARGIN_S:
            reason = (f"Пока вариант ждал решения, он стал хуже текущего плана (задержка "
                      f"{round(new[0].kpi.total_delay_s / 60)} мин против {round(cur[0].kpi.total_delay_s / 60)}) — "
                      f"пересчитываю варианты")
        if reason:
            for other in self.proposed():  # the whole batch was computed for the same, now outdated state
                other.status = "stale"
            await self._journal("variants_stale", reason)
            await self._publish_variants()
            self._request("incident" if self.incidents else "broken")
            return await self._reply(env, ok=False, code=409, reason=reason)
        plan = new[0]
        plan.strategy, plan.solver, plan.solve_ms = v.strategy, v.plan.solver, v.plan.solve_ms
        plan.kpi.robust_total_delay_s = v.plan.kpi.robust_total_delay_s
        await self._approve(plan, base)
        for other in self.variants.values():
            other.status = "applied" if other.id == vid else ("stale" if other.status == "proposed" else other.status)
        await self._journal("variant_applied", f"Применён «{v.title}» → план v{self.version}: прогноз индекса "
                            f"{plan.index.value:.0f}, суммарное опоздание {mins(plan.kpi.total_delay_s)} мин",
                            variant_id=v.id, strategy=v.strategy, incident_ids=v.incident_ids)
        await self._publish_variants()
        await self._archive_if_done()
        await self._sync_hold()
        log.info("variant %s (%s) applied as plan v%d", vid, v.strategy, self.version)
        await self._reply(env, ok=True, code=200, plan_version=self.version, index=plan.index.value,
                          total_delay_s=plan.kpi.total_delay_s)

    async def _whatif(self, env: Envelope) -> None:
        req = WhatIfRequest.model_validate(env.payload)
        if self.plan is None:
            return await self._reply(env, ok=False, code=409, reason="План ещё не построен")
        snap = self.snapshot()
        try:
            res = await run_whatif(self.pool, self.world, snap, req, self.settings)
        except (ValueError, KeyError) as e:
            await self.bus.publish("planner.whatif.result", {"ok": False, "reason": str(e)},
                                   corr_id=env.corr_id, source="planner")
            return
        await self.bus.publish("planner.whatif.result", {"ok": True, **res.model_dump(mode="json")},
                               corr_id=env.corr_id, source="planner")
