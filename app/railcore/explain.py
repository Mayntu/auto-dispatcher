"""Template explanations in Russian, built from plan data (CLAUDE.md §12.10). No LLM."""

from __future__ import annotations

from app.railcore.index import CATEGORY_LABELS
from app.railcore.infra import World
from app.railcore.models import Incident, Meeting, Plan

CAT_NOM = {"express": "Скорый", "passenger": "Пассажирский", "freight": "Грузовой"}
CAT_ACC = {"express": "скорый", "passenger": "пассажирский", "freight": "грузовой"}


def plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    return few if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else many


def mins(s: float) -> int:
    return round(s / 60)


def signed_min(s: float) -> str:
    m = mins(s)
    return f"+{m} мин" if m > 0 else f"−{-m} мин" if m < 0 else "±0 мин"


def train_label(world: World, train_id: str, acc: bool = False) -> str:
    cat = world.trains[train_id].category if train_id in world.trains else "freight"
    return f"{(CAT_ACC if acc else CAT_NOM)[cat]} {train_id}"


def meeting_sentence(world: World, m: Meeting) -> str:
    st = world.stations[m.station_id].name
    verb = "пропуская" if m.kind == "crossing" else "пропуская вперёд"
    return f"{train_label(world, m.waiting_train)} ждёт на {st} {mins(m.wait_s)} мин, {verb} {train_label(world, m.passing_train, acc=True)}."


def delay_sentence(world: World, plan: Plan, compare: tuple[str, float] | None = None) -> str:
    k = plan.kpi
    s = f"Суммарная задержка {mins(k.total_delay_s)} мин"
    if compare is not None:
        title, other = compare
        s += f" ({signed_min(k.total_delay_s - other)} к «{title}»)"
    late = [t for t in k.delayed_trains if t[1] >= 60]
    if late:
        top = late[0]
        s += f", опаздывают {len(late)} {plural(len(late), 'поезд', 'поезда', 'поездов')}, больше всех — {top[0]} (+{mins(top[1])} мин)."
    else:
        s += ", опозданий по прибытию нет."
    return s


def robust_sentence(plan: Plan, incidents: list[Incident], strategy: str) -> str | None:
    rob = plan.kpi.robust_total_delay_s
    if rob is None or not incidents:
        return None
    worst = max(incidents, key=lambda i: i.est_max_s)
    extra = rob - plan.kpi.total_delay_s
    if strategy == "rescue":
        return f"Срок ограничен прибытием вспомогательного локомотива: риск затягивания снят ({signed_min(extra)} при худшем сценарии)."
    return f"Если сбой затянется до максимума ({mins(worst.est_max_s)} мин): {signed_min(extra)} задержки."


def index_sentence(plan: Plan, base_index: float | None) -> str:
    v = plan.index.value
    s = f"Индекс {v:.0f} → «{CATEGORY_LABELS[plan.index.category]}»"
    if base_index is not None:
        d = v - base_index
        s += f" ({'+' if d >= 0 else '−'}{abs(d):.0f} к текущему прогнозу)"
    return s + "."


def explain_plan(world: World, plan: Plan, incidents: list[Incident], strategy: str,
                 base_index: float | None, compare: tuple[str, float] | None = None) -> list[str]:
    lines = []
    waits = sorted(plan.meetings, key=lambda m: -m.wait_s)
    if waits:
        lines.append(meeting_sentence(world, waits[0]))
    lines.append(delay_sentence(world, plan, compare))
    rob = robust_sentence(plan, incidents, strategy)
    if rob:
        lines.append(rob)
    lines.append(index_sentence(plan, base_index))
    return lines[:4]
