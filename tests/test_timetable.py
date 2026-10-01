"""unit/timetable (CLAUDE.md §9.3, §21): continuous, conflict-free normative timetable."""

from __future__ import annotations

from collections import defaultdict

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
    """One train per single-track segment (+ clearance), never more trains than tracks at a station."""
    runs = defaultdict(list)
    visits = defaultdict(list)
    for t in TT:
        for a, b in zip(t.stops, t.stops[1:]):
            runs[WORLD.segment_between(a.station_id, b.station_id).id].append((a.dep, b.arr, t.id))
        for s in t.stops[1:-1]:
            visits[s.station_id] += [(s.arr, 1), (s.dep, -1)]
    for seg, rs in runs.items():
        rs.sort()
        for (s1, e1, a), (s2, _, b) in zip(rs, rs[1:]):
            assert s2 >= e1 + 60 - 1, f"{seg}: {b} enters before {a} clears"
    for st, ev in visits.items():
        level = 0
        for _, d in sorted(ev, key=lambda x: (x[0], x[1])):
            level += d
            assert level <= WORLD.capacity(st), f"{st} over capacity"


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
