"""Traffic quality index (CLAUDE.md §14). MVP: energy factor uses the simplified energy model."""

from __future__ import annotations

from app.railcore.models import KPI, IndexComponent, IndexValue

LABELS = {
    "schedule": ("Соблюдение расписания", "мин"),
    "capacity": ("Пропускная способность", "поездов/ч"),
    "energy": ("Энергоэффективность", "кВт·ч"),
    "conflicts": ("Конфликты и вынужденные остановки", "шт"),
    "accuracy": ("Точность прибытия", "%"),
}
CATEGORY_LABELS = {"norm": "Норма", "attention": "Внимание", "critical": "Критично"}


def compute_index(kpi: KPI, total_weight: float, horizon_s: float, settings: dict) -> IndexValue:
    """`total_weight` — sum of priority weights of trains in the horizon (for the weighted mean delay)."""
    cfg = settings["index"]
    w = cfg["weights"]
    norm = sum(w.values()) or 1.0
    refs = cfg["refs"]

    mean_delay = kpi.weighted_delay_s / total_weight if total_weight > 0 else 0.0
    scores = {
        "schedule": (1 - min(1.0, mean_delay / (refs["delay_ref_min"] * 60)), mean_delay / 60),
        "capacity": (
            min(1.0, kpi.throughput / kpi.planned_throughput) if kpi.planned_throughput else 1.0,
            kpi.throughput / (horizon_s / 3600),
        ),
        "energy": (min(1.0, kpi.energy_ideal_kwh / kpi.energy_kwh) if kpi.energy_kwh > 0 else 1.0, kpi.energy_kwh),
        "conflicts": (1 - min(1.0, kpi.conflicts / refs["conflicts_ref"]), float(kpi.conflicts)),
        "accuracy": (kpi.arrival_accuracy, kpi.arrival_accuracy * 100),
    }
    comps = [
        IndexComponent(key=k, label=LABELS[k][0], score=round(s, 4), weight=round(w[k] / norm, 4),
                       raw=round(raw, 2), unit=LABELS[k][1])
        for k, (s, raw) in scores.items()
    ]
    value = round(100 * sum(c.score * c.weight for c in comps), 1)
    return IndexValue(value=value, category=categorize(value, settings), components=comps)


def categorize(value: float, settings: dict) -> str:
    th = settings["index"]["thresholds"]
    if value >= th["norm"]:
        return "norm"
    return "attention" if value >= th["attention"] else "critical"
