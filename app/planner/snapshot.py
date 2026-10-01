"""Build the planner's "now" snapshot from the latest field state."""

from __future__ import annotations

from app.railcore.models import Incident, Plan, Train, TrainState
from app.railcore.problem import Snapshot


def build_snapshot(field_state: dict, trains: list[Train], incidents: list[Incident], plan: Plan | None) -> Snapshot:
    """Only trains the field reports (on the line, upcoming, recently finished) take part: with a continuous
    48-hour timetable a train without a state is either long gone or far in the future."""
    states = {t["train_id"]: TrainState.model_validate(t) for t in field_state["trains"]}
    return Snapshot(
        now=field_state["sim_time"],
        trains=[t for t in trains if t.id in states],
        states=states,
        incidents=[i for i in incidents if i.status == "active"],
        hint=plan.entries if plan else [],
    )
