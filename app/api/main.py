"""API gateway (MVP: no auth, no DB). REST + WS + static web-mvp."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import AbstractAsyncContextManager
from typing import Callable

from fastapi import FastAPI, HTTPException, WebSocket
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.api.state_cache import StateCache
from app.api.ws import WSManager
from app.bus.base import EventBus
from app.bus.envelope import Envelope
from app.common.config import get_env
from app.railcore.eco import train_profile
from app.railcore.infra import World
from app.railcore.models import Direction, Incident, Modification, Plan, SpeedProfile, TrainState, Variant, WhatIfResult
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


class IncidentCreate(BaseModel):
    type: str = Field(description="obstacle | train_failure | segment_closed")
    segment_id: str | None = None
    train_id: str | None = None
    km: float | None = None
    est_min_min: float = Field(description="Оценка длительности, минимум, мин")
    est_max_min: float = Field(description="Оценка длительности, максимум, мин")
    actual_min: float | None = Field(None, description="Фактическая длительность (для сценариев), мин")
    description: str | None = None


class ApplyRequest(BaseModel):
    variant_id: str
    base_plan_version: int


class WhatIfBody(BaseModel):
    modifications: list[Modification]


class ClockRequest(BaseModel):
    paused: bool | None = None
    speed: float | None = None


async def subscribe_api(bus: EventBus, cache: StateCache, ws: WSManager, req: Requester) -> None:
    for topic in ("field.state", "plan.approved", "planner.variants", "kpi.index"):
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
        res = await req.request("cmd.field.create_incident", body.model_dump())
        if not res["ok"]:
            raise HTTPException(422, res.get("reason"))
        return res["incident"]

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
                "safety_violations": cache.field and cache.field["safety_violations"]}

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        await wsm.handle(ws)

    web = get_env().web_dir

    @app.get("/", include_in_schema=False)
    async def index_html() -> FileResponse:
        return FileResponse(web / "index.html")

    app.mount("/static", StaticFiles(directory=web), name="static")
    return app
