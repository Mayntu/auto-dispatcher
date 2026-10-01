"""Crossings and overtakes derived from a plan (CLAUDE.md §12.9)."""

from __future__ import annotations

from collections import defaultdict

from app.railcore.models import Meeting, PlanEntry

MIN_WAIT_S = 30.0


def find_meetings(entries: list[PlanEntry], directions: dict[str, str], base_dwell: dict[tuple[str, str], float]) -> list[Meeting]:
    """`base_dwell[(train, station)]` — the dwell the train needs anyway (scheduled stop or passing time)."""
    by_station: dict[str, list[PlanEntry]] = defaultdict(list)
    for e in entries:
        if e.kind == "dwell" and (e.train_id, e.station_id) in base_dwell:  # intermediate stations only
            by_station[e.station_id].append(e)

    out: list[Meeting] = []
    for st, dwells in by_station.items():
        for i, a in enumerate(dwells):
            for b in dwells[i + 1:]:
                if a.start >= b.end or b.start >= a.end:
                    continue
                wa = (a.end - a.start) - base_dwell.get((a.train_id, st), 0.0)
                wb = (b.end - b.start) - base_dwell.get((b.train_id, st), 0.0)
                waiting, passing, wait = (a, b, wa) if wa >= wb else (b, a, wb)
                if wait < MIN_WAIT_S:
                    continue
                if directions[a.train_id] != directions[b.train_id]:
                    kind = "crossing"
                elif (a.start < b.start) != (a.end < b.end):
                    kind = "overtake"
                else:
                    continue
                out.append(Meeting(kind=kind, station_id=st, time=passing.end, waiting_train=waiting.train_id,
                                   passing_train=passing.train_id, wait_s=round(wait)))
    return sorted(out, key=lambda m: m.time)
