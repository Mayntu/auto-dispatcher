"""Traction calculation (CLAUDE.md §10): the fastest run over a segment.

Davis resistance w0 = a + b·v + c·v² [N/kN] (v in km/h) plus grade [N/kN = ‰], tractive effort
F(v) = min(F_start, P / v), service braking b_service. Classic forward/backward pass on a 25 m grid:
forward — full traction from v_in, never above the speed limit (holding it costs F = max(R, 0));
backward — the braking curve to v_out; the run is the lower envelope of the two.

Positions are metres in the direction of travel; zones in infra are odd-direction coordinates.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from app.railcore.models import Direction, Segment, TrainCategory

DS = 25.0
G = 9.81
V_MIN = 1.0  # m/s: tractive effort is P / max(v, 1 m/s)


@dataclass(frozen=True)
class Restriction:
    """A temporary speed restriction (warning): odd-direction metres on the segment."""

    from_m: float
    to_m: float
    v_kmh: float


@dataclass
class Profile:
    time_s: float
    energy_kwh: float
    s: list[float]  # grid, metres in the direction of travel
    v: list[float]  # m/s at grid points
    regime: list[str]  # per step: accel | cruise | coast | brake


def _travel(seg: Segment, a: float, b: float, direction: Direction) -> tuple[float, float]:
    """Odd-direction interval -> interval in the direction of travel."""
    return (a, b) if direction == Direction.ODD else (seg.length_m - b, seg.length_m - a)


def speed_limits(seg: Segment, cat: TrainCategory, direction: Direction, v_override_kmh: float | None = None,
                 restrictions: tuple[Restriction, ...] = (), ds: float = DS) -> tuple[list[float], list[float]]:
    """Grid (travel metres) and the speed limit at every grid point, m/s. A limit applies while any part of
    the train is inside the zone: it starts at the zone and ends one train length after it."""
    n = max(1, math.ceil(seg.length_m / ds))
    grid = [min(seg.length_m, k * ds) for k in range(n + 1)]
    base = min(cat.v_max_kmh, seg.v_max_kmh, v_override_kmh or math.inf)
    zones = [(*_travel(seg, z.from_m, z.to_m, direction), z.v_kmh) for z in seg.speed_zones]
    zones += [(*_travel(seg, r.from_m, r.to_m, direction), r.v_kmh) for r in restrictions]
    lim = []
    for s in grid:
        v = base
        for a, b, vz in zones:
            if a - 1e-6 <= s <= b + cat.length_m:
                v = min(v, vz)
        lim.append(v / 3.6)
    return grid, lim


def grades(seg: Segment, direction: Direction, grid: list[float]) -> list[float]:
    """Grade at grid points, ‰, + = uphill for this direction."""
    out = []
    for s in grid:
        x = s if direction == Direction.ODD else seg.length_m - s
        g = 0.0
        for z in seg.grade_zones:
            if z.from_m <= x < z.to_m:
                g = z.permille
        out.append(g if direction == Direction.ODD else -g)
    return out


def resistance_n(cat: TrainCategory, v_ms: float, grade_permille: float = 0.0) -> float:
    v_kmh = v_ms * 3.6
    w0 = cat.davis_a + cat.davis_b * v_kmh + cat.davis_c * v_kmh**2
    return (w0 + grade_permille) * cat.mass_t * G


def traction_n(cat: TrainCategory, v_ms: float) -> float:
    return min(cat.f_start_kn * 1000.0, cat.power_kw * 1000.0 / max(v_ms, V_MIN))


def min_profile(seg: Segment, cat: TrainCategory, direction: Direction, v_in_kmh: float, v_out_kmh: float,
                v_override_kmh: float | None = None, restrictions: tuple[Restriction, ...] = (),
                ds: float = DS) -> Profile:
    """The fastest run from v_in to v_out (0 = stop at that end)."""
    grid, lim = speed_limits(seg, cat, direction, v_override_kmh, restrictions, ds)
    gr = grades(seg, direction, grid)
    m_eff = cat.mass_t * 1000.0 * (1.0 + cat.rotating_mass)
    n = len(grid)
    # forward: full traction, capped by the limit
    fw = [0.0] * n
    fw[0] = min(v_in_kmh / 3.6, lim[0])
    for k in range(n - 1):
        h = grid[k + 1] - grid[k]
        v = fw[k]
        a = (traction_n(cat, v) - resistance_n(cat, v, gr[k])) / m_eff
        fw[k + 1] = min(math.sqrt(max(0.0, v * v + 2.0 * a * h)), lim[k + 1])
    # backward: service braking into v_out and into every lower limit ahead
    bw = [0.0] * n
    bw[-1] = min(v_out_kmh / 3.6, lim[-1])
    for k in range(n - 2, -1, -1):
        h = grid[k + 1] - grid[k]
        bw[k] = min(math.sqrt(bw[k + 1] ** 2 + 2.0 * cat.b_service * h), lim[k])
    v = [min(f, b) for f, b in zip(fw, bw)]
    time_s = 0.0
    energy_j = 0.0
    regime = []
    for k in range(n - 1):
        h = grid[k + 1] - grid[k]
        v1, v2 = v[k], v[k + 1]
        vm = max((v1 + v2) / 2.0, 1e-3)
        time_s += h / vm if v1 + v2 > 1e-6 else 0.0
        if bw[k + 1] < fw[k + 1] - 1e-9 and v2 < v1 - 1e-9:
            regime.append("brake")
        elif v2 > v1 + 1e-9:
            regime.append("accel")
            energy_j += traction_n(cat, vm) * h
        elif v2 < v1 - 1e-9:  # slowing under full traction (uphill)
            regime.append("accel")
            energy_j += traction_n(cat, vm) * h
        else:
            regime.append("cruise")
            energy_j += max(resistance_n(cat, vm, gr[k]), 0.0) * h
    return Profile(time_s, energy_j / 3.6e6, grid, v, regime)


def line_speed_kmh(seg: Segment, cat: TrainCategory, v_override_kmh: float | None = None) -> float:
    """Speed of a train passing a station on the main track at the segment boundary."""
    return min(cat.v_max_kmh, seg.v_max_kmh, v_override_kmh or math.inf)
