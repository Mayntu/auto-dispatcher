"""Latest state of everything the UI needs; source of the WS snapshot on connect."""

from __future__ import annotations

from collections import deque

from app.bus.envelope import Envelope

FACT_WINDOW_S = 3 * 3600
FACT_MIN_STEP_S = 15
JOURNAL_MAX = 2000
HISTORY_WINDOW_S = 20 * 60  # replay: the last 20 minutes of simulation (§1.7: 5-15 min)
HISTORY_MIN_STEP_S = 2.0  # one frame per 2 s of simulation at most
TRAIN_KEYS = ("train_id", "status", "segment_id", "station_id", "track_id", "block_id", "km", "pos_m", "speed_kmh",
              "delay_s", "next_station_id")


class StateCache:
    def __init__(self) -> None:
        self.field: dict | None = None
        self.plan: dict | None = None
        self.variants: dict | None = None
        self.index: dict | None = None
        self.fact: dict[str, list[list[float]]] = {}
        self.journal: list[dict] = []
        self.conflicts: list[dict] = []
        self.frames: deque[dict] = deque()  # replay frames without a database (§17: ring buffer in the API)
        self.index_minutes: list[tuple[float, float, str]] = []  # (sim minute, index, category) for the report

    async def on_event(self, env: Envelope) -> None:
        t = env.type
        if t == "field.state":
            self.field = env.payload
            self._track(env.payload)
            self._frame(env.payload)
        elif t in ("plan.approved", "plan.refreshed"):
            self.plan = env.payload
        elif t == "planner.variants":
            self.variants = env.payload
        elif t == "kpi.index":
            self.index = env.payload
            minute = (env.sim_time or 0) // 60 * 60
            if not self.index_minutes or self.index_minutes[-1][0] != minute:
                ix = env.payload["index"]
                self.index_minutes.append((minute, ix["value"], ix["category"]))
                del self.index_minutes[:-48 * 60]
        elif t == "planner.conflicts":
            self.conflicts = env.payload["conflicts"]
        elif t == "journal.entry":
            self.journal.append(env.payload)
            del self.journal[:-JOURNAL_MAX]

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

    def _frame(self, state: dict) -> None:
        now = state["sim_time"]
        if self.frames and now - self.frames[-1]["sim_time"] < HISTORY_MIN_STEP_S:
            return
        if self.frames and now < self.frames[-1]["sim_time"]:
            self.frames.clear()  # the simulation was restarted
        self.frames.append({
            "sim_time": now,
            "trains": [{k: tr.get(k) for k in TRAIN_KEYS} for tr in state["trains"] if tr.get("on_field")],
            "blocks": [b["id"] for b in state.get("blocks", []) if b.get("occupied_by") or b.get("obstacle")],
            "signals": {sg["id"]: sg["aspect"] for sg in state.get("signals", [])},
            "incidents": [i["id"] for i in state.get("incidents", [])],
            "index": self.index["index"]["value"] if self.index else None,
            "plan_version": self.plan["version"] if self.plan else None,
            "safety_violations": state.get("safety_violations", 0),
        })
        while self.frames and self.frames[0]["sim_time"] < now - HISTORY_WINDOW_S:
            self.frames.popleft()

    def history(self, t_from: float | None, t_to: float | None, step: float | None) -> list[dict]:
        out, last = [], None
        for f in self.frames:
            if (t_from is not None and f["sim_time"] < t_from) or (t_to is not None and f["sim_time"] > t_to):
                continue
            if step and last is not None and f["sim_time"] - last < step:
                continue
            out.append(f)
            last = f["sim_time"]
        return out

    def snapshot(self) -> dict:
        return {"field": self.field, "plan": self.plan, "variants": self.variants, "index": self.index,
                "fact": self.fact, "journal": self.journal[-100:], "conflicts": self.conflicts}
