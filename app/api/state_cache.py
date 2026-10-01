"""Latest state of everything the UI needs; source of the WS snapshot on connect."""

from __future__ import annotations

from app.bus.envelope import Envelope

FACT_WINDOW_S = 3 * 3600
FACT_MIN_STEP_S = 15


class StateCache:
    def __init__(self) -> None:
        self.field: dict | None = None
        self.plan: dict | None = None
        self.variants: dict | None = None
        self.index: dict | None = None
        self.fact: dict[str, list[list[float]]] = {}

    async def on_event(self, env: Envelope) -> None:
        t = env.type
        if t == "field.state":
            self.field = env.payload
            self._track(env.payload)
        elif t == "plan.approved":
            self.plan = env.payload
        elif t == "planner.variants":
            self.variants = env.payload
        elif t == "kpi.index":
            self.index = env.payload

    def _track(self, state: dict) -> None:
        now = state["sim_time"]
        for tr in state["trains"]:
            if not tr["on_field"]:
                continue
            trail = self.fact.setdefault(tr["train_id"], [])
            if not trail or now - trail[-1][0] >= FACT_MIN_STEP_S or tr["km"] != trail[-1][1] and now > trail[-1][0]:
                trail.append([round(now, 1), tr["km"]])
            while trail and trail[0][0] < now - FACT_WINDOW_S:
                trail.pop(0)

    def snapshot(self) -> dict:
        return {"field": self.field, "plan": self.plan, "variants": self.variants, "index": self.index,
                "fact": self.fact}
