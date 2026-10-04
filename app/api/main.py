"""API gateway (MVP: no auth, no DB). REST + WS + static web-mvp."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import AbstractAsyncContextManager
from typing import Callable

from fastapi import FastAPI, HTTPException, Query, Response, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.api.reports import build_report_csv
from app.api.state_cache import StateCache
from app.api.ws import WSManager
from app.bus.base import EventBus
from app.bus.envelope import Envelope
from app.common.config import get_env
from app.railcore.eco import train_profile
from app.railcore.infra import World
from app.railcore.models import Direction, Incident, JournalEntry, Modification, Plan, SpeedProfile, TrainState, Variant, WhatIfResult
from app.railcore.running_time import RunningTimes

REPLY_TOPICS = ("dc.command_result", "planner.command_result", "planner.whatif.result")


class Requester:
    """Command -> reply over the bus, matched by corr_id."""

    def __init__(self, bus: EventBus):
        self.bus = bus
        self.pending: dict[str, asyncio.Future] = {}

    async def on_reply(self, env: Envelope) -> None:
        fut = self.pending.pop(env.corr_id or "", None)
        if fut and not fut.done():
            fut.set_result(env.payload)

    async def request(self, topic: str, payload: dict, timeout: float = 10.0) -> dict:
        corr = uuid.uuid4().hex
        fut = asyncio.get_running_loop().create_future()
        self.pending[corr] = fut
        await self.bus.publish(topic, payload, corr_id=corr, source="api")
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self.pending.pop(corr, None)
            raise HTTPException(504, "Сервис не ответил вовремя")


# §9.4, §27.4: interval names and definitions for the settings screen
INTERVAL_LABELS = {
    "headway_s": {"name": "Межпоездной интервал",
                  "hint": "Минимальный интервал между попутными поездами на перегоне с автоблокировкой."},
    "tau_cross_s": {"name": "Станционный интервал скрещения (τск)",
                    "hint": "От прибытия поезда на раздельный пункт до отправления встречного на освободившийся перегон."},
    "tau_np_s": {"name": "Станционный интервал неодновременного прибытия (τнп)",
                 "hint": "Между прибытиями встречных поездов на пункт, где одновременный приём запрещён (разъезды)."},
    "direction_change_s": {"name": "Время смены направления",
                           "hint": "Смена установленного направления на свободном перегоне."},
}


class IncidentCreate(BaseModel):
    type: str = Field(description="obstacle | train_failure | segment_closed (окно) | speed_restriction (предупреждение) | "
                                  "train_delay (train_id) | signal_failure (station_id + direction: выходной светофор)")
    segment_id: str | None = None
    station_id: str | None = None
    direction: str | None = Field(None, description="signal_failure: odd | even")
    train_id: str | None = None
    km: float | None = None
    km_from: float | None = Field(None, description="Предупреждение: начало, км")
    km_to: float | None = Field(None, description="Предупреждение: конец, км")
    v_kmh: float | None = Field(None, description="Предупреждение: допустимая скорость, км/ч")
    est_min_min: float = Field(description="Оценка длительности, минимум, мин")
    est_max_min: float = Field(description="Оценка длительности, максимум, мин")
    actual_min: float | None = Field(None, description="Фактическая длительность (для сценариев), мин")
    description: str | None = None


class EstimateRequest(BaseModel):
    est_min_min: float = Field(description="Новая оценка длительности сбоя от его начала, минимум, мин")
    est_max_min: float = Field(description="Максимум, мин")
    actual_min: float | None = Field(None, description="Фактическая длительность (для сценариев), мин")
    description: str | None = None


class SettingsPatch(BaseModel):
    """Only these parts are editable at run time; anything else is ignored."""

    index: dict | None = Field(None, description="weights {schedule, capacity, energy, conflicts, accuracy}, thresholds {norm, attention}, refs")
    priority_weights: dict | None = Field(None, description="{express, passenger, freight}")
    planner: dict | None = Field(None, description="time_limit_s, return_gain_s, replan_deviation_s, rescue_eta_s")
    intervals: dict | None = Field(None, description="headway_s, tau_cross_s, tau_np_s, direction_change_s")


EDITABLE = {"planner": {"time_limit_s", "return_gain_s", "replan_deviation_s", "rescue_eta_s", "rescue_haul_s"},
            "intervals": {"headway_s", "tau_cross_s", "tau_np_s", "direction_change_s"}}


def apply_settings(settings: dict, patch: SettingsPatch) -> dict:
    """Validate and merge in place: every service reads the same settings object, so changes apply at once."""
    new_index = dict(settings["index"])
    if patch.index:
        if "weights" in patch.index:
            w = {k: float(patch.index["weights"].get(k, settings["index"]["weights"][k])) for k in settings["index"]["weights"]}
            if any(x < 0 for x in w.values()) or sum(w.values()) <= 0:
                raise ValueError("веса индекса должны быть неотрицательными и не все нулевые")
            total = sum(w.values())
            new_index["weights"] = {k: round(x / total, 4) for k, x in w.items()}  # auto-normalised to sum 1
        if "thresholds" in patch.index:
            t = {**settings["index"]["thresholds"], **{k: float(v) for k, v in patch.index["thresholds"].items()}}
            if not 0 <= t["attention"] <= t["norm"] <= 100:
                raise ValueError("пороги: 0 ≤ «Внимание» ≤ «Норма» ≤ 100")
            new_index["thresholds"] = t
        if "refs" in patch.index:
            r = {**settings["index"]["refs"], **{k: float(v) for k, v in patch.index["refs"].items()}}
            if any(v <= 0 for v in r.values()):
                raise ValueError("опорные значения индекса должны быть > 0")
            new_index["refs"] = r
    pw = dict(settings["priority_weights"])
    if patch.priority_weights:
        pw.update({k: float(v) for k, v in patch.priority_weights.items() if k in pw})
        if any(v <= 0 for v in pw.values()):
            raise ValueError("веса приоритета должны быть > 0")
    sections = {}
    for name in ("planner", "intervals"):
        part = getattr(patch, name) or {}
        bad = set(part) - EDITABLE[name]
        if bad:
            raise ValueError(f"{name}: нельзя менять {sorted(bad)}")
        if any(float(v) < 0 for v in part.values()):
            raise ValueError(f"{name}: значения должны быть ≥ 0")
        sections[name] = {k: float(v) for k, v in part.items()}
    if sections["planner"].get("time_limit_s", 1) <= 0 or sections["planner"].get("time_limit_s", 1) > 4.5:
        raise ValueError("лимит решателя: от 0 до 4.5 с (бюджет вариантов ≤ 5 с)")
    settings["index"] = new_index
    settings["priority_weights"] = pw
    for name, part in sections.items():
        settings[name].update({k: (int(v) if name == "intervals" else v) for k, v in part.items()})
    return settings


class ManualDrag(BaseModel):
    base_plan_version: int
    train_id: str
    station_id: str
    kind: str = Field(description="arr | dep")
    time: float = Field(description="Новое время, секунды симуляции (округляется до минуты)")

    model_config = {"json_schema_extra": {"examples": [
        {"base_plan_version": 3, "train_id": "2003", "station_id": "STP", "kind": "dep", "time": 9900}]}}


class DirectionRequest(BaseModel):
    segment_id: str
    direction: str = Field(description="odd | even")


class ApplyRequest(BaseModel):
    variant_id: str
    base_plan_version: int


class WhatIfBody(BaseModel):
    modifications: list[Modification]


class ClockRequest(BaseModel):
    paused: bool | None = None
    speed: float | None = None


async def subscribe_api(bus: EventBus, cache: StateCache, ws: WSManager, req: Requester) -> None:
    for topic in ("field.state", "plan.approved", "plan.refreshed", "planner.variants", "kpi.index", "journal.entry",
                  "planner.conflicts", "sim.reset"):
        await bus.subscribe(topic, cache.on_event)
    await bus.subscribe("#", ws.on_event)
    for topic in REPLY_TOPICS:
        await bus.subscribe(topic, req.on_reply)


def create_app(bus: EventBus, world: World, settings: dict,
               lifespan: Callable[[FastAPI], AbstractAsyncContextManager] | None = None) -> FastAPI:
    app = FastAPI(title="Автодиспетчер API (MVP)", version="0.1.0", lifespan=lifespan,
                  description="Консультативная система поддержки решений поездного диспетчера однопутного участка.")
    cache = StateCache()
    wsm = WSManager(cache)
    req = Requester(bus)
    rts = RunningTimes(world)
    app.state.cache, app.state.ws, app.state.requester = cache, wsm, req

    @app.get("/health", summary="Проверка живости")
    async def health() -> dict:
        return {"ok": True, "sim_time": cache.field and cache.field["sim_time"]}

    @app.get("/api/infra", summary="Топология участка, категории поездов, расписание")
    async def infra() -> dict:
        return {
            "infra": world.infra.model_dump(mode="json"),
            "categories": [c.model_dump(mode="json") for c in world.categories.values()],
            "timetable": [t.model_dump(mode="json") for t in world.timetable],
            "sim_epoch": get_env().sim_epoch.isoformat(),
            "thresholds": settings["index"]["thresholds"],
            "station_order": world.station_order,
            "intervals": settings["intervals"],
            "interval_labels": INTERVAL_LABELS,
            "segment_times": {
                seg.id: {cat: {d.value: round(rts.get(cat, seg.id, d).t_pp / 60) for d in Direction}
                         for cat in world.categories}
                for seg in world.infra.segments
            },
        }

    @app.get("/api/plan", response_model=Plan, summary="Текущий утверждённый план")
    async def plan() -> dict:
        if cache.plan is None:
            raise HTTPException(404, "План ещё не построен")
        return cache.plan

    @app.get("/api/state", summary="Снимок для отладки: поле, план, варианты, индекс")
    async def state() -> dict:
        return cache.snapshot()

    @app.get("/api/incidents", response_model=list[Incident], summary="Активные сбои")
    async def incidents() -> list:
        return cache.field["incidents"] if cache.field else []

    @app.post("/api/incidents", response_model=Incident, summary="Создать сбой (запускает генерацию вариантов)")
    async def create_incident(body: IncidentCreate) -> dict:
        payload = body.model_dump()
        if body.type == "speed_restriction":
            payload["params"] = {"km_from": body.km_from, "km_to": body.km_to, "v_kmh": body.v_kmh}
        res = await req.request("cmd.field.create_incident", payload)
        if not res["ok"]:
            raise HTTPException(422, res.get("reason"))
        return res["incident"]

    @app.post("/api/sim/reset", summary="Начать симуляцию заново с 07:55 (поезда по графику, без сбоев и указаний)")
    async def sim_reset() -> dict:
        res = await req.request("cmd.field.reset", {})
        if not res["ok"]:
            raise HTTPException(500, res.get("reason"))
        return {"ok": True}

    @app.post("/api/dc/direction", summary="Сменить направление на перегоне (только свободный перегон)")
    async def set_direction(body: DirectionRequest) -> dict:
        res = await req.request("cmd.field.set_direction", body.model_dump())
        if not res["ok"]:
            raise HTTPException(409, res.get("reason"))
        return {"ok": True, "directions": res["directions"]}

    @app.post("/api/incidents/{incident_id}/resolve", response_model=Incident, summary="Снять сбой")
    async def resolve_incident(incident_id: str) -> dict:
        res = await req.request("cmd.field.resolve_incident", {"incident_id": incident_id})
        if not res["ok"]:
            raise HTTPException(404, res.get("reason"))
        return res["incident"]

    @app.get("/api/variants", response_model=list[Variant], summary="Варианты плана по последнему сбою")
    async def variants() -> list:
        return cache.variants["variants"] if cache.variants else []

    @app.post("/api/plan/apply", summary="Применить вариант; 409 если план уже изменился")
    async def apply(body: ApplyRequest) -> dict:
        res = await req.request("cmd.planner.apply", body.model_dump())
        if not res["ok"]:
            raise HTTPException(res.get("code", 409), res.get("reason"))
        return res

    async def planner_call(cmd: str, payload: dict):
        res = await req.request(f"cmd.planner.{cmd}", payload)
        if not res["ok"]:
            return JSONResponse(status_code=res.get("code", 409), content=res.get("body") or {"error": res.get("reason")})
        return res["body"]

    @app.get("/api/plan/manual/bounds", summary="Указания на ГИД: границы перетаскивания прибытия и отправления")
    async def manual_bounds(train_id: str, station_id: str):
        return await planner_call("manual_bounds", {"train_id": train_id, "station_id": station_id})

    @app.post("/api/incidents/{incident_id}/estimate", response_model=Incident,
              summary="Уточнить оценку длительности сбоя (пересчитывает варианты)")
    async def estimate(incident_id: str, body: EstimateRequest) -> dict:
        res = await req.request("cmd.field.update_incident_estimate", {"incident_id": incident_id, **body.model_dump()})
        if not res["ok"]:
            raise HTTPException(404 if "не найден" in (res.get("reason") or "") else 422, res.get("reason"))
        return res["incident"]

    @app.post("/api/whatif/{request_id}/promote", response_model=Variant,
              summary="What-if → «Перенести в работу»: вариант на решение диспетчера")
    async def promote(request_id: str) -> dict:
        res = await req.request("cmd.planner.promote_whatif", {"request_id": request_id})
        if not res["ok"]:
            raise HTTPException(res.get("code", 409), res.get("reason"))
        return res["body"]

    @app.get("/api/settings", summary="Настройки: веса и пороги индекса, приоритеты, решатель, интервалы")
    async def get_settings() -> dict:
        return {"index": settings["index"], "priority_weights": settings["priority_weights"],
                "planner": {k: settings["planner"][k] for k in EDITABLE["planner"] if k in settings["planner"]},
                "intervals": settings["intervals"], "interval_labels": INTERVAL_LABELS}

    @app.put("/api/settings", summary="Изменить настройки (применяются сразу, без перезапуска)")
    async def put_settings(body: SettingsPatch) -> dict:
        try:
            apply_settings(settings, body)
        except (ValueError, KeyError, TypeError) as e:
            raise HTTPException(422, str(e))
        await bus.publish("settings.updated", await get_settings(), source="api")
        return await get_settings()

    @app.get("/api/history/frames", summary="Кадры для перемотки (последние 20 мин симуляции, шаг ≥ 2 с)")
    async def history_frames(t_from: float | None = Query(None, alias="from"), t_to: float | None = Query(None, alias="to"),
                             step: float | None = None) -> list:
        return cache.history(t_from, t_to, step)

    @app.get("/api/history/events", summary="События и решения за период (журнал, часы симуляции)")
    async def history_events(t_from: float | None = Query(None, alias="from"),
                             t_to: float | None = Query(None, alias="to")) -> list:
        return [j for j in cache.journal if (t_from is None or j["time"] >= t_from) and (t_to is None or j["time"] <= t_to)]

    @app.get("/api/reports", summary="Отчёт за период: события, решения, опоздания, индекс по минутам (CSV)")
    async def report(t_from: float | None = Query(None, alias="from"), t_to: float | None = Query(None, alias="to"),
                     format: str = "csv"):
        if format != "csv":
            raise HTTPException(422, "В прототипе отчёт выгружается в CSV; PDF — в полной версии")
        return Response(build_report_csv(cache, t_from, t_to), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="report.csv"'})

    @app.get("/api/scenarios", summary="Сценарии сбоев для демонстрации (§9.5)")
    async def scenarios() -> list:
        from app.field.scenarios import load_scenarios

        return [{"id": x["id"], "title": x["title"], "steps": len(x["steps"]),
                 "duration_min": max(st["at_min"] for st in x["steps"])} for x in load_scenarios()]

    @app.post("/api/scenarios/{scenario_id}/run", summary="Запустить сценарий: шаги от текущего времени симуляции")
    async def run_scenario(scenario_id: str) -> dict:
        res = await req.request("cmd.field.run_scenario", {"scenario_id": scenario_id})
        if not res["ok"]:
            raise HTTPException(404 if "не найден" in (res.get("reason") or "") else 422, res.get("reason"))
        return {k: res[k] for k in ("run_id", "scenario_id", "steps", "log")}

    @app.post("/api/plan/manual/preview", summary="Указания на ГИД: как изменение расходится на другие поезда (≤ 100 мс)")
    async def manual_preview(body: ManualDrag):
        return await planner_call("manual_preview", body.model_dump())

    @app.post("/api/plan/manual/commit",
              summary="Указания на ГИД: варианты «Сохранить порядок» и «Переразвести» (≤ 5 с); применение — /api/plan/apply")
    async def manual_commit(body: ManualDrag):
        return await planner_call("manual_commit", body.model_dump())

    @app.get("/api/plan/pins", summary="Действующие указания диспетчера")
    async def pins():
        return await planner_call("pins", {})

    @app.delete("/api/plan/pins/{pin_id}", summary="Снять указание диспетчера")
    async def pin_remove(pin_id: str):
        return await planner_call("pin_remove", {"pin_id": pin_id})

    @app.post("/api/plan/variants/{variant_id}/reject", summary="Отклонить вариант")
    async def reject(variant_id: str) -> dict:
        res = await req.request("cmd.planner.reject", {"variant_id": variant_id})
        if not res["ok"]:
            raise HTTPException(res.get("code", 409), res.get("reason"))
        return res

    @app.get("/api/journal", response_model=list[JournalEntry], summary="Журнал решений и событий (часы симуляции)")
    async def journal(limit: int = 200) -> list:
        return cache.journal[-limit:]

    @app.post("/api/plan/replan", summary="Пересчитать план вручную: варианты придут в planner.variants")
    async def replan() -> dict:
        return await req.request("cmd.planner.replan", {})

    @app.post("/api/whatif", response_model=WhatIfResult, summary="What-if: прогноз на копии обстановки (≤ 3 с)")
    async def whatif(body: WhatIfBody) -> dict:
        version = cache.plan["version"] if cache.plan else 0
        payload = {"id": uuid.uuid4().hex[:8], "base_plan_version": version,
                   "modifications": [m.model_dump(mode="json") for m in body.modifications]}
        res = await req.request("cmd.planner.whatif", payload, timeout=15)
        if not res.pop("ok", False):
            raise HTTPException(422, res.get("reason"))
        return res

    @app.get("/api/ato/{train_id}", response_model=SpeedProfile, summary="Профиль скорости на ближайший перегон")
    async def ato(train_id: str) -> SpeedProfile:
        train = world.trains.get(train_id)
        if train is None or cache.field is None:
            raise HTTPException(404, "Поезд не найден")
        st = next(TrainState.model_validate(t) for t in cache.field["trains"] if t["train_id"] == train_id)
        plan = Plan.model_validate(cache.plan) if cache.plan else None
        prof = await asyncio.to_thread(train_profile, world, rts, plan, st, train, plan.version if plan else 0)
        if prof is None:
            raise HTTPException(404, "Поезд завершил маршрут")
        return prof

    @app.post("/api/sim/clock", summary="Пауза и скорость симуляции")
    async def clock(body: ClockRequest) -> dict:
        return await req.request("cmd.field.clock", body.model_dump())

    @app.get("/api/system/metrics", summary="Живые метрики для UI")
    async def metrics() -> dict:
        idx = cache.index or {}
        return {"last_solve_ms": idx.get("last_solve_ms"), "ui_p95_ms": wsm.p95_ms(),
                "safety_violations": cache.field and cache.field["safety_violations"],
                "deadlock_guard_triggered_total": idx.get("deadlock_guard_triggered_total", 0),
                "plan_overrides": cache.field and cache.field.get("plan_overrides")}

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        await wsm.handle(ws)

    web = get_env().web_dir

    @app.get("/", include_in_schema=False)
    async def index_html() -> FileResponse:
        return FileResponse(web / "index.html")

    app.mount("/static", StaticFiles(directory=web), name="static")
    return app
