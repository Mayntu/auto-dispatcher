"""Build the planner's "now" snapshot from the latest field state."""

from __future__ import annotations

from app.railcore.models import Incident, Plan, Train, TrainState
from app.railcore.problem import Snapshot


def build_snapshot(field_state: dict, trains: list[Train], incidents: list[Incident], plan: Plan | None) -> Snapshot:
    return Snapshot(
        now=field_state["sim_time"],
        trains=trains,
        states={t["train_id"]: TrainState.model_validate(t) for t in field_state["trains"]},
        incidents=[i for i in incidents if i.status == "active"],
        hint=plan.entries if plan else [],
    )
