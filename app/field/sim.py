"""Field simulator (MVP): trains execute the approved plan.

Position inside a `run` interval is interpolated so the train reaches the next station at the
planned time (never faster than the minimum running time). A train leaves a station only when
the plan says so, the segment is free and open, the next station has a free track and every
train planned earlier on that segment has already passed it. No signals/interlocking in MVP.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field

from app.bus.base import EventBus
from app.bus.envelope import Envelope
from app.field.autoblock import SIGNAL_STOP_M, AutoBlock
from app.railcore.warnings import restriction_of, restrictions_by_segment
from app.railcore.warnings import validate as validate_warning
from app.field.incidents import MVP_TYPES, FieldIncident, IncidentError, make_incident
from app.railcore.infra import World
from app.railcore.models import Direction, Incident, IncidentType, Plan, PlanEntry, Train, TrainState, TrainStatus
from app.railcore.problem import CLOSING_TYPES, OBSTACLE_STOP_M
from app.railcore.running_time import RunningTimes

log = logging.getLogger("field")
SUBSTEP_S = 1.0
STALL_OVERRIDE_S = 600.0
YELLOW_KMH = 60.0
MANUAL_DIR_S = 900  # a direction set by the dispatcher holds this long unless a train uses it first
STARVE_OVERRIDE_S = 900.0  # one train held only by a stale plan order this long -> let it go if safe
VISIBLE_AHEAD_S = 3 * 3600
VISIBLE_BEHIND_S = 3 * 3600  # nothing moved this long -> the DC lets a physically possible move go out of plan order


@dataclass
class SimTrain:
    train: Train
    route: list[str]
    loc: str = "none"  # none | station | segment | done
    idx: int = 0  # current station index, or index of the station behind on a segment
    progress: float = 0.0
    arrived_at: float | None = None
    stopped: bool = False
    held_at_obstacle: bool = False
    energy_kwh: float = 0.0
    delay_s: float = 0.0
    speed_kmh: float = 0.0
    track_id: str | None = None
    finished_at: float | None = None
    order_blocked_since: float | None = None  # ready to go, held only by the plan's order
    held_signal: str | None = None  # block signal the train is standing at
    guard_released: str | None = None  # station where a deadlock guard let this train go out of plan
    done_segments: set[str] = field(default_factory=set)


class FieldSim:
    def __init__(self, world: World, rts: RunningTimes, settings: dict, seed: int = 7):
        self.world = world
        self.rts = rts
        self.settings = settings
        self.rng = random.Random(seed)
        self.now = 0.0
        self.trains = {t.id: SimTrain(t, world.route(t)) for t in world.timetable}
        self.incidents: dict[str, FieldIncident] = {}
        self.plan: Plan | None = None
        self._dwell: dict[tuple[str, str], PlanEntry] = {}
        self._run: dict[tuple[str, str], PlanEntry] = {}
        self._seg_order: dict[str, list[tuple[float, str]]] = {}
        self.safety_violations = 0
        self._violating: set[str] = set()
        self._last_progress = 0.0
        self._overdue = False
        self.plan_overrides = 0
        self._guard_events: list[dict] = []
        self.ab = AutoBlock(world, settings)
        self._last_arrival: dict[tuple[str, str], float] = {}
        self._slow_factor: dict[tuple[str, str], float] = {}
        self._warn: dict[str, tuple] = {}
        self.manual_dir: dict[str, tuple[Direction, float]] = {}  # dispatcher's direction command, until
        self._tau_np_breaches: list[str] = []

    # ---- plan -------------------------------------------------------------------------------
    def set_plan(self, plan: Plan) -> None:
        self.plan = plan
        self._dwell = {(e.train_id, e.station_id): e for e in plan.entries if e.kind == "dwell"}
        factor = self.settings.get("manual", {}).get("max_run_factor", 2.0)
        self._slow_factor = {(p.train_id, p.station_id): factor for p in getattr(plan, "pins", [])
                             if p.kind == "arr" and p.status in ("active", "violated")}
        self._run = {(e.train_id, e.segment_id): e for e in plan.entries if e.kind == "run"}
        order: dict[str, list[tuple[float, str]]] = {}
        for e in plan.entries:
            if e.kind == "run":
                order.setdefault(e.segment_id, []).append((e.start, e.train_id))
        self._seg_order = {k: sorted(v) for k, v in order.items()}
        if plan.strategy == "rescue":
            p = self.settings["planner"]
            for fi in self.active():
                if fi.incident.type == IncidentType.TRAIN_FAILURE:
                    fi.actual_s = min(fi.actual_s, max(p["rescue_eta_s"] + p["rescue_haul_s"],
                                                       self.now - fi.incident.started_at + 60))

    # ---- incidents --------------------------------------------------------------------------
    def active(self) -> list[FieldIncident]:
        return [fi for fi in self.incidents.values() if fi.incident.status == "active"]

    def create_incident(self, req: dict) -> Incident:
        try:
            itype = IncidentType(req["type"])
        except ValueError:
            raise IncidentError(f"неизвестный тип сбоя «{req['type']}»") from None
        if itype not in MVP_TYPES:  # the type first: otherwise a missing segment hides the real reason
            raise IncidentError(f"тип сбоя «{itype}» пока не поддерживается")
        seg_id, km = req.get("segment_id"), req.get("km")
        if req["type"] == IncidentType.TRAIN_FAILURE:
            tr = self.trains.get(req.get("train_id") or "")
            if tr is None or tr.loc == "done":
                raise IncidentError("train not found or already finished")
            st = self._train_state(tr)
            seg_id, km = st.segment_id, st.km
        else:
            if seg_id not in self.world.segments:
                raise IncidentError("unknown segment")
            if req["type"] == IncidentType.SPEED_RESTRICTION:
                try:
                    req = {**req, "params": validate_warning(req.get("params") or req, seg_id, self.world)}
                except ValueError as e:
                    raise IncidentError(str(e)) from None
                km = (req["params"]["km_from"] + req["params"]["km_to"]) / 2
            if km is None:
                a, b = self.world.segment_km(seg_id)
                km = (a + b) / 2
        fi = make_incident(req, self.now, seg_id, km, self.rng)
        self.incidents[fi.id] = fi
        return fi.incident

    def resolve_incident(self, incident_id: str) -> Incident | None:
        fi = self.incidents.get(incident_id)
        if fi is None or fi.incident.status != "active":
            return None
        fi.incident = fi.incident.model_copy(update={"status": "resolved"})
        return fi.incident

    def _closed(self, seg_id: str) -> bool:
        return any(fi.incident.type in CLOSING_TYPES and fi.incident.segment_id == seg_id for fi in self.active())

    def _broken(self, train_id: str) -> bool:
        return any(fi.incident.type == IncidentType.TRAIN_FAILURE and fi.incident.train_id == train_id
                   for fi in self.active())

    # ---- simulation -------------------------------------------------------------------------
    def step(self, dt: float) -> list[tuple[str, dict]]:
        events: list[tuple[str, dict]] = []
        end = self.now + dt
        while self.now < end - 1e-9:
            h = min(SUBSTEP_S, end - self.now)
            self.now += h
            for fi in self.active():
                if self.now >= fi.ends_at():
                    events.append(("incident.resolved", self.resolve_incident(fi.id).model_dump(mode="json")))
            self._autoblock()
            self._overdue = False
            for tr in self.trains.values():
                ev = self._advance(tr, h)
                if ev:
                    events.append(("field.train_event", {**ev, "train_id": tr.train.id, "time": self.now}))
            if not self._overdue:  # everybody waits for the plan's times: the field is not stalled
                self._last_progress = self.now
            self._safety(events)
            events += [("field.guard", g) for g in self._guard_events]
            self._guard_events.clear()
        return events

    def _cat(self, tr: SimTrain):
        return self.world.categories[tr.train.category]

    def _seg(self, tr: SimTrain, k: int):
        return self.world.segment_between(tr.route[k], tr.route[k + 1])

    def _occupancy(self, station_id: str, exclude: str) -> int:
        n = 0
        for o in self.trains.values():
            if o.train.id == exclude:
                continue
            if o.loc == "station" and o.route[o.idx] == station_id:
                n += 1
            elif o.loc == "segment" and o.route[o.idx + 1] == station_id:
                n += 1
        return n

    def _advance(self, tr: SimTrain, h: float) -> dict | None:
        t = self.now
        tid = tr.train.id
        if tr.loc == "done":
            return None
        if tr.loc == "none":
            origin = tr.route[0]
            d = self._dwell.get((tid, origin))
            appear = d.start if d else tr.train.stops[0].dep - self.settings["station"]["ready_before_dep_s"]
            if t >= appear and self._occupancy(origin, tid) < self.world.capacity(origin) \
                    and self._direction_ok(tr, origin):
                self._last_progress = t
                tr.loc, tr.idx, tr.arrived_at, tr.stopped = "station", 0, t, True
                tr.track_id = self._pick_track(tr, origin, will_stop=True)
            return None
        if tr.loc == "station":
            return self._at_station(tr, t)
        return self._on_segment(tr, t, h)

    def _at_station(self, tr: SimTrain, t: float) -> dict | None:
        tid, k, last = tr.train.id, tr.idx, len(tr.route) - 1
        st_id = tr.route[k]
        tr.speed_kmh = 0.0 if tr.stopped else tr.speed_kmh
        if k == last:
            if t >= tr.arrived_at + self.settings["station"]["leave_after_arr_s"]:
                self._last_progress = t
                tr.loc, tr.finished_at = "done", t
                return {"kind": "finished", "station_id": st_id}
            return None
        seg = self._seg(tr, k)
        cat = self._cat(tr)
        stop = tr.train.stops[k]
        t_pass = self.rts.t_pass(cat.id, seg.id, tr.train.direction, self.settings["station"]["pass_clear_m"],
                                 tr.train.v_max_override_kmh)
        if not tr.stopped and t - tr.arrived_at > t_pass + 1:
            tr.stopped = True
        dwell = self._dwell.get((tid, st_id))
        dep_plan = dwell.end if dwell else (stop.dep if stop.dep is not None else t)
        tr.delay_s = max(tr.arrived_at - dwell.start, t - dep_plan) if dwell else max(0.0, t - dep_plan)
        min_dwell = (stop.min_dwell_s or cat.min_dwell_s) if stop.stop and k > 0 else (0.0 if k == 0 else t_pass)
        if t < tr.arrived_at + min_dwell:
            return None
        if k == 0 or (stop.stop and cat.id != "freight"):
            if stop.dep is not None and t < stop.dep:  # never before the timetable at origin / passenger stops
                return None
        if self._broken(tid) or self._closed(seg.id):
            return None
        nxt = tr.route[k + 1]
        if self._occupancy(nxt, tid) >= self.world.capacity(nxt) or not self._direction_ok(tr, nxt):
            return None
        order_ok = self._segment_turn(tr, seg.id) and self._my_turn_at(tr, nxt)
        if t >= dep_plan and not order_ok:
            tr.order_blocked_since = tr.order_blocked_since or t
        else:
            tr.order_blocked_since = None
        if t >= dep_plan:
            self._overdue = True  # past its planned departure and still here: counts towards "the field stalled"
        if (t < dep_plan or not order_ok) and tr.guard_released != st_id:  # once released, it stays released
            # waiting for the plan's time and order is right — unless the whole field has stopped, or this
            # train has been held only by the plan's order for long: then the plan no longer matches reality
            # and the DC lets a physically safe move through (counted: it must not hide planning bugs)
            starving = tr.order_blocked_since is not None and t - tr.order_blocked_since >= STARVE_OVERRIDE_S
            if (self.stalled_s < STALL_OVERRIDE_S and not starving) or not self._safe_out_of_order(tr, nxt):
                return None
        if (t < dep_plan or not order_ok) and tr.guard_released != st_id:
            tr.guard_released = st_id  # one release per stop, however long the segment takes to turn round
            self.plan_overrides += 1
            held = t - (tr.order_blocked_since or t)
            log.warning("deadlock guard: %s leaves %s out of plan (field stalled %.0f s, held %.0f s)", tid, st_id,
                        self.stalled_s, held)
            # the automatics would turn the free segment towards the next train of the (stale) plan again and
            # the released train could never leave: hold the direction for it, as a dispatcher's command does
            self.manual_dir[seg.id] = (tr.train.direction, t + MANUAL_DIR_S)
            self._guard_events.append({"train_id": tid, "station_id": st_id, "time": t,
                                       "reason": "stalled" if self.stalled_s >= STALL_OVERRIDE_S else "starving",
                                       "stalled_s": round(self.stalled_s), "held_s": round(held)})
        d = tr.train.direction
        if not self.ab.can_enter(seg.id, d):
            # automatic block: the exit opens only for the established direction and a free first block section.
            # Only a train whose turn it is may ask to turn a free segment round (takes direction_change_s) —
            # otherwise trains on both ends would keep flipping the direction against each other;
            # a direction the dispatcher set by hand is not turned round by the automatics
            if seg.id not in self.manual_dir or self.manual_dir[seg.id][0] == d:
                self.ab.request_direction(seg.id, d, t, self._segment_empty(seg.id))
            return None
        tr.order_blocked_since = None
        tr.guard_released = None
        self._last_progress = t
        self.manual_dir.pop(seg.id, None)  # the dispatcher's direction has been used
        rt = self._rt(tr, seg.id)
        if tr.stopped:
            tr.energy_kwh += rt.e_start_kwh
        tr.loc, tr.progress, tr.held_at_obstacle, tr.track_id = "segment", 0.0, False, None
        return {"kind": "departed", "station_id": st_id}

    def _on_segment(self, tr: SimTrain, t: float, h: float) -> dict | None:
        tid = tr.train.id
        seg = self._seg(tr, tr.idx)
        rt = self._rt(tr, seg.id)
        if self._broken(tid):
            tr.speed_kmh = 0.0
            return None
        cap = 1.0
        for fi in self.active():
            inc = fi.incident
            if inc.type == IncidentType.OBSTACLE and inc.segment_id == seg.id and inc.km is not None:
                stop_pos = self.world.pos_of_km(seg.id, inc.km, tr.train.direction) - OBSTACLE_STOP_M
                if tr.progress * seg.length_m <= stop_pos + 1:
                    cap = min(cap, max(tr.progress, stop_pos / seg.length_m))
        ahead = tr.route[tr.idx + 1]
        if not self.world.stations[ahead].simultaneous_reception:
            # tau_np: a siding receives an oncoming train only tau_np after the previous one arrived;
            # meanwhile the train waits at the entry signal
            opposite = Direction.EVEN if tr.train.direction == Direction.ODD else Direction.ODD
            last = self._last_arrival.get((ahead, opposite.value))
            if last is not None and t - last < self.settings["intervals"]["tau_np_s"]:
                entry = (seg.length_m - SIGNAL_STOP_M) / seg.length_m
                if tr.progress <= entry + 1e-9:
                    cap = min(cap, max(tr.progress, entry))
        head = tr.progress * seg.length_m
        max_head, held_signal, yellow = self.ab.stop_point(seg.id, tid, head, tr.train.direction)
        cap = min(cap, max_head / seg.length_m)
        event = None
        if held_signal and max_head <= head + 1e-6 and tr.held_signal != held_signal:
            tr.held_signal = held_signal
            event = {"kind": "held_at_signal", "station_id": None, "segment_id": seg.id, "signal_id": held_signal,
                     "reason": "встречное направление" if self.ab.dirs[seg.id].direction != tr.train.direction
                     else "блок-участок впереди занят"}
        elif not held_signal or max_head > head + 1e-6:
            tr.held_signal = None
        run = self._run.get((tid, seg.id))
        rate = 1.0 / rt.t_pp
        if run is not None:
            # follow the plan's arrival time, but never crawl slower than the model allows (1.3x the running
            # time + the cost of a stop): a stale plan must not leave a train creeping along the line for hours;
            # if it arrives early it waits at the station, whose track was reserved when it left
            factor = self._slow_factor.get((tid, tr.route[tr.idx + 1]), 1.3)  # an arrival instruction may stretch it
            slowest = 1.0 / (factor * (rt.t_pp + rt.sup_start + rt.sup_end) + rt.sup_start + rt.sup_end)
            rate = min(max((1.0 - tr.progress) / max(run.end - t, 1.0), slowest), rate)
        if yellow:  # yellow ahead: no faster than 60 km/h towards the next signal
            rate = min(rate, YELLOW_KMH / 3.6 / seg.length_m)
        v_warn = self._warning_limit(tr, seg, head, head + rate * h * seg.length_m)
        if v_warn is not None:  # warning: no faster than its speed while any part of the train is in it
            rate = min(rate, v_warn / 3.6 / seg.length_m)
        new_p = min(tr.progress + rate * h, cap)
        if new_p > tr.progress:
            self._last_progress = t
        tr.held_at_obstacle = new_p >= cap - 1e-9 and cap < 1.0
        if new_p <= tr.progress:
            self._overdue = True  # standing on the line
        tr.speed_kmh = (new_p - tr.progress) * seg.length_m / h * 3.6
        tr.energy_kwh += rt.e_pp_kwh * (new_p - tr.progress)
        tr.progress = new_p
        eta = t + (1.0 - new_p) / rate if not tr.held_at_obstacle else t + (1.0 - new_p) / (1.0 / rt.t_pp)
        tr.delay_s = eta - run.end if run is not None else 0.0
        if tr.progress >= 1.0 - 1e-9:
            tr.done_segments.add(seg.id)
            tr.idx += 1
            tr.loc, tr.progress, tr.arrived_at, tr.stopped = "station", 0.0, t, False
            st_id = tr.route[tr.idx]
            dwell = self._dwell.get((tid, st_id))
            will_stop = tr.idx == len(tr.route) - 1 or (dwell.stop if dwell else tr.train.stops[tr.idx].stop)
            tr.track_id = self._pick_track(tr, st_id, will_stop)
            tr.held_signal = None
            if not self.world.stations[st_id].simultaneous_reception:
                other = self._last_arrival.get((st_id, "even" if tr.train.direction == Direction.ODD else "odd"))
                if other is not None and t - other < self.settings["intervals"]["tau_np_s"]:
                    self._tau_np_breaches.append(f"tau_np:{st_id}:{tid}:{t:.0f}")
                self._last_arrival[(st_id, tr.train.direction.value)] = t
            return {"kind": "arrived", "station_id": st_id, "track_id": tr.track_id}
        return event

    # ---- automatic block ----------------------------------------------------------------------
    def _segment_empty(self, seg_id: str) -> bool:
        return not any(o.loc == "segment" and self._seg(o, o.idx).id == seg_id for o in self.trains.values())

    def _autoblock(self) -> None:
        """Occupancy and aspects for this tick, direction changes, and turning free segments round towards
        the next train the plan sends onto them (automatic route setting by the plan, §11.3)."""
        on_seg = []
        for o in self.trains.values():
            if o.loc == "segment":
                seg = self._seg(o, o.idx)
                on_seg.append((o.train.id, seg.id, o.progress * seg.length_m, self._cat(o).length_m, o.train.direction))
        obstacles = []
        for fi in self.active():
            inc = fi.incident
            if inc.type == IncidentType.OBSTACLE and inc.segment_id and inc.km is not None:
                obstacles.append((inc.segment_id, self.world.pos_of_km(inc.segment_id, inc.km, Direction.ODD)))
        closed_entries = {(st, d) for st in self.world.station_order for d in Direction
                          if self._occupancy(st, "") >= self.world.capacity(st)}
        self.ab.tick_directions(self.now)
        self._warn = restrictions_by_segment([fi.incident for fi in self.active()], self.world)
        busy = {seg for _, seg, _, _, _ in on_seg}
        for seg_id, (d, until) in list(self.manual_dir.items()):
            if self.now >= until:
                del self.manual_dir[seg_id]
        for seg_id, order in self._seg_order.items():
            if seg_id in busy or self.ab.dirs[seg_id].changing_to is not None or seg_id in self.manual_dir:
                continue
            nxt = next((self.trains[tid] for _, tid in order if self.trains[tid].loc != "done"
                        and seg_id not in self.trains[tid].done_segments), None)
            if nxt is not None:
                self.ab.request_direction(seg_id, nxt.train.direction, self.now, True)
        self.ab.update(on_seg, obstacles, closed_entries)

    def counters(self) -> dict:
        """Status line (§27.5): on the line / at stations / waiting to depart / arrived this shift."""
        c = {"in_transit": 0, "at_stations": 0, "waiting": 0, "arrived": 0}
        for t in self.trains.values():
            if t.loc == "segment":
                c["in_transit"] += 1
            elif t.loc == "station":
                c["waiting" if t.idx == 0 else "at_stations"] += 1
            elif t.loc == "done":
                c["arrived"] += 1
        return c

    @property
    def stalled_s(self) -> float:
        """How long nothing has moved on the field while some train is out there."""
        if not any(t.loc in ("station", "segment") for t in self.trains.values()):
            return 0.0
        return self.now - self._last_progress

    def _direction_ok(self, tr: SimTrain, station_id: str) -> bool:
        """Single-track deadlock avoidance: a station never fills up with trains of one direction while an
        opposite train still has to pass it — one track stays for the crossing. Any chain of waiting trains
        then unwinds from the termini, where trains finish and free their tracks."""
        d = tr.train.direction
        opposite_needs = False
        same = 0
        for o in self.trains.values():
            if o is tr or o.loc == "done" or station_id not in o.route:
                continue
            k = o.route.index(station_id)
            here = (o.loc == "station" and o.idx == k) or (o.loc == "segment" and o.idx + 1 == k)
            if o.train.direction == d:
                same += here
            elif here or o.loc == "none" or k > o.idx:
                opposite_needs = True
        return not opposite_needs or same + 1 <= self.world.capacity(station_id) - 1

    def _rt(self, tr: SimTrain, seg_id: str):
        return self.rts.get(tr.train.category, seg_id, tr.train.direction, tr.train.v_max_override_kmh,
                            self._warn.get(seg_id, ()))

    def _warning_limit(self, tr: SimTrain, seg, head_from: float, head_to: float) -> float | None:
        """Lowest warning speed over the stretch the train's body covers while its head moves head_from..head_to."""
        v = None
        length = self._cat(tr).length_m
        for r in self._warn.get(seg.id, ()):
            a, b = (r.from_m, r.to_m) if tr.train.direction == Direction.ODD else (seg.length_m - r.to_m, seg.length_m - r.from_m)
            if head_to > a and head_from - length < b:
                v = r.v_kmh if v is None else min(v, r.v_kmh)
        return v

    def set_direction(self, seg_id: str, d: Direction) -> None:
        """Dispatcher's command: turn a segment round. Only a free segment; holds 15 min or until used."""
        if seg_id not in self.world.segments:
            raise IncidentError("unknown segment")
        if not self._segment_empty(seg_id):
            raise IncidentError("перегон занят — сменить направление нельзя")
        st = self.ab.dirs[seg_id]
        if st.direction != d or st.changing_to not in (None, d):
            st.changing_to, st.change_done_at = d, self.now + self.ab.change_s
            if st.direction is None:
                st.direction, st.changing_to = d, None
        self.manual_dir[seg_id] = (d, self.now + MANUAL_DIR_S)

    def _segment_turn(self, tr: SimTrain, seg_id: str) -> bool:
        """Every train planned onto this segment before this one has passed it — or, if it runs the same way,
        has at least entered it: followers run in a packet behind it, spaced by the block signals."""
        my_run = self._run.get((tr.train.id, seg_id))
        if my_run is None:
            return True
        for start, other_id in self._seg_order.get(seg_id, []):
            if start >= my_run.start:
                break
            o = self.trains[other_id]
            if other_id == tr.train.id or o.loc == "done" or seg_id in o.done_segments:
                continue
            on_it = o.loc == "segment" and self._seg(o, o.idx).id == seg_id
            if not (on_it and o.train.direction == tr.train.direction):
                return False
        return True

    def _safe_out_of_order(self, tr: SimTrain, station_id: str) -> bool:
        """An out-of-order move must not create a new deadlock: leave a free track at the next station, or
        be able to roll on through it right away."""
        if self._occupancy(station_id, tr.train.id) + 1 < self.world.capacity(station_id):
            return True
        k = tr.route.index(station_id)
        if k == len(tr.route) - 1:
            return True
        seg = self.world.segment_between(station_id, tr.route[k + 1])
        rollable = self.ab.can_enter(seg.id, tr.train.direction) or self._segment_empty(seg.id)
        return rollable and not self._closed(seg.id) \
            and self._occupancy(tr.route[k + 1], tr.train.id) < self.world.capacity(tr.route[k + 1])

    def _my_turn_at(self, tr: SimTrain, station_id: str) -> bool:
        """Like the DC following the approved plan: keep a track reserved for every train that the plan
        brings to this station earlier and that will still be there when this train arrives. Without the
        reservation two sidings can fill with trains waiting for each other (single-track deadlock)."""
        mine = self._dwell.get((tr.train.id, station_id))
        if mine is None:
            return True
        reserved = 0
        for o in self.trains.values():
            if o is tr or o.loc == "done" or station_id not in o.route:
                continue
            if o.loc != "none" and o.route.index(station_id) <= o.idx + (o.loc == "segment"):
                continue  # already there or heading in: counted by _occupancy
            theirs = self._dwell.get((o.train.id, station_id))
            if theirs is not None and theirs.start < mine.start and theirs.end > mine.start:
                reserved += 1
        return self._occupancy(station_id, tr.train.id) + reserved < self.world.capacity(station_id)

    def _pick_track(self, tr: SimTrain, station_id: str, will_stop: bool) -> str | None:
        """Through trains take the main track; stopping trains take a side track that fits the train
        (passenger stops: a platform track), main track as the last resort."""
        station = self.world.stations[station_id]
        busy = {o.track_id for o in self.trains.values()
                if o is not tr and o.loc == "station" and o.route[o.idx] == station_id}
        free = [t for t in station.tracks if t.id not in busy]
        if not free:
            return None
        cat = self._cat(tr)
        planned = self._dwell.get((tr.train.id, station_id))
        if planned is not None and planned.track_id:  # the DC sets the route to the track the plan assigned
            match = next((t for t in free if t.id == planned.track_id), None)
            if match is not None and match.length_m >= cat.length_m:
                return match.id
        fits = [t for t in free if t.length_m >= cat.length_m] or free
        needs_platform = will_stop and cat.id != "freight" and tr.train.stops[tr.route.index(station_id)].stop

        def rank(t) -> tuple:
            if not will_stop:
                return (not t.main, -t.length_m)
            if needs_platform:
                return (not t.platform, t.main, -t.length_m)
            return (t.main, t.length_m)

        return min(fits, key=rank).id

    def signals(self) -> list[dict]:
        """All signal aspects: entry green when the station has a free track; exit green when the automatic
        block lets this direction onto the segment; block and pre-entry signals from the automatic block."""
        out = []
        order = self.world.station_order
        for i, st in enumerate(order):
            free_track = self._occupancy(st, "") < self.world.capacity(st)
            for d, nxt in ((Direction.ODD, i + 1), (Direction.EVEN, i - 1)):
                prv = i - 1 if d == Direction.ODD else i + 1
                if 0 <= prv < len(order):
                    out.append({"id": f"{st}-{d.value}-entry", "station_id": st, "direction": d.value, "kind": "entry",
                                "aspect": "green" if free_track else "red"})
                if 0 <= nxt < len(order):
                    seg = self.world.segment_between(st, order[nxt]).id
                    ok = self.ab.can_enter(seg, d) and not self._closed(seg)
                    out.append({"id": f"{st}-{d.value}-exit", "station_id": st, "direction": d.value, "kind": "exit",
                                "aspect": "green" if ok else "red", "segment_id": seg})
        for sig in self.world.infra.signals:
            if sig.segment_id:
                out.append({"id": sig.id, "segment_id": sig.segment_id, "direction": sig.direction.value,
                            "kind": sig.kind, "pos_m": sig.pos_m, "aspect": self.ab.aspects.get(sig.id, "green")})
        return out

    def _safety(self, events: list) -> None:
        now_bad: set[str] = set()
        per_block: dict[str, set[str]] = {}
        dirs_on_seg: dict[str, set] = {}
        for tr in self.trains.values():
            if tr.loc != "segment":
                continue
            seg = self._seg(tr, tr.idx)
            for blk in self.ab.blocks_covered(seg.id, tr.progress * seg.length_m, self._cat(tr).length_m,
                                              tr.train.direction):
                per_block.setdefault(blk.id, set()).add(tr.train.id)
            dirs_on_seg.setdefault(seg.id, set()).add(tr.train.direction)
            established = self.ab.dirs[seg.id].direction
            if established is not None and established != tr.train.direction:
                now_bad.add(f"against-direction:{seg.id}:{tr.train.id}")
        now_bad |= {f"block:{b}" for b, ts in per_block.items() if len(ts) > 1}
        for tr in self.trains.values():  # a warning is never exceeded
            if tr.loc == "segment":
                seg = self._seg(tr, tr.idx)
                head = tr.progress * seg.length_m
                v_warn = self._warning_limit(tr, seg, head, head)
                if v_warn is not None and tr.speed_kmh > v_warn + 2:
                    now_bad.add(f"warning:{seg.id}:{tr.train.id}")
        now_bad |= set(self._tau_np_breaches)
        self._tau_np_breaches.clear()
        now_bad |= {f"oncoming:{s}" for s, ds in dirs_on_seg.items() if len(ds) > 1}
        for st in self.world.station_order:
            n = sum(1 for tr in self.trains.values() if tr.loc == "station" and tr.route[tr.idx] == st)
            if n > self.world.capacity(st):
                now_bad.add(f"station:{st}")
        for key in now_bad - self._violating:
            self.safety_violations += 1
            log.critical("safety violation %s at %.0f", key, self.now)
            events.append(("safety.violation", {"resource": key, "time": self.now}))
        self._violating = now_bad

    # ---- state ------------------------------------------------------------------------------
    def _train_state(self, tr: SimTrain) -> TrainState:
        tid = tr.train.id
        cat_id = tr.train.category
        base = dict(train_id=tid, track_id=tr.track_id if tr.loc == "station" else None, energy_kwh=round(tr.energy_kwh, 1), delay_s=round(tr.delay_s),
                    progress=tr.progress, arrived_at=tr.arrived_at, stopped=tr.stopped)
        if tr.loc in ("none", "done"):
            st = self.world.stations[tr.route[0] if tr.loc == "none" else tr.route[-1]]
            return TrainState(**base, status=TrainStatus.WAITING if tr.loc == "none" else TrainStatus.FINISHED,
                              segment_id=None, station_id=None, pos_m=0, km=st.km, speed_kmh=0, regime="stop",
                              next_station_id=tr.route[0] if tr.loc == "none" else None, on_field=False)
        if tr.loc == "station":
            k = tr.idx
            st = self.world.stations[tr.route[k]]
            sched_stop = tr.train.stops[k].stop
            status = (TrainStatus.WAITING if k == 0 else TrainStatus.BROKEN if self._broken(tid)
                      else TrainStatus.DWELLING if sched_stop or k == len(tr.route) - 1
                      else TrainStatus.STOPPED if tr.stopped else TrainStatus.RUNNING)
            return TrainState(**base, status=status, segment_id=None, station_id=st.id, pos_m=0, km=st.km,
                              speed_kmh=0 if tr.stopped else round(tr.speed_kmh, 1),
                              regime="stop" if tr.stopped else "cruise",
                              next_station_id=tr.route[k + 1] if k + 1 < len(tr.route) else None, on_field=True)
        seg = self._seg(tr, tr.idx)
        pos = tr.progress * seg.length_m
        status = (TrainStatus.BROKEN if self._broken(tid) else
                  TrainStatus.STOPPED if tr.held_at_obstacle else TrainStatus.RUNNING)
        regime = "stop" if status != TrainStatus.RUNNING else (
            "accel" if tr.progress < 0.05 else "brake" if tr.progress > 0.95 else "cruise")
        base["block_id"] = self.ab.block_at(seg.id, pos if tr.train.direction == Direction.ODD
                                            else seg.length_m - pos).id
        return TrainState(**base, status=status, segment_id=seg.id, station_id=None, pos_m=round(pos, 1),
                          km=round(self.world.km_of(seg.id, pos, tr.train.direction), 3),
                          speed_kmh=round(tr.speed_kmh, 1), regime=regime,
                          next_station_id=tr.route[tr.idx + 1], on_field=True)

    def _visible(self, tr: SimTrain) -> bool:
        """Continuous traffic (§9.3): publish trains on the line, the next ones (departing within the ГИД
        window, 3 h) and those finished within the last 3 h — not the whole 48-hour timetable."""
        if tr.loc in ("station", "segment"):
            return True
        if tr.loc == "none":
            return tr.train.stops[0].dep <= self.now + VISIBLE_AHEAD_S
        return tr.finished_at is not None and tr.finished_at >= self.now - VISIBLE_BEHIND_S

    def snapshot(self) -> dict:
        occupied = sorted({self._seg(tr, tr.idx).id for tr in self.trains.values() if tr.loc == "segment"})
        return {
            "sim_time": self.now,
            "trains": [self._train_state(tr).model_dump(mode="json") for tr in self.trains.values() if self._visible(tr)],
            "incidents": [fi.incident.model_dump(mode="json") for fi in self.active()],
            "warnings": [{"id": fi.incident.id, "segment_id": fi.incident.segment_id, **fi.incident.params,
                          "from_m": r.from_m, "to_m": r.to_m}
                         for fi in self.active() for r in [restriction_of(fi.incident, self.world)] if r is not None],
            "closed_segments": sorted({fi.incident.segment_id for fi in self.active()
                                       if fi.incident.type in CLOSING_TYPES}),
            "occupied_segments": occupied,
            "signals": self.signals(),
            **{k: v for k, v in self.ab.snapshot().items() if k not in ("block_aspects", "directions")},
            "directions": {seg: {**v, "manual": seg in self.manual_dir}
                           for seg, v in self.ab.snapshot()["directions"].items()},
            "safety_violations": self.safety_violations,
            "plan_overrides": self.plan_overrides,
            "counters": self.counters(),
            "stalled_s": round(self.stalled_s),
            "plan_version": self.plan.version if self.plan else None,
        }


