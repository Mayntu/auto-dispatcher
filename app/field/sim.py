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
from app.field.incidents import FieldIncident, IncidentError, make_incident
from app.railcore.infra import World
from app.railcore.models import Incident, IncidentType, Plan, PlanEntry, Train, TrainState, TrainStatus
from app.railcore.problem import CLOSING_TYPES, OBSTACLE_STOP_M
from app.railcore.running_time import RunningTimes

log = logging.getLogger("field")
SUBSTEP_S = 1.0


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

    # ---- plan -------------------------------------------------------------------------------
    def set_plan(self, plan: Plan) -> None:
        self.plan = plan
        self._dwell = {(e.train_id, e.station_id): e for e in plan.entries if e.kind == "dwell"}
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
            for tr in self.trains.values():
                ev = self._advance(tr, h)
                if ev:
                    events.append(("field.train_event", {**ev, "train_id": tr.train.id, "time": self.now}))
            self._safety(events)
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
            if t >= appear and self._occupancy(origin, tid) < self.world.capacity(origin):
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
                tr.loc = "done"
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
        if t < dep_plan or t < tr.arrived_at + min_dwell:
            return None
        if self._broken(tid) or self._closed(seg.id) or any(
                o.loc == "segment" and self._seg(o, o.idx).id == seg.id for o in self.trains.values()):
            return None
        nxt = tr.route[k + 1]
        if self._occupancy(nxt, tid) >= self.world.capacity(nxt):
            return None
        my_run = self._run.get((tid, seg.id))
        if my_run is not None:
            for start, other_id in self._seg_order.get(seg.id, []):
                if start >= my_run.start:
                    break
                o = self.trains[other_id]
                if other_id != tid and o.loc != "done" and seg.id not in o.done_segments:
                    return None
        rt = self.rts.get(cat.id, seg.id, tr.train.direction, tr.train.v_max_override_kmh)
        if tr.stopped:
            tr.energy_kwh += rt.e_start_kwh
        tr.loc, tr.progress, tr.held_at_obstacle, tr.track_id = "segment", 0.0, False, None
        return {"kind": "departed", "station_id": st_id}

    def _on_segment(self, tr: SimTrain, t: float, h: float) -> dict | None:
        tid = tr.train.id
        seg = self._seg(tr, tr.idx)
        rt = self.rts.get(tr.train.category, seg.id, tr.train.direction, tr.train.v_max_override_kmh)
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
        run = self._run.get((tid, seg.id))
        rate = 1.0 / rt.t_pp
        if run is not None:
            rate = min((1.0 - tr.progress) / max(run.end - t, 1.0), rate)
        new_p = min(tr.progress + rate * h, cap)
        tr.held_at_obstacle = new_p >= cap - 1e-9 and cap < 1.0
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
            return {"kind": "arrived", "station_id": st_id, "track_id": tr.track_id}
        return None

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
        """MVP aspects: exit is green when the next segment is free and open; entry is green when the
        station has a free track."""
        out = []
        order = self.world.station_order
        on_seg = {self._seg(o, o.idx).id for o in self.trains.values() if o.loc == "segment"}
        for i, st in enumerate(order):
            free_track = self._occupancy(st, "") < self.world.capacity(st)
            for d, nxt in (("odd", i + 1), ("even", i - 1)):
                prv = i - 1 if d == "odd" else i + 1
                if 0 <= prv < len(order):
                    out.append({"id": f"{st}-{d}-entry", "station_id": st, "direction": d, "kind": "entry",
                                "aspect": "green" if free_track else "red"})
                if 0 <= nxt < len(order):
                    seg = self.world.segment_between(st, order[nxt]).id
                    ok = seg not in on_seg and not self._closed(seg)
                    out.append({"id": f"{st}-{d}-exit", "station_id": st, "direction": d, "kind": "exit",
                                "aspect": "green" if ok else "red", "segment_id": seg})
        return out

    def _safety(self, events: list) -> None:
        now_bad: set[str] = set()
        per_seg: dict[str, int] = {}
        for tr in self.trains.values():
            if tr.loc == "segment":
                s = self._seg(tr, tr.idx).id
                per_seg[s] = per_seg.get(s, 0) + 1
        now_bad |= {f"segment:{s}" for s, n in per_seg.items() if n > 1}
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
        return TrainState(**base, status=status, segment_id=seg.id, station_id=None, pos_m=round(pos, 1),
                          km=round(self.world.km_of(seg.id, pos, tr.train.direction), 3),
                          speed_kmh=round(tr.speed_kmh, 1), regime=regime,
                          next_station_id=tr.route[tr.idx + 1], on_field=True)

    def snapshot(self) -> dict:
        occupied = sorted({self._seg(tr, tr.idx).id for tr in self.trains.values() if tr.loc == "segment"})
        return {
            "sim_time": self.now,
            "trains": [self._train_state(tr).model_dump(mode="json") for tr in self.trains.values()],
            "incidents": [fi.incident.model_dump(mode="json") for fi in self.active()],
            "closed_segments": sorted({fi.incident.segment_id for fi in self.active()
                                       if fi.incident.type in CLOSING_TYPES}),
            "occupied_segments": occupied,
            "signals": self.signals(),
            "safety_violations": self.safety_violations,
            "plan_version": self.plan.version if self.plan else None,
        }


class FieldService:
    """Thin async wrapper: ticks the simulator, publishes state, executes `cmd.field.*`."""

    def __init__(self, bus: EventBus, sim: FieldSim, settings: dict):
        self.bus = bus
        self.sim = sim
        self.tick_s = settings["sim"]["tick_s"]
        self.speed = float(settings["sim"]["speed"])
        self.paused = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        await self.bus.subscribe("plan.approved", self._on_plan)
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
                for topic, payload in self.sim.step(self.tick_s * self.speed):
                    await self.bus.publish(topic, payload, source="field", sim_time=self.sim.now)
            await self._publish_state()
            await asyncio.sleep(max(0.0, nxt - time.monotonic()))

    async def _publish_state(self) -> None:
        state = self.sim.snapshot()
        state.update(speed=self.speed, paused=self.paused)
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
            elif cmd == "clock":
                if p.get("paused") is not None:
                    self.paused = bool(p["paused"])
                if p.get("speed"):
                    self.speed = float(p["speed"])
                result.update(paused=self.paused, speed=self.speed)
            else:
                result.update(ok=False, reason=f"unknown command {cmd}")
        except (IncidentError, KeyError, ValueError) as e:
            result.update(ok=False, reason=str(e))
        log.info("dc command %s ok=%s %s", cmd, result["ok"], result.get("reason", ""))
        await self.bus.publish("dc.command_result", result, corr_id=env.corr_id, source="field", sim_time=self.sim.now)
