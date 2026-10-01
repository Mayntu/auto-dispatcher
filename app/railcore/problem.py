"""Shared problem description for the CP-SAT model and the fixed-order evaluator.

`Snapshot` (the "now" state) -> list[Task] (per-train chain of station nodes and segment legs,
times in int seconds relative to `now`) -> solution {train_id: [(arr, dep, stop), ...]} -> `Plan`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel

from app.railcore.index import compute_index
from app.railcore.infra import World
from app.railcore.meetings import find_meetings
from app.railcore.models import (
    KPI, Direction, Incident, IncidentType, Pin, Plan, PlanEntry, Train, TrainState, TrainStatus,
)
from app.railcore.warnings import restrictions_by_segment
from app.railcore.running_time import RunningTimes

CLOSING_TYPES = {IncidentType.OBSTACLE, IncidentType.SEGMENT_CLOSED}
OBSTACLE_STOP_M = 200.0  # trains stop this far before an obstacle
MIN_REMAINING_S = 60
INVITATION_EXTRA_S = 180  # leaving on the call-on aspect of a failed exit signal (§11.3)  # an incident past its estimate but still active blocks for at least this long

Solution = dict[str, list[tuple[int, int, bool]]]


class Snapshot(BaseModel):
    now: float
    trains: list[Train]  # effective timetable (what-if overrides applied)
    states: dict[str, TrainState] = {}
    incidents: list[Incident] = []  # active only
    duration_override_s: dict[str, int] = {}  # incident id -> total duration (what-if)
    hint: list[PlanEntry] = []  # current plan, for warm start and fixed order
    pins: list[Pin] = []  # the dispatcher's instructions (active, violated and the one being previewed)


@dataclass
class Node:
    station_id: str
    kind: str  # origin | mid | dest
    sched_arr: int | None
    sched_dep: int | None
    sched_stop: bool
    dwell_min: int  # dep - arr lower bound
    pass_threshold: int  # dwell above this means the train stopped
    arr_fixed: int | None = None
    dep_min: int = 0
    stop_fixed: bool = False
    dest_dwell: int = 0
    capacity: int = 1  # tracks at the station
    tracks: list[str] = field(default_factory=list)  # tracks this train may use here (useful length, platform)
    side_tracks: list[str] = field(default_factory=list)  # of those, the side (non-main) ones
    track_fixed: str | None = None  # the track the train stands on now
    simultaneous_reception: bool = True
    pin_arr: int | None = None  # dispatcher's instruction: arrive exactly then
    pin_dep: int | None = None  # dispatcher's instruction: depart exactly then
    free_arr: bool = False  # preview of a drag: this event ignores the current plan's time as a lower bound
    free_dep: bool = False


@dataclass
class Leg:
    segment_id: str
    t_pp: int
    sup_start: int
    sup_end: int
    e_pp: float
    e_start: float
    max_factor: float = 1.3  # longest run = factor x minimum; an arrival instruction may stretch it
    fixed_run: int | None = None  # preview of a drag: the run keeps exactly this duration (speed unchanged)


@dataclass
class Current:
    segment_id: str
    remaining: int  # minimum time to reach nodes[0], before the stop supplement
    sup_end: int
    e_rest: float
    progress: float = 0.0  # where on the segment (trains running in a packet keep their order)


@dataclass
class Task:
    train_id: str
    category: str
    direction: Direction
    weight: float  # strategy weight (objective)
    base_weight: float  # category weight (KPI)
    nodes: list[Node]
    legs: list[Leg]  # legs[i] goes nodes[i] -> nodes[i+1]
    current: Current | None = None
    meta: dict = field(default_factory=dict)


def incident_remaining(inc: Incident, now: float, durations: dict[str, int]) -> int:
    d = durations.get(inc.id, inc.est_expected_s)
    return max(MIN_REMAINING_S, round(inc.started_at + d - now))


def build_tasks(snap: Snapshot, world: World, rts: RunningTimes, settings: dict,
                durations: dict[str, int] | None = None, weight_mult: dict[str, float] | None = None) -> list[Task]:
    now = snap.now
    durations = {**(durations or {}), **snap.duration_override_s}
    weight_mult = weight_mult or {}
    horizon = settings["planner"]["horizon_s"]
    st_cfg = settings["station"]
    ready, leave, clear_m = st_cfg["ready_before_dep_s"], st_cfg["leave_after_arr_s"], st_cfg["pass_clear_m"]

    def rel(t: float | None) -> int | None:
        return None if t is None else round(t - now)

    closed: dict[str, int] = {}
    obstacles: list[tuple[Incident, int]] = []
    failures: dict[str, int] = {}
    delays: dict[tuple[str, str], int] = {}  # (train, station) -> held there until (relative s)
    sig_fail: dict[tuple[str, str], int] = {}  # (station, direction) -> exit signal failed until (relative s)
    # warnings: slower running times for trains that enter the segment while the warning is expected to last
    warn = restrictions_by_segment(snap.incidents, world)
    warn_until: dict[str, int] = {}
    for inc in snap.incidents:
        if inc.type == IncidentType.SPEED_RESTRICTION and inc.segment_id and inc.status == "active":
            warn_until[inc.segment_id] = max(warn_until.get(inc.segment_id, 0), incident_remaining(inc, now, durations))
    for inc in snap.incidents:
        if inc.status != "active":
            continue
        rem = incident_remaining(inc, now, durations)
        if inc.type in CLOSING_TYPES and inc.segment_id:
            closed[inc.segment_id] = max(closed.get(inc.segment_id, 0), rem)
            if inc.type == IncidentType.OBSTACLE:
                obstacles.append((inc, rem))
        elif inc.type == IncidentType.TRAIN_FAILURE and inc.train_id:
            failures[inc.train_id] = max(failures.get(inc.train_id, 0), rem)
        elif inc.type == IncidentType.TRAIN_DELAY and inc.train_id and inc.station_id:
            delays[(inc.train_id, inc.station_id)] = max(delays.get((inc.train_id, inc.station_id), 0), rem)
        elif inc.type == IncidentType.SIGNAL_FAILURE and inc.station_id:
            sig_fail[(inc.station_id, inc.params.get("direction"))] = max(
                sig_fail.get((inc.station_id, inc.params.get("direction")), 0), rem)

    tasks: list[Task] = []
    for train in snap.trains:
        if train.cancelled:
            continue
        st = snap.states.get(train.id)
        if st is not None and st.status == TrainStatus.FINISHED:
            continue
        route = world.route(train)
        last = len(route) - 1
        cat = world.categories[train.category]
        ov = train.v_max_override_kmh
        if (st is None or not st.on_field) and train.stops[0].dep is not None and train.stops[0].dep - now > horizon:
            continue

        legs_all = []
        for k in range(last):
            seg = world.segment_between(route[k], route[k + 1])
            restr = ()
            if seg.id in warn:
                on_it_now = st is not None and st.on_field and st.segment_id == seg.id
                enters = train.stops[k].dep
                if on_it_now or enters is None or enters - now <= warn_until[seg.id]:
                    restr = warn[seg.id]
            rt = rts.get(cat.id, seg.id, train.direction, ov, restr)
            legs_all.append(Leg(seg.id, round(rt.t_pp), round(rt.sup_start), round(rt.sup_end), rt.e_pp_kwh, rt.e_start_kwh))

        def make_node(k: int) -> Node:
            s = train.stops[k]
            seg_id = legs_all[min(k, last - 1)].segment_id
            t_pass = round(rts.t_pass(cat.id, seg_id, train.direction, clear_m, ov))
            kind = "origin" if k == 0 else "dest" if k == last else "mid"
            station = world.stations[s.station_id]
            # useful length: the train must fit; a passenger stop needs a platform track
            fit = [tr for tr in station.tracks if tr.length_m >= cat.length_m] or list(station.tracks)
            if s.stop and cat.id != "freight" and kind != "origin":
                fit = [tr for tr in fit if tr.platform] or fit
            node = Node(station_id=s.station_id, kind=kind, sched_arr=rel(s.arr), sched_dep=rel(s.dep),
                        sched_stop=s.stop, dwell_min=0, pass_threshold=t_pass, capacity=world.capacity(s.station_id),
                        tracks=[tr.id for tr in fit], side_tracks=[tr.id for tr in fit if not tr.main],
                        simultaneous_reception=station.simultaneous_reception)
            if kind == "mid":
                node.dwell_min = max(s.min_dwell_s, cat.min_dwell_s) if s.stop else t_pass
                node.stop_fixed = s.stop
                # never leave a stop before the timetable — passenger stops and the technical stops the
                # normative timetable keeps for crossings alike: leaving a crossing stop early takes the
                # segment from a train that is still beyond the planning horizon
                if s.stop and node.sched_dep is not None:
                    node.dep_min = max(0, node.sched_dep)
            elif kind == "origin":
                node.stop_fixed = True
                node.dep_min = max(0, node.sched_dep or 0)
            else:
                node.stop_fixed = True
                node.dest_dwell = leave
            if k < last and legs_all[k].segment_id in closed:
                node.dep_min = max(node.dep_min, closed[legs_all[k].segment_id])
            if k < last and (train.id, s.station_id) in delays:  # train_delay: held at this station
                node.dep_min = max(node.dep_min, delays[(train.id, s.station_id)])
                node.stop_fixed = True
            fail_until = sig_fail.get((s.station_id, train.direction.value))
            when = node.sched_dep if node.sched_dep is not None else node.sched_arr
            if k < last and fail_until is not None and (when is None or when <= fail_until):
                # exit signal failed: stop and leave on the call-on aspect (+180 s) while it lasts
                # (decided by the timetable time — a train delayed past the repair is treated as affected too)
                node.stop_fixed = True
                node.dwell_min = max(node.dwell_min, INVITATION_EXTRA_S)
            return node

        current = None
        if st is None or not st.on_field:
            first = 0
            nodes = [make_node(k) for k in range(len(route))]
            nodes[0].arr_fixed = max(0, round(train.stops[0].dep - ready - now))
        elif st.segment_id:
            first = route.index(st.next_station_id)
            nodes = [make_node(k) for k in range(first, len(route))]
            leg = legs_all[first - 1]
            rest = 1.0 - st.progress
            remaining = rest * leg.t_pp
            for inc, rem in obstacles:
                if inc.segment_id == leg.segment_id and inc.km is not None:
                    obs_pos = world.pos_of_km(leg.segment_id, inc.km, train.direction)
                    stop_pos = obs_pos - OBSTACLE_STOP_M
                    if st.pos_m < stop_pos + 1:
                        # while the obstacle is being cleared the train runs up to it and stops; afterwards only
                        # the part beyond the stop point is left (plus restarting from a standstill)
                        seg_len = world.segment(leg.segment_id).length_m
                        after = (1 - max(stop_pos, st.pos_m) / seg_len) * leg.t_pp + leg.sup_start
                        remaining = max(remaining, rem + after)
            remaining += failures.get(train.id, 0)
            current = Current(leg.segment_id, round(remaining), leg.sup_end, rest * leg.e_pp, st.progress)
        else:
            first = route.index(st.station_id)
            nodes = [make_node(k) for k in range(first, len(route))]
            n0 = nodes[0]
            n0.arr_fixed = 0
            n0.track_fixed = st.track_id
            if st.track_id and st.track_id not in n0.tracks:
                n0.tracks.append(st.track_id)
            elapsed = max(0.0, now - (st.arrived_at if st.arrived_at is not None else now))
            if n0.kind == "dest":
                # already arrived: keep the real arrival time (in the past), not "now" — otherwise the
                # train looks later and later while it stands at its terminus
                n0.arr_fixed = -round(elapsed)
                n0.dest_dwell = leave
            elif n0.kind == "mid":
                n0.dep_min = max(n0.dep_min, round(n0.dwell_min - elapsed)) if n0.sched_stop else n0.dep_min
                n0.pass_threshold = max(0, round(n0.pass_threshold - elapsed))
                n0.dwell_min = 0
                n0.stop_fixed = n0.stop_fixed or st.stopped
            if train.id in failures:
                n0.dep_min = max(n0.dep_min, failures[train.id])

        base_w = train.priority_override or settings["priority_weights"][cat.id]
        legs = legs_all[first:]
        apply_pins(train.id, nodes, legs, snap.pins, now, settings)
        tasks.append(Task(
            train_id=train.id, category=cat.id, direction=train.direction,
            weight=base_w * weight_mult.get(cat.id, 1.0), base_weight=base_w,
            nodes=nodes, legs=legs, current=current,
            meta={"final_sched_arr": rel(train.stops[-1].arr)},
        ))
    return tasks


def apply_pins(train_id: str, nodes: list[Node], legs: list[Leg], pins: list[Pin], now: float, settings: dict) -> None:
    """The dispatcher's instructions on this train's future events (tasks/02 §3.4): an arrival instruction may
    stretch the run into that station up to `manual.max_run_factor` x minimum."""
    factor = settings.get("manual", {}).get("max_run_factor", 2.0)
    for pin in pins:
        if pin.train_id != train_id or pin.status not in ("active", "violated") or pin.time < now:
            continue
        for i, n in enumerate(nodes):
            if n.station_id != pin.station_id:
                continue
            t = round(pin.time - now)
            if pin.kind == "dep" and n.kind != "dest":
                n.pin_dep = t
            elif pin.kind == "arr" and n.kind != "origin" and n.arr_fixed is None:
                n.pin_arr = t
                if i > 0:
                    legs[i - 1].max_factor = max(legs[i - 1].max_factor, factor)


def _fill_tracks(entries: list[PlanEntry], tasks: list[Task]) -> None:
    """A plan that did not come from CP-SAT (re-timing, fallback) has no track for trains new to the horizon:
    give each a candidate track (fits, platform) free at that time — the main track first when passing."""
    cands = {(t.train_id, n.station_id): (n.tracks, n.side_tracks) for t in tasks for n in t.nodes}
    busy: dict[tuple[str, str], list[tuple[float, float]]] = {}
    for e in entries:
        if e.kind == "dwell" and e.track_id:
            busy.setdefault((e.station_id, e.track_id), []).append((e.start, e.end))
    for e in sorted((e for e in entries if e.kind == "dwell" and not e.track_id), key=lambda e: e.start):
        tracks, side = cands.get((e.train_id, e.station_id), ([], []))
        order = sorted(tracks, key=lambda tr: (tr in side) != bool(e.stop))  # passing: main; stopping: side
        free = [tr for tr in order
                if all(e.end <= s0 or e.start >= s1 for s0, s1 in busy.get((e.station_id, tr), []))]
        pick = (free or order or [None])[0]
        if pick is not None:
            e.track_id = pick
            busy.setdefault((e.station_id, pick), []).append((e.start, e.end))


def assemble_plan(tasks: list[Task], sol: Solution, snap: Snapshot, world: World, settings: dict, *,
                  solver: str, strategy: str | None, solve_ms: int, version: int = 0,
                  base_version: int | None = None) -> Plan:
    now = snap.now
    horizon = settings["planner"]["horizon_s"]
    window = settings["index"]["refs"]["accuracy_window_s"]
    # station tracks: assigned by CP-SAT, or carried over from the plan being re-timed
    tracks = getattr(sol, "tracks", None) or {(e.train_id, e.station_id): e.track_id
                                               for e in snap.hint if e.kind == "dwell" and e.track_id}
    entries: list[PlanEntry] = []
    total = weighted = 0.0
    delayed: list[tuple[str, float]] = []
    unplanned = throughput = planned = acc_ok = acc_n = 0
    energy = energy_ideal = 0.0

    for task in tasks:
        times = sol[task.train_id]
        if task.current:
            entries.append(PlanEntry(train_id=task.train_id, kind="run", segment_id=task.current.segment_id,
                                     station_id=None, track_id=None, start=now, end=now + times[0][0]))
            energy += task.current.e_rest
            energy_ideal += task.current.e_rest
        for i, (node, (arr, dep, stop)) in enumerate(zip(task.nodes, times)):
            is_unplanned = stop and node.kind == "mid" and not node.sched_stop
            unplanned += is_unplanned
            entries.append(PlanEntry(train_id=task.train_id, kind="dwell", segment_id=None, station_id=node.station_id,
                                     track_id=tracks.get((task.train_id, node.station_id)) or node.track_fixed, start=now + arr, end=now + dep, stop=stop, unplanned=is_unplanned))
            if i < len(task.legs):
                leg = task.legs[i]
                entries.append(PlanEntry(train_id=task.train_id, kind="run", segment_id=leg.segment_id,
                                         station_id=None, track_id=None, start=now + dep, end=now + times[i + 1][0]))
                energy += leg.e_pp + leg.e_start * stop
                energy_ideal += leg.e_pp + leg.e_start * (node.kind == "origin" or node.sched_stop)
            if (task.category != "freight" and node.sched_stop and node.kind != "origin"
                    and node.arr_fixed is None and node.sched_arr is not None):
                acc_n += 1
                acc_ok += abs(arr - node.sched_arr) <= window
        dest_arr = times[-1][0]
        sched_dest = task.meta["final_sched_arr"]
        late = max(0, dest_arr - sched_dest) if sched_dest is not None else 0
        total += late
        weighted += late * task.base_weight
        if late >= 60:
            delayed.append((task.train_id, float(late)))
        throughput += dest_arr <= horizon
        planned += sched_dest is not None and sched_dest <= horizon

    _fill_tracks(entries, tasks)
    pins = [p for p in snap.pins if p.status in ("active", "violated")]
    kpi = KPI(
        total_delay_s=total, weighted_delay_s=weighted, delayed_trains=sorted(delayed, key=lambda x: -x[1]),
        unplanned_stops=unplanned, energy_kwh=round(energy, 1), energy_ideal_kwh=round(energy_ideal, 1),
        conflicts=unplanned, throughput=throughput, planned_throughput=planned,
        arrival_accuracy=acc_ok / acc_n if acc_n else 1.0,
    )
    directions = {t.train_id: t.direction.value for t in tasks}
    base_dwell = {(t.train_id, n.station_id): float(n.dwell_min) for t in tasks for n in t.nodes if n.kind == "mid"}
    return Plan(
        version=version, base_version=base_version, created_at=now, horizon_end=now + horizon,
        entries=entries, meetings=find_meetings(entries, directions, base_dwell), kpi=kpi,
        index=compute_index(kpi, sum(t.base_weight for t in tasks), horizon, settings),
        strategy=strategy, solver=solver, solve_ms=solve_ms, pins=pins,
    )
