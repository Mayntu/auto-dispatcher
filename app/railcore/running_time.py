"""Running times. MVP: no physics, t_pp = L / (0.9 * v); fixed start/stop supplements.

The interface (t_pp, sup_start, sup_end, energy) is what §10 physics will later fill in.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.railcore.infra import World
from app.railcore.models import Direction, Segment, TrainCategory

SUP_START_S = 60.0
SUP_END_S = 60.0
FREIGHT_UPHILL = {("STP-R2", Direction.ODD): 0.6}  # MVP stand-in for the 8 permille climb


@dataclass(frozen=True)
class RunTime:
    t_pp: float  # pass-to-pass, seconds
    sup_start: float  # extra when starting from a stop
    sup_end: float  # extra when stopping at the end
    v_kmh: float  # effective running speed
    e_pp_kwh: float  # traction energy at constant speed
    e_start_kwh: float  # extra energy to accelerate from a stop


def effective_speed_kmh(cat: TrainCategory, seg: Segment, direction: Direction, v_override: float | None) -> float:
    v = min(cat.v_max_kmh, seg.v_max_kmh, v_override or 1e9)
    if cat.id == "freight":
        v *= FREIGHT_UPHILL.get((seg.id, direction), 1.0)
    return v


def mean_grade(seg: Segment, direction: Direction) -> float:
    """Length-weighted grade, permille, signed for the direction of travel (+ = uphill)."""
    g = sum(z.permille * (z.to_m - z.from_m) for z in seg.grade_zones) / seg.length_m
    return g if direction == Direction.ODD else -g


def resistance_n(cat: TrainCategory, v_kmh: float, grade_permille: float = 0.0) -> float:
    w0 = cat.davis_a + cat.davis_b * v_kmh + cat.davis_c * v_kmh**2
    return (w0 + grade_permille) * cat.mass_t * 9.81


class RunningTimes:
    def __init__(self, world: World):
        self.world = world
        self._cache: dict[tuple, RunTime] = {}

    def get(self, category: str, segment_id: str, direction: Direction, v_override: float | None = None) -> RunTime:
        key = (category, segment_id, direction, v_override)
        rt = self._cache.get(key)
        if rt is None:
            rt = self._cache[key] = self._compute(*key)
        return rt

    def _compute(self, category: str, segment_id: str, direction: Direction, v_override: float | None) -> RunTime:
        cat = self.world.categories[category]
        seg = self.world.segments[segment_id]
        v_kmh = effective_speed_kmh(cat, seg, direction, v_override)
        v = v_kmh / 3.6
        e_pp = max(resistance_n(cat, v_kmh, mean_grade(seg, direction)), 0.0) * seg.length_m / 3.6e6
        e_start = 0.5 * cat.mass_t * 1000 * (1 + cat.rotating_mass) * v * v / 3.6e6
        return RunTime(seg.length_m / (0.9 * v), SUP_START_S, SUP_END_S, v_kmh, e_pp, e_start)

    def t_pass(self, category: str, segment_id: str, direction: Direction, pass_clear_m: float,
               v_override: float | None = None) -> float:
        """Time to pass a station without stopping (train length + clearance at line speed)."""
        cat = self.world.categories[category]
        v = self.get(category, segment_id, direction, v_override).v_kmh / 3.6
        return (cat.length_m + pass_clear_m) / v
