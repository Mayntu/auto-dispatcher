"""unit/timetable (CLAUDE.md §9.3, §21): continuous, conflict-free normative timetable."""

from __future__ import annotations

from collections import defaultdict

from app.common.config import load_settings
from app.railcore.infra import get_world

WORLD = get_world()
TT = WORLD.timetable
EPOCH_MIN = 7 * 60 + 55


def at(hhmm: str, day: int = 0) -> float:
    h, m = map(int, hhmm.split(":"))
    return (day * 24 * 60 + h * 60 + m - EPOCH_MIN) * 60.0


def test_gid_window_never_empty_for_48_hours():
    """In any moment of the first 48 h the ГИД window (-60…+180 min) shows at least 6 threads."""
    worst = None
    for now in range(int(at("08:00")), 48 * 3600, 300):
        n = sum(1 for t in TT if t.stops[0].dep <= now + 180 * 60 and t.stops[-1].arr >= now - 60 * 60)
        worst = n if worst is None else min(worst, n)
        assert n >= 6, f"only {n} threads in the window at sim {now:.0f}"
    assert worst <= 14


def test_normative_timetable_is_conflict_free():
    """Automatic block: oncoming trains never share a segment (+ clearance), followers keep the headway and
    never overtake on the line; never more trains than tracks at a station; tau_np at sidings."""
    iv = load_settings()["intervals"]
    clear = max(iv["tau_cross_s"], iv["direction_change_s"])
    runs = defaultdict(list)
    visits = defaultdict(list)
    arrivals = defaultdict(list)
    for t in TT:
        for a, b in zip(t.stops, t.stops[1:]):
            runs[WORLD.segment_between(a.station_id, b.station_id).id].append((a.dep, b.arr, t.id, t.direction))
        for s in t.stops[1:-1]:
            visits[s.station_id] += [(s.arr, 1), (s.dep, -1)]
        for s in t.stops[1:]:
            if not WORLD.stations[s.station_id].simultaneous_reception:
                arrivals[s.station_id].append((s.arr, t.id, t.direction))
    for seg, rs in runs.items():
        rs.sort()
        for x, (s1, e1, a, da) in enumerate(rs):
            for s2, e2, b, db in rs[x + 1:]:
                if da != db:
                    assert s2 >= e1 + clear - 1, f"{seg}: oncoming {b} enters before {a} clears"
                else:
                    assert s2 >= s1 + iv["headway_s"] - 1 and e2 >= e1 + iv["headway_s"] - 1, \
                        f"{seg}: {b} follows {a} closer than the headway"
    for st, ev in visits.items():
        level = 0
        for _, d in sorted(ev, key=lambda x: (x[0], x[1])):
            level += d
            assert level <= WORLD.capacity(st), f"{st} over capacity"
    for st, arrs in arrivals.items():
        arrs.sort()
        for (ta, a, da), (tb, b, db) in zip(arrs, arrs[1:]):
            assert da == db or tb - ta >= iv["tau_np_s"] - 1, f"{st}: oncoming {a}, {b} within tau_np"


def test_scenario_trains_depart_in_first_two_hours():
    by_id = {t.id: t for t in TT}
    for tid in ("2003", "2004", "341", "342", "101", "102"):
        assert by_id[tid].stops[0].dep <= at("09:55"), f"{tid} departs too late for the demo scenarios"


def test_template_numbering_and_categories():
    by_cat = defaultdict(list)
    for t in TT:
        by_cat[t.category].append(t)
        assert (int(t.id) % 2 == 1) == (t.direction.value == "odd"), f"{t.id}: odd number = odd direction"
    assert len(by_cat["express"]) >= 16 and len(by_cat["passenger"]) >= 32 and len(by_cat["freight"]) >= 64