class FieldService:
    """Thin async wrapper: ticks the simulator, publishes state, executes `cmd.field.*`."""

    def __init__(self, bus: EventBus, sim: FieldSim, settings: dict):
        self.bus = bus
        self.sim = sim
        self.tick_s = settings["sim"]["tick_s"]
        self.speed = float(settings["sim"]["speed"])
        self.decision_speed = float(settings["sim"].get("decision_speed", 1))
        self.decision_hold = False  # set by the planner while variants await a decision
        self.paused = False
        self._task: asyncio.Task | None = None

    @property
    def effective_speed(self) -> float:
        return min(self.speed, self.decision_speed) if self.decision_hold else self.speed

    async def start(self) -> None:
        await self.bus.subscribe("plan.approved", self._on_plan)
        await self.bus.subscribe("plan.refreshed", self._on_plan)
        await self.bus.subscribe("cmd.field.*", self._on_cmd)
        await self._publish_state()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _loop(self) -> None:
        nxt = time.monotonic()
        while True:
            nxt += self.tick_s
            if not self.paused:
                for topic, payload in self.sim.step(self.tick_s * self.effective_speed):
                    await self.bus.publish(topic, payload, source="field", sim_time=self.sim.now)
            await self._publish_state()
            await asyncio.sleep(max(0.0, nxt - time.monotonic()))

    async def _publish_state(self) -> None:
        state = self.sim.snapshot()
        state.update(speed=self.speed, paused=self.paused, decision_hold=self.decision_hold,
                     effective_speed=0.0 if self.paused else self.effective_speed)
        await self.bus.publish("field.state", state, source="field", sim_time=self.sim.now)

    async def _on_plan(self, env: Envelope) -> None:
        self.sim.set_plan(Plan.model_validate(env.payload))

    async def _on_cmd(self, env: Envelope) -> None:
        cmd = env.type.rsplit(".", 1)[-1]
        p = env.payload
        result: dict = {"ok": True, "command": cmd}
        try:
            if cmd == "create_incident":
                inc = self.sim.create_incident(p)
                result["incident"] = inc.model_dump(mode="json")
                await self.bus.publish("incident.created", inc, source="field", sim_time=self.sim.now)
            elif cmd == "resolve_incident":
                inc = self.sim.resolve_incident(p["incident_id"])
                if inc is None:
                    result.update(ok=False, reason="incident not found or not active")
                else:
                    result["incident"] = inc.model_dump(mode="json")
                    await self.bus.publish("incident.resolved", inc, source="field", sim_time=self.sim.now)
            elif cmd == "set_direction":
                self.sim.set_direction(p["segment_id"], Direction(p["direction"]))
                result["directions"] = self.sim.ab.snapshot()["directions"]
            elif cmd == "clock":
                if p.get("paused") is not None:
                    self.paused = bool(p["paused"])
                if p.get("speed"):
                    self.speed = float(p["speed"])
                if p.get("decision_hold") is not None:
                    self.decision_hold = bool(p["decision_hold"])
                result.update(paused=self.paused, speed=self.speed, decision_hold=self.decision_hold)
            else:
                result.update(ok=False, reason=f"unknown command {cmd}")
        except (IncidentError, KeyError, ValueError) as e:
            result.update(ok=False, reason=str(e))
        log.info("dc command %s ok=%s %s", cmd, result["ok"], result.get("reason", ""))
        await self.bus.publish("dc.command_result", result, corr_id=env.corr_id, source="field", sim_time=self.sim.now)
