"""unit/physics (CLAUDE.md §10, §21): analytic checks of the traction calculation."""

from __future__ import annotations

import math

from app.railcore.infra import get_world
from app.railcore.models import Direction, GradeZone, Segment
from app.railcore.physics import min_profile, resistance_n, traction_n

WORLD = get_world()
CAT = WORLD.categories


def flat(length_m: float, v_max_kmh: float = 200, grade: float = 0.0) -> Segment:
    zones = [GradeZone(from_m=0, to_m=length_m, permille=grade)] if grade else []
    return Segment(id="T", from_station="A", to_station="B", length_m=length_m, v_max_kmh=v_max_kmh, grade_zones=zones)


def test_constant_speed_run_matches_l_over_v():
    seg = flat(15000, v_max_kmh=120)
    p = min_profile(seg, CAT["passenger"], Direction.ODD, 120, 120)
    assert abs(p.time_s - 15000 / (120 / 3.6)) / p.time_s < 0.02


def test_acceleration_under_constant_force_matches_analytic():
    """Below the power limit the express accelerates under F_start: v² = 2·a·s with a = (F − R)/m_eff,
    R evaluated along the way — checked against a fine numeric integral of dt = ds / v."""
    cat = CAT["express"]
    seg = flat(400, v_max_kmh=200)
    p = min_profile(seg, cat, Direction.ODD, 0, 200)
    m_eff = cat.mass_t * 1000 * (1 + cat.rotating_mass)
    v, t, h = 0.0, 0.0, 0.01
    for _ in range(int(400 / h)):
        a = (traction_n(cat, v) - resistance_n(cat, v)) / m_eff
        v2 = math.sqrt(v * v + 2 * a * h)
        t += 2 * h / (v + v2)
        v = v2
    assert abs(p.time_s - t) / t < 0.02
    assert abs(p.v[-1] - v) / v < 0.02


def test_braking_distance_is_v2_over_2b():
    cat = CAT["passenger"]
    v = 120 / 3.6
    seg = flat(6000, v_max_kmh=120)
    p = min_profile(seg, cat, Direction.ODD, 120, 0, ds=5.0)
    brake_from = next(s for s, r in zip(p.s, p.regime) if r == "brake")
    distance = seg.length_m - brake_from
    assert abs(distance - v * v / (2 * cat.b_service)) / distance < 0.02


def test_freight_settles_at_45_to_52_kmh_on_8_permille():
    seg = flat(20000, v_max_kmh=100, grade=8)
    p = min_profile(seg, CAT["freight"], Direction.ODD, 80, 80)
    v_mid = p.v[len(p.v) * 3 // 4] * 3.6
    assert 45 <= v_mid <= 52, v_mid


def test_stop_supplements_are_positive_and_running_times_follow_physics():
    from app.railcore.running_time import RunningTimes

    rts = RunningTimes(WORLD)
    up = rts.get("freight", "STP-R2", Direction.ODD)
    down = rts.get("freight", "STP-R2", Direction.EVEN)
    assert up.sup_start > 0 and up.sup_end > 0
    assert up.t_pp > down.t_pp, "the 8 permille climb slows the freight train"
    curve = rts.get("express", "R1-STP", Direction.ODD)
    straight = rts.get("express", "R2-OZR", Direction.ODD)
    assert curve.t_pp / 18000 > straight.t_pp / 17000, "the 70 km/h curve costs time"
