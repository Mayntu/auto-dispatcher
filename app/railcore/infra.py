"""Static world: topology, rolling stock, timetable, plus topology helpers."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from app.common.config import get_env
from app.railcore.models import Direction, Infra, Segment, Station, Train, TrainCategory


class World:
    def __init__(self, infra: Infra, categories: list[TrainCategory], timetable: list[Train]):
        self.infra = infra
        self.categories = {c.id: c for c in categories}
        self.timetable = timetable
        self.trains = {t.id: t for t in timetable}
        self.stations = {s.id: s for s in infra.stations}
        self.segments = {s.id: s for s in infra.segments}
        self.station_order = [s.id for s in sorted(infra.stations, key=lambda s: s.km)]
        self._between = {frozenset((s.from_station, s.to_station)): s for s in infra.segments}

    def station(self, station_id: str) -> Station:
        return self.stations[station_id]

    def segment(self, segment_id: str) -> Segment:
        return self.segments[segment_id]

    def segment_between(self, a: str, b: str) -> Segment:
        return self._between[frozenset((a, b))]

    def neighbors(self, station_id: str) -> list[str]:
        i = self.station_order.index(station_id)
        return [self.station_order[j] for j in (i - 1, i + 1) if 0 <= j < len(self.station_order)]

    def capacity(self, station_id: str) -> int:
        return len(self.stations[station_id].tracks)

    def segment_km(self, segment_id: str) -> tuple[float, float]:
        seg = self.segments[segment_id]
        return self.stations[seg.from_station].km, self.stations[seg.to_station].km

    def km_of(self, segment_id: str, pos_m: float, direction: Direction) -> float:
        """Km of a point `pos_m` metres from the segment start in the direction of travel."""
        a, b = self.segment_km(segment_id)
        return a + pos_m / 1000 if direction == Direction.ODD else b - pos_m / 1000

    def pos_of_km(self, segment_id: str, km: float, direction: Direction) -> float:
        a, b = self.segment_km(segment_id)
        return (km - a) * 1000 if direction == Direction.ODD else (b - km) * 1000

    def route(self, train: Train) -> list[str]:
        return [s.station_id for s in train.stops]

    def category(self, train: Train) -> TrainCategory:
        return self.categories[train.category]


def _read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def load_world(data_dir: Path | None = None, with_timetable: bool = True) -> World:
    d = data_dir or get_env().data_dir
    infra = Infra.model_validate(_read(d / "infra.json"))
    cats = [TrainCategory.model_validate(c) for c in _read(d / "rolling_stock.json")["categories"]]
    tt: list[Train] = []
    if with_timetable:
        tt = [Train.model_validate(t) for t in _read(d / "timetable.json")["trains"]]
    return World(infra, cats, tt)


@lru_cache
def get_world() -> World:
    return load_world()
