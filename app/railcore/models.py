"""Domain model (CLAUDE.md §8). Fields may be added, never removed."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict


class Direction(StrEnum):
    ODD = "odd"  # km increasing
    EVEN = "even"


class _Static(BaseModel):
    model_config = ConfigDict(frozen=True)


class Track(_Static):
    id: str
    length_m: int
    main: bool
    platform: bool = False


class Station(_Static):
    id: str
    name: str
    kind: Literal["station", "siding"]
    km: float
    tracks: list[Track]
    simultaneous_reception: bool = True  # False: oncoming trains are not received simultaneously (tau_np)


class SpeedZone(_Static):
    from_m: float
    to_m: float
    v_kmh: float  # from segment start (odd direction)


class GradeZone(_Static):
    from_m: float
    to_m: float
    permille: float  # + = uphill in ODD direction


class Segment(_Static):
    id: str
    from_station: str
    to_station: str
    length_m: float
    v_max_kmh: float
    speed_zones: list[SpeedZone] = []
    grade_zones: list[GradeZone] = []


class BlockSection(_Static):
    """Part of a segment between two block signals (automatic block, §9.1). Coordinates from the segment
    start in the odd direction; the blocks of a segment tile it without gaps."""
    id: str
    segment_id: str
    index: int  # 1.. in the odd direction
    from_m: float
    to_m: float


class Signal(_Static):
    id: str
    direction: Direction
    kind: Literal["entry", "exit", "block", "pre_entry"]
    station_id: str | None = None  # entry / exit
    segment_id: str | None = None  # block / pre_entry: on the segment
    pos_m: float | None = None  # block / pre_entry: from the segment start (odd direction)


class Switch(_Static):
    id: str
    station_id: str
    throat: Literal["west", "east"]
    track_id: str


class Infra(_Static):
    stations: list[Station]
    segments: list[Segment]
    blocks: list[BlockSection] = []
    signals: list[Signal]
    switches: list[Switch]


class TrainCategory(_Static):
    id: Literal["express", "passenger", "freight"]
    name: str
    mass_t: float
    length_m: float
    v_max_kmh: float
    power_kw: float
    f_start_kn: float
    b_service: float  # m/s^2, service braking
    davis_a: float
    davis_b: float
    davis_c: float  # w0 = a + b*v + c*v^2, N/kN, v in km/h
    rotating_mass: float = 0.06
    priority_weight: float
    min_dwell_s: int
    color: str


class TimetableStop(BaseModel):
    station_id: str
    arr: float | None  # sim seconds; None at origin
    dep: float | None  # None at destination
    stop: bool  # scheduled commercial/technical stop
    min_dwell_s: int = 0


class Train(BaseModel):
    id: str
    category: str
    direction: Direction
    stops: list[TimetableStop]  # every station on route, in order
    v_max_override_kmh: float | None = None
    priority_override: float | None = None
    cancelled: bool = False


class TrainStatus(StrEnum):
    WAITING = "waiting"
    RUNNING = "running"
    DWELLING = "dwelling"
    STOPPED = "stopped"
    BROKEN = "broken"
    FINISHED = "finished"


class TrainState(BaseModel):
    train_id: str
    status: TrainStatus
    segment_id: str | None
    station_id: str | None
    track_id: str | None
    pos_m: float  # distance from segment start in direction of travel
    km: float
    speed_kmh: float
    regime: Literal["accel", "cruise", "coast", "brake", "stop"]
    delay_s: float  # vs current plan (positive = late)
    next_station_id: str | None
    energy_kwh: float
    block_id: str | None = None  # block section of the train's head (automatic block)
    # MVP additions: what the planner needs to rebuild the "now" snapshot
    on_field: bool = False
    progress: float = 0.0  # 0..1 along current segment
    arrived_at: float | None = None  # when the train reached its current station
    stopped: bool = False  # standing at the current station (not just passing)


class IncidentType(StrEnum):
    TRAIN_FAILURE = "train_failure"
    OBSTACLE = "obstacle"
    SIGNAL_FAILURE = "signal_failure"
    SEGMENT_CLOSED = "segment_closed"
    SPEED_RESTRICTION = "speed_restriction"
    TRAIN_DELAY = "train_delay"
    TRACK_CLOSED = "track_closed"


class Incident(BaseModel):
    id: str
    type: IncidentType
    status: Literal["active", "resolved"]
    segment_id: str | None = None
    station_id: str | None = None
    train_id: str | None = None
    track_id: str | None = None
    km: float | None = None
    started_at: float
    est_min_s: int
    est_max_s: int
    est_expected_s: int  # expected = (min+max)/2 unless given
    params: dict = {}
    description: str
    # actual_s lives ONLY inside field service, never serialized to bus/API


class PlanEntry(BaseModel):
    train_id: str
    kind: Literal["run", "dwell"]
    segment_id: str | None
    station_id: str | None
    track_id: str | None
    start: float
    end: float
    stop: bool = False
    unplanned: bool = False


class Meeting(BaseModel):
    kind: Literal["crossing", "overtake"]
    station_id: str
    time: float
    waiting_train: str
    passing_train: str
    wait_s: float


class KPI(BaseModel):
    total_delay_s: float
    weighted_delay_s: float
    delayed_trains: list[tuple[str, float]]
    unplanned_stops: int
    energy_kwh: float
    energy_ideal_kwh: float
    conflicts: int
    throughput: int
    planned_throughput: int
    arrival_accuracy: float
    robust_total_delay_s: float | None = None  # same order, incidents at max duration


class IndexComponent(BaseModel):
    key: str
    label: str
    score: float
    weight: float
    raw: float
    unit: str


class IndexValue(BaseModel):
    value: float
    category: Literal["norm", "attention", "critical"]
    components: list[IndexComponent]


class Pin(BaseModel):
    """A dispatcher's instruction from the train graph (tasks/02): this train arrives at / departs from this
    station at this time. A hard requirement for every later re-planning until the dispatcher removes it."""

    id: str
    train_id: str
    station_id: str
    kind: Literal["arr", "dep"]
    time: float  # sim seconds
    created_at: float  # sim seconds
    status: Literal["active", "violated", "done", "removed"]
    reason: str | None = None  # why it is violated
    description: str  # «Задержать 2003 отправлением со Степной до 10:42»


class Plan(BaseModel):
    version: int
    base_version: int | None
    created_at: float
    horizon_end: float
    entries: list[PlanEntry]
    meetings: list[Meeting]
    kpi: KPI
    index: IndexValue
    strategy: str | None = None
    solver: Literal["cpsat", "fallback", "refresh"]
    solve_ms: int
    pins: list[Pin] = []  # the dispatcher's instructions this plan keeps (active and violated)


class Variant(BaseModel):
    id: str
    incident_ids: list[str]
    base_plan_version: int
    strategy: str
    title: str
    plan: Plan
    delta_index: float
    delta_delay_s: float
    explanation: list[str]
    status: Literal["proposed", "applied", "stale", "rejected"]
    # step 1: one yardstick for all variants (balanced objective, weighted minutes, lower is better),
    # the recommendation follows it; live cards are re-timed from "now" every second
    score: float | None = None
    recommended: bool = False
    updated_at: float | None = None  # sim time of the last live re-timing
    kind: Literal["incident", "return", "replan", "broken", "manual"] = "incident"
    source: Literal["incident", "manual", "return_to_schedule", "whatif"] = "incident"
    manual: dict | None = None  # {train_id, station_id, kind, from_time, to_time} for source="manual"


class JournalEntry(BaseModel):
    id: str
    time: float  # sim seconds
    kind: Literal["incident_created", "incident_resolved", "variants_proposed", "variant_applied",
                  "variant_rejected", "variants_stale", "return_offered", "no_decision_needed",
                  "decisions_archived", "plan_broken", "decision_hold", "guard_replan", "signal_stop",
                  "pin_set", "pin_removed", "pin_violated", "pin_done", "incident_updated",
                  "settings_updated", "scenario_started"]
    text: str
    incident_ids: list[str] = []
    variant_id: str | None = None
    strategy: str | None = None


class Modification(BaseModel):
    kind: Literal[
        "train_speed", "incident_duration", "train_priority", "departure_shift",
        "add_train", "cancel_train", "segment_speed_limit", "close_segment",
    ]
    target_id: str | None = None
    value: float | None = None
    params: dict = {}


class WhatIfRequest(BaseModel):
    id: str
    base_plan_version: int
    modifications: list[Modification]


class WhatIfResult(BaseModel):
    request_id: str
    plan: Plan
    delta_index: float
    per_train: list[dict]  # {train_id, arr_delta_s, final_delay_s}
    changed_meetings: list[Meeting]
    explanation: list[str]


class ProfilePoint(BaseModel):
    s_m: float
    v_kmh: float
    regime: Literal["accel", "cruise", "coast", "brake"]


class SpeedProfile(BaseModel):
    train_id: str
    plan_version: int
    segment_id: str
    t_target_s: float
    t_profile_s: float
    energy_kwh: float
    energy_min_time_kwh: float
    saving_pct: float
    points: list[ProfilePoint]
    # MVP additions for the chart
    min_points: list[ProfilePoint] = []
    t_min_s: float = 0.0
    v_lim_kmh: float = 0.0
    direction: Direction = Direction.ODD
    km_from: float = 0.0
    km_to: float = 0.0


class Route(BaseModel):
    id: str
    station_id: str
    train_id: str | None
    direction: Direction
    kind: Literal["arrival", "departure", "through"]
    track_id: str
    segment_id: str | None
    status: Literal["set", "occupied", "released"]
    set_by: Literal["auto", "manual"]
