"""Warnings — temporary speed restrictions (CLAUDE.md §4, §11.4 `speed_restriction`, §12.2).

An active `speed_restriction` incident carries `params = {km_from, km_to, v_kmh}`; here it becomes a
`physics.Restriction` in odd-direction metres of its segment, used by the running times (planner), the field
(the train never runs faster there) and the safety monitor.
"""

from __future__ import annotations

from app.railcore.infra import World
from app.railcore.models import Incident, IncidentType
from app.railcore.physics import Restriction


def restriction_of(inc: Incident, world: World) -> Restriction | None:
    if inc.type != IncidentType.SPEED_RESTRICTION or not inc.segment_id or inc.status != "active":
        return None
    a, b = world.segment_km(inc.segment_id)
    k1, k2 = sorted((float(inc.params["km_from"]), float(inc.params["km_to"])))
    return Restriction(from_m=round((k1 - a) * 1000), to_m=round((k2 - a) * 1000), v_kmh=float(inc.params["v_kmh"]))


def restrictions_by_segment(incidents: list[Incident], world: World) -> dict[str, tuple[Restriction, ...]]:
    out: dict[str, list[Restriction]] = {}
    for inc in incidents:
        r = restriction_of(inc, world)
        if r is not None:
            out.setdefault(inc.segment_id, []).append(r)
    return {seg: tuple(sorted(rs, key=lambda r: (r.from_m, r.to_m, r.v_kmh))) for seg, rs in out.items()}


def validate(params: dict, segment_id: str, world: World) -> dict:
    """Normalise and check a warning request (API units: km, km/h)."""
    try:
        k1, k2, v = float(params["km_from"]), float(params["km_to"]), float(params["v_kmh"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("предупреждение: нужны km_from, km_to, v_kmh") from None
    a, b = world.segment_km(segment_id)
    lo, hi = sorted((k1, k2))
    if lo < min(a, b) - 1e-6 or hi > max(a, b) + 1e-6 or hi - lo < 0.1:
        raise ValueError(f"предупреждение: отрезок {lo}–{hi} км вне перегона {segment_id} ({a}–{b} км) или короче 100 м")
    if not 5 <= v <= 120:
        raise ValueError("предупреждение: скорость 5–120 км/ч")
    return {"km_from": lo, "km_to": hi, "v_kmh": v}
