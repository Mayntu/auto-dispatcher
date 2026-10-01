"""Three-aspect two-way automatic block on single-track segments (CLAUDE.md §4, §11.3).

- A block section is occupied by a train whose body (head .. head − length) overlaps it, by an obstacle,
  or by a train standing broken in it.
- Block signal of a direction: red if the block section behind it (in the direction of travel) is occupied
  or the segment is set for the other direction; yellow if the following one is occupied or the station
  entry ahead is closed; green otherwise. The pre-entry signal repeats the entry signal: yellow if it is closed.
- Each segment has an established direction. It changes only when the segment is completely free and takes
  `direction_change_s`; while it changes no exit signal onto the segment opens.

Positions: `pos` is measured from the segment start in the train's direction of travel (as `TrainState.pos_m`);
block data use odd-direction coordinates (`from_m`/`to_m`), converted where needed.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.railcore.infra import World
from app.railcore.models import Direction

SIGNAL_STOP_M = 20.0  # a train held at a signal stops this far before it


@dataclass
class SegDirection:
    direction: Direction | None = None
    changing_to: Direction | None = None
    change_done_at: float = 0.0


def to_odd(seg_len: float, pos: float, direction: Direction) -> float:
    return pos if direction == Direction.ODD else seg_len - pos


class AutoBlock:
    def __init__(self, world: World, settings: dict):
        self.world = world
        self.change_s = settings["intervals"]["direction_change_s"]
        self.blocks: dict[str, list] = {}
        for b in sorted(world.infra.blocks, key=lambda b: (b.segment_id, b.index)):
            self.blocks.setdefault(b.segment_id, []).append(b)
        self.signals: dict[tuple[str, Direction], list] = {}
        for s in world.infra.signals:
            if s.segment_id:
                self.signals.setdefault((s.segment_id, s.direction), []).append(s)
        for key, sigs in self.signals.items():  # in the direction of travel
            sigs.sort(key=lambda s: s.pos_m if key[1] == Direction.ODD else -s.pos_m)
        self.dirs = {seg: SegDirection() for seg in world.segments}
        self.occupied: dict[str, str] = {}  # block id -> occupant ("2003", "obstacle")
        self.aspects: dict[str, str] = {}

    # ---- geometry -----------------------------------------------------------------------------
    def block_at(self, seg_id: str, odd_m: float):
        for b in self.blocks[seg_id]:
            if b.from_m <= odd_m < b.to_m or (odd_m >= b.to_m and b is self.blocks[seg_id][-1]):
                return b
        return self.blocks[seg_id][0]

    def blocks_covered(self, seg_id: str, head: float, length: float, direction: Direction) -> list:
        L = self.world.segments[seg_id].length_m
        a = to_odd(L, max(0.0, head - length), direction)
        b = to_odd(L, head, direction)
        lo, hi = min(a, b), max(a, b)
        return [blk for blk in self.blocks[seg_id] if blk.from_m < hi + 1e-6 and blk.to_m > lo - 1e-6]

    def first_block(self, seg_id: str, direction: Direction):
        bl = self.blocks[seg_id]
        return bl[0] if direction == Direction.ODD else bl[-1]

    # ---- state update -------------------------------------------------------------------------
    def update(self, on_segments: list[tuple[str, str, float, float, Direction]], obstacles: list[tuple[str, float]],
               closed_entries: set[tuple[str, Direction]]) -> None:
        """`on_segments`: (train_id, segment_id, head pos in travel coords, length, direction);
        `obstacles`: (segment_id, odd-coordinate metres); `closed_entries`: (station, direction) with entry red."""
        occ: dict[str, str] = {}
        for tid, seg, head, length, d in on_segments:
            for b in self.blocks_covered(seg, head, length, d):
                occ.setdefault(b.id, tid)
        for seg, odd_m in obstacles:
            occ.setdefault(self.block_at(seg, odd_m).id, "obstacle")
        self.occupied = occ
        self.aspects = {}
        for (seg, d), sigs in self.signals.items():
            ahead_station = self._end_station(seg, d)
            entry_closed = (ahead_station, d) in closed_entries
            opposite = self.dirs[seg].direction not in (None, d) or self.dirs[seg].changing_to not in (None, d)
            for s in sigs:
                nxt = self._blocks_beyond(seg, s.pos_m, d)
                if opposite or (nxt and nxt[0].id in occ):
                    aspect = "red"
                elif s.kind == "pre_entry":
                    aspect = "yellow" if entry_closed else "green"
                elif (len(nxt) > 1 and nxt[1].id in occ) or (len(nxt) == 1 and entry_closed):
                    aspect = "yellow"
                else:
                    aspect = "green"
                self.aspects[s.id] = aspect

    def _end_station(self, seg_id: str, d: Direction) -> str:
        seg = self.world.segments[seg_id]
        return seg.to_station if d == Direction.ODD else seg.from_station

    def _blocks_beyond(self, seg_id: str, odd_pos: float, d: Direction) -> list:
        """Block sections after a boundary at `odd_pos`, in the direction of travel."""
        if d == Direction.ODD:
            return [b for b in self.blocks[seg_id] if b.from_m >= odd_pos - 1e-6]
        return [b for b in reversed(self.blocks[seg_id]) if b.to_m <= odd_pos + 1e-6]

    # ---- what a train may do --------------------------------------------------------------------
    def stop_point(self, seg_id: str, tid: str, head: float, d: Direction) -> tuple[float, str | None, bool]:
        """Furthest head position (travel coords) the train may reach now; the signal it is held at (if any);
        whether the next signal shows yellow (speed restriction)."""
        L = self.world.segments[seg_id].length_m
        yellow = False
        for s in self.signals.get((seg_id, d), []):
            pos = to_odd(L, s.pos_m, d) if d == Direction.ODD else L - s.pos_m  # signal position in travel coords
            if pos <= head + 1e-6:
                continue  # already passed
            aspect = self.aspects.get(s.id, "green")
            beyond = self._blocks_beyond(seg_id, s.pos_m, d)
            held_by_self = beyond and self.occupied.get(beyond[0].id) == tid
            if aspect == "red" and not held_by_self:
                return max(head, pos - SIGNAL_STOP_M), s.id, False
            if not yellow and aspect == "yellow":
                yellow = True
            break  # only the next signal matters for the decision in this tick
        return L, None, yellow

    def can_enter(self, seg_id: str, d: Direction) -> bool:
        st = self.dirs[seg_id]
        return st.direction == d and st.changing_to is None and self.first_block(seg_id, d).id not in self.occupied

    def request_direction(self, seg_id: str, d: Direction, now: float, segment_empty: bool) -> None:
        st = self.dirs[seg_id]
        if st.direction == d and st.changing_to is None:
            return
        if st.direction is None and segment_empty:
            st.direction = d  # never set yet: establish immediately
            return
        if segment_empty and st.changing_to is None:
            st.changing_to, st.change_done_at = d, now + self.change_s

    def tick_directions(self, now: float) -> None:
        for st in self.dirs.values():
            if st.changing_to is not None and now >= st.change_done_at:
                st.direction, st.changing_to = st.changing_to, None

    def snapshot(self) -> dict:
        return {
            "blocks": [{"id": b.id, "occupied_by": None if self.occupied.get(b.id) in (None, "obstacle")
                        else self.occupied[b.id], "obstacle": self.occupied.get(b.id) == "obstacle"}
                       for bl in self.blocks.values() for b in bl],
            "block_aspects": self.aspects,
            "directions": {seg: {"direction": st.direction.value if st.direction else None,
                                 "changing": st.changing_to is not None} for seg, st in self.dirs.items()},
        }
