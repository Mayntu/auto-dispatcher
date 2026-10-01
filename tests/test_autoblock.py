"""unit/autoblock (CLAUDE.md §11.3, §21; tasks/01-realism step 4 DoD)."""

from __future__ import annotations

from app.common.config import load_settings
from app.field.sim import FieldSim
from app.railcore.infra import get_world
from app.railcore.models import Direction
from app.railcore.running_time import RunningTimes

WORLD = get_world()
SETTINGS = load_settings()
SEG = "R1-STP"  # 18 km, 7 block sections, odd direction R1 -> STP


def sim_with(*placements) -> FieldSim:
    """placements: (train_id, 'segment'|'station', station index or segment start index, progress)."""
    sim = FieldSim(WORLD, RunningTimes(WORLD), SETTINGS)
    for tid, loc, idx, progress in placements:
        tr = sim.trains[tid]
        tr.loc, tr.idx, tr.progress, tr.arrived_at, tr.stopped = loc, idx, progress, 0.0, True
    for tr in sim.trains.values():  # everyone else: far away
        if tr.train.id not in {p[0] for p in placements}:
            tr.loc = "done"
    return sim


def odd_trains(n):
    return [t.id for t in WORLD.timetable if t.direction == Direction.ODD and t.category == "freight"][:n]


def even_train():
    return next(t.id for t in WORLD.timetable if t.direction == Direction.EVEN and t.category == "freight")


def test_aspects_red_yellow_green_behind_a_train():
    (a,) = odd_trains(1)
    sim = sim_with((a, "segment", 1, 0.5))  # route SEV(0) R1(1) STP(2): on R1-STP, head at 9 km
    sim.ab.dirs[SEG].direction = Direction.ODD
    sim._autoblock()
    occ = {b for b, who in sim.ab.occupied.items() if who == a}
    assert occ, "the train occupies block sections"
    first = min(int(b.rsplit("B", 1)[1]) for b in occ)
    # odd block signal at boundary k guards block k+1
    assert sim.ab.aspects[f"{SEG}-P{first - 1}N"] == "red"
    if first - 2 >= 1:
        assert sim.ab.aspects[f"{SEG}-P{first - 2}N"] == "yellow"
    if first - 3 >= 1:
        assert sim.ab.aspects[f"{SEG}-P{first - 3}N"] == "green"
    # set for the odd direction: every even block signal is red
    assert all(sim.ab.aspects[s.id] == "red" for s in WORLD.infra.signals
               if s.segment_id == SEG and s.direction == Direction.EVEN)


def test_pre_entry_repeats_closed_entry():
    sim = sim_with()
    sim.ab.dirs[SEG].direction = Direction.ODD
    sim.ab.update([], [], closed_entries={("STP", Direction.ODD)})
    pre = next(s for s in WORLD.infra.signals if s.segment_id == SEG and s.kind == "pre_entry"
               and s.direction == Direction.ODD)
    assert sim.ab.aspects[pre.id] == "yellow"
    sim.ab.update([], [], closed_entries=set())
    assert sim.ab.aspects[pre.id] == "green"


def test_follower_stops_at_red_behind_broken_train_and_oncoming_waits():
    leader, follower = odd_trains(2)
    oncoming = even_train()
    sim = sim_with((leader, "segment", 1, 0.55), (follower, "segment", 1, 0.05), (oncoming, "station", 4, 0.0))
    sim.trains[oncoming].idx = sim.trains[oncoming].route.index("STP")  # waiting at Степная for R1-STP
    sim.ab.dirs[SEG].direction = Direction.ODD
    sim.create_incident({"type": "train_failure", "train_id": leader, "est_min_min": 30, "est_max_min": 30})
    sim.step(1200)
    lead, fol = sim.trains[leader], sim.trains[follower]
    seg_len = WORLD.segments[SEG].length_m
    assert lead.loc == "segment" and abs(lead.progress - 0.55) < 1e-9, "the broken train stands"
    assert fol.loc == "segment" and fol.held_signal is not None, "the follower is held at a block signal"
    assert sim.ab.aspects[fol.held_signal] == "red"
    lead_tail = lead.progress * seg_len - WORLD.categories[lead.train.category].length_m
    assert fol.progress * seg_len < lead_tail, "and stops short of the leader's tail"
    assert sim.trains[oncoming].loc == "station", "the oncoming train does not enter a segment set the other way"
    assert sim.safety_violations == 0


def test_direction_changes_only_on_a_free_segment_and_takes_time():
    (a,) = odd_trains(1)
    sim = sim_with((a, "segment", 1, 0.5))
    ab = sim.ab
    ab.dirs[SEG].direction = Direction.ODD
    ab.request_direction(SEG, Direction.EVEN, sim.now, segment_empty=False)
    assert ab.dirs[SEG].direction == Direction.ODD and ab.dirs[SEG].changing_to is None, "occupied: no change"
    ab.request_direction(SEG, Direction.EVEN, sim.now, segment_empty=True)
    assert ab.dirs[SEG].changing_to == Direction.EVEN
    assert not ab.can_enter(SEG, Direction.EVEN) and not ab.can_enter(SEG, Direction.ODD), "no exit while changing"
    ab.tick_directions(sim.now + SETTINGS["intervals"]["direction_change_s"] - 1)
    assert ab.dirs[SEG].direction == Direction.ODD
    ab.tick_directions(sim.now + SETTINGS["intervals"]["direction_change_s"])
    assert ab.dirs[SEG].direction == Direction.EVEN
