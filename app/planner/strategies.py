"""Variant strategies (CLAUDE.md §12.3)."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.railcore.models import Incident, IncidentType


@dataclass(frozen=True)
class Strategy:
    id: str
    title: str
    duration: str = "expected"  # expected | max | rescue
    weight_mult: dict[str, float] = field(default_factory=dict)
    lambda_stop_mult: float = 1.0


STRATEGIES = {
    s.id: s
    for s in [
        Strategy("balanced", "Минимум задержек"),
        Strategy("passenger_first", "Приоритет пассажирских", weight_mult={"express": 3, "passenger": 3, "freight": 0.5}),
        Strategy("robust", "Устойчивый план", duration="max"),
        Strategy("fewer_stops", "Меньше остановок", lambda_stop_mult=4),
        Strategy("rescue", "Вспомогательный локомотив", duration="rescue"),
        # the dispatcher's instruction from the train graph (tasks/02): both weigh like "balanced"
        Strategy("keep_order", "Сохранить порядок"),
        Strategy("reoptimize", "Переразвести"),
    ]
}


def rescue_applicable(incidents: list[Incident], settings: dict) -> bool:
    eta = settings["planner"]["rescue_eta_s"]
    return any(i.type == IncidentType.TRAIN_FAILURE and i.est_max_s > eta for i in incidents)


def pick_strategies(incidents: list[Incident], settings: dict) -> list[str]:
    third = "rescue" if rescue_applicable(incidents, settings) else "passenger_first"
    return ["balanced", "robust", third]


def durations_for(strategy: Strategy, incidents: list[Incident], settings: dict) -> dict[str, int]:
    p = settings["planner"]
    out = {}
    for inc in incidents:
        if strategy.duration == "max":
            out[inc.id] = inc.est_max_s
        elif strategy.duration == "rescue" and inc.type == IncidentType.TRAIN_FAILURE:
            out[inc.id] = min(inc.est_max_s, p["rescue_eta_s"] + p["rescue_haul_s"])
        else:
            out[inc.id] = inc.est_expected_s
    return out
