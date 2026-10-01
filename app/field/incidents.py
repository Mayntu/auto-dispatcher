"""Incident creation for the field service (CLAUDE.md §11.4). MVP types: obstacle, train_failure, segment_closed.

`actual_s` lives only here and is never serialized to the bus or the API.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass

from app.railcore.models import Incident, IncidentType

MVP_TYPES = {IncidentType.OBSTACLE, IncidentType.TRAIN_FAILURE, IncidentType.SEGMENT_CLOSED}
DEFAULT_DESCRIPTION = {
    IncidentType.OBSTACLE: "Скот на пути, бригада выехала",
    IncidentType.TRAIN_FAILURE: "Неисправность локомотива поезда {train}",
    IncidentType.SEGMENT_CLOSED: "Закрытие перегона для работ",
}


@dataclass
class FieldIncident:
    incident: Incident
    actual_s: float

    @property
    def id(self) -> str:
        return self.incident.id

    def ends_at(self) -> float:
        return self.incident.started_at + self.actual_s


class IncidentError(ValueError):
    pass


def make_incident(req: dict, now: float, segment_id: str | None, km: float | None,
                  rng: random.Random) -> FieldIncident:
    """`req` uses API units (minutes): {type, segment_id?, train_id?, km?, est_min_min, est_max_min, actual_min?}."""
    itype = IncidentType(req["type"])
    if itype not in MVP_TYPES:
        raise IncidentError(f"incident type '{itype}' is not supported in MVP")
    lo = round(float(req["est_min_min"]) * 60)
    hi = round(float(req.get("est_max_min") or req["est_min_min"]) * 60)
    if hi < lo:
        raise IncidentError("est_max_min must be >= est_min_min")
    actual = float(req["actual_min"]) * 60 if req.get("actual_min") is not None else rng.uniform(lo, hi)
    desc = req.get("description") or DEFAULT_DESCRIPTION[itype].format(train=req.get("train_id"))
    inc = Incident(
        id=uuid.uuid4().hex[:8], type=itype, status="active", segment_id=segment_id,
        train_id=req.get("train_id"), km=km, started_at=now, est_min_s=lo, est_max_s=hi,
        est_expected_s=round((lo + hi) / 2), params=req.get("params") or {}, description=desc,
    )
    return FieldIncident(inc, actual)
