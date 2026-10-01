"""Running times from the traction calculation (CLAUDE.md §10, `physics.py`).

For a category, segment, direction and speed override: `t_pp` — the fastest run passing both ends at line speed;
`sup_start` / `sup_end` — the extra time when the train starts from / stops at that end; traction energies of the
same runs. The interface is what the planner, the field and the ATO have used since the MVP.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.railcore.infra import World
from app.railcore.models import Direction
from app.railcore.physics import Restriction, line_speed_kmh, min_profile
from app.railcore.physics import resistance_n as _resistance_ms


@dataclass(frozen=True)
class RunTime:
    t_pp: float  # pass-to-pass, seconds
    sup_start: float  # extra when starting from a stop
    sup_end: float  # extra when stopping at the end
    v_kmh: float  # line speed at the segment ends (passing a station on the main track)
    e_pp_kwh: float  # traction energy of the pass-to-pass run
    e_start_kwh: float  # extra energy when starting from a stop


def resistance_n(cat, v_kmh: float, grade_permille: float = 0.0) -> float:
    """Davis + grade resistance, N (v in km/h) — kept for the ATO module."""
    return _resistance_ms(cat, v_kmh / 3.6, grade_permille)


class RunningTimes:
    def __init__(self, world: World):
        self.world = world
        self._cache: dict[tuple, RunTime] = {}

    def get(self, category: str, segment_id: str, direction: Direction, v_override: float | None = None,
            restrictions: tuple[Restriction, ...] = ()) -> RunTime:
        key = (category, segment_id, direction, v_override, restrictions)
        rt = self._cache.get(key)
        if rt is None:
            rt = self._cache[key] = self._compute(*key)
        return rt

    def _compute(self, category: str, segment_id: str, direction: Direction, v_override: float | None,
                 restrictions: tuple[Restriction, ...]) -> RunTime:
        cat = self.world.categories[category]
        seg = self.world.segments[segment_id]
        v = line_speed_kmh(seg, cat, v_override)

        def run(v_in: float, v_out: float):
            return min_profile(seg, cat, direction, v_in, v_out, v_override, restrictions)

        pp, sp, ps = run(v, v), run(0.0, v), run(v, 0.0)
        return RunTime(t_pp=pp.time_s, sup_start=max(0.0, sp.time_s - pp.time_s),
                       sup_end=max(0.0, ps.time_s - pp.time_s), v_kmh=v,
                       e_pp_kwh=pp.energy_kwh, e_start_kwh=max(0.0, sp.energy_kwh - pp.energy_kwh))

    def t_pass(self, category: str, segment_id: str, direction: Direction, pass_clear_m: float,
               v_override: float | None = None) -> float:
        """Time to pass a station without stopping (train length + clearance at line speed)."""
        cat = self.world.categories[category]
        v = self.get(category, segment_id, direction, v_override).v_kmh / 3.6
        return (cat.length_m + pass_clear_m) / v
