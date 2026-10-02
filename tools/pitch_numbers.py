"""Numbers for the pitch — measured, never estimated (docs/pitch-numbers.md).

    python -m tools.pitch_numbers --part all          # everything (≈ 40 min on an idle machine)
    python -m tools.pitch_numbers --part speed        # A: variants, manual drag, WS, mass incidents
    python -m tools.pitch_numbers --part demo         # D: the defence scenario with a fixed seed
    python -m tools.pitch_numbers --part facts        # E: stack, model size, timetable, tests, settings

Reliability and effect (B, C) come from tools/stress.py runs with --json (see the report for commands).
Results are merged into docs/pitch-numbers.json; tools/pitch_report.py renders docs/pitch-numbers.md.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import random
import statistics
import subprocess
import sys
import time
from pathlib import Path

import httpx
import websockets

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "pitch-numbers.json"
PY = sys.executable


def pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    return round(v[min(len(v) - 1, max(0, round(q / 100 * (len(v) - 1))))], 1)


def summary(values: list[float]) -> dict:
    return {"n": len(values), "p50": pct(values, 50), "p95": pct(values, 95), "max": round(max(values), 1) if values else None,
            "mean": round(statistics.mean(values), 1) if values else None}


def save(part: str, data: dict) -> None:
    allv = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    allv[part] = data
    OUT.write_text(json.dumps(allv, ensure_ascii=False, indent=1), encoding="utf-8")


# ---- a server of our own --------------------------------------------------------------------------------------
class Server:
    def __init__(self, port: int):
        self.port, self.proc = port, None
        self.base = f"http://127.0.0.1:{port}"

    def __enter__(self):
        env = {**os.environ, "PORT": str(self.port)}
        self.proc = subprocess.Popen([PY, "-c", "from app import all as m; m.main()"], cwd=ROOT, env=env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(60):
            try:
                if httpx.get(self.base + "/health", timeout=1).status_code == 200:
                    return self
            except httpx.HTTPError:
                pass
            time.sleep(1)
        raise RuntimeError("server did not start")

    def __exit__(self, *a):
        self.proc.terminate()
        self.proc.wait(10)


class WsClients:
    """N UI-like WebSocket clients: delivery latency of field.state (receive time - ts_wall of the envelope)."""

    def __init__(self, base: str, n: int):
        self.url = base.replace("http", "ws") + "/ws"
        self.n = n
        self.lat: list[float] = []
        self.events: list[tuple[float, dict]] = []  # (receive wall time, envelope) for non-state messages
        self.stop = False
        self.tasks = []

    async def _one(self, keep_events: bool) -> None:
        async with websockets.connect(self.url, max_size=None) as ws:
            await ws.recv()  # snapshot
            while not self.stop:
                try:
                    m = json.loads(await asyncio.wait_for(ws.recv(), 2))
                except asyncio.TimeoutError:
                    continue
                now = time.time()
                if m.get("type") == "field.state":
                    self.lat.append((now - m["ts_wall"]) * 1000)
                elif keep_events:
                    self.events.append((now, m))

    async def __aenter__(self):
        self.tasks = [asyncio.create_task(self._one(i == 0)) for i in range(self.n)]
        await asyncio.sleep(1.0)
        return self

    async def __aexit__(self, *a):
        self.stop = True
        await asyncio.gather(*self.tasks, return_exceptions=True)


# ---- A. speed ---------------------------------------------------------------------------------------------------
def a1_headless(n: int = 54) -> dict:
    """Incident -> 3 variants with the service's own code path (SolverPool, generate_variants, scoring) on stress seeds;
    the service adds the debounce (planner.debounce_ms) before this."""
    from app.common.config import load_settings
    from app.field.incidents import IncidentError
    from app.field.sim import FieldSim
    from app.planner.pool import SolverPool, forecast_plan, solve_job
    from app.planner.snapshot import build_snapshot
    from app.planner.strategies import pick_strategies
    from app.planner.variants import generate_variants
    from app.railcore.infra import get_world
    from app.railcore.models import Plan
    from app.railcore.running_time import RunningTimes

    world, settings = get_world(), load_settings()
    types = ["obstacle", "train_failure", "segment_closed", "speed_restriction", "train_delay", "signal_failure"]
    rows = []

    async def run():
        pool = SolverPool(settings["planner"]["workers"])
        await pool.warm(settings["planner"]["workers"])
        try:
            for k in range(n):
                seed = 120 + k
                rng = random.Random(seed)
                sim = FieldSim(world, RunningTimes(world), settings, seed=seed)
                snap0 = build_snapshot(sim.snapshot(), world.timetable, [], None)
                plan = Plan.model_validate(solve_job(snap0.model_dump(mode="json"), "balanced", settings)["plan"])
                sim.set_plan(plan)
                sim.step(rng.uniform(900, 9000))
                kind = types[k % len(types)]
                try:
                    inc = sim.create_incident(make_req(rng, sim, world, kind))
                except IncidentError:
                    continue
                snap = build_snapshot(sim.snapshot(), world.timetable, [fi.incident for fi in sim.active()], plan)
                fc = forecast_plan(snap, settings)
                t0 = time.perf_counter()
                vs = await generate_variants(pool, world, snap, settings, 1, fc, pick_strategies(snap.incidents, settings))
                total = (time.perf_counter() - t0) * 1000
                rows.append({"seed": seed, "type": inc.type.value, "total_ms": round(total),
                             "per_strategy": {v.strategy: v.plan.solve_ms for v in vs},
                             "solver": {v.strategy: v.plan.solver for v in vs}})
                print(f"  A1 seed {seed} {inc.type.value:18} {total:6.0f} ms", flush=True)
        finally:
            pool.shutdown()

    asyncio.run(run())
    by_strategy: dict[str, list[float]] = {}
    for r in rows:
        for s, ms in r["per_strategy"].items():
            by_strategy.setdefault(s, []).append(ms)
    by_type: dict[str, list[float]] = {}
    for r in rows:
        by_type.setdefault(r["type"], []).append(r["total_ms"])
    return {"total_ms": summary([r["total_ms"] for r in rows]),
            "by_strategy_solve_ms": {s: summary(v) for s, v in by_strategy.items()},
            "by_type_total_ms": {t: summary(v) for t, v in by_type.items()},
            "fallbacks": sum(1 for r in rows for s in r["solver"].values() if s == "fallback"),
            "debounce_ms": settings["planner"]["debounce_ms"], "rows": rows}


def make_req(rng: random.Random, sim, world, kind: str) -> dict:
    segs = list(world.segments)
    req = {"type": kind, "est_min_min": rng.randint(10, 30)}
    req["est_max_min"] = req["est_min_min"] + rng.randint(0, 20)
    if kind == "train_failure":
        on = [t.train.id for t in sim.trains.values() if t.loc == "segment"]
        req["train_id"] = rng.choice(on) if on else rng.choice([t.id for t in world.timetable])
    elif kind == "train_delay":
        at = [t.train.id for t in sim.trains.values() if t.loc == "station" and t.idx < len(t.route) - 1]
        req["train_id"] = rng.choice(at) if at else rng.choice([t.id for t in world.timetable])
    elif kind == "signal_failure":
        order = world.station_order
        req.update(station_id=order[rng.randrange(1, len(order) - 1)], direction=rng.choice(["odd", "even"]))
    else:
        seg = rng.choice(segs)
        req["segment_id"] = seg
        a, b = world.segment_km(seg)
        if kind == "obstacle":
            req["km"] = round(rng.uniform(a + 0.5, b - 0.5), 2)
        if kind == "speed_restriction":
            k = round(rng.uniform(a + 0.5, b - 3.5), 1)
            req["params"] = {"km_from": k, "km_to": k + 3, "v_kmh": rng.choice([25, 40, 60])}
    return req


async def a1_live(base: str, n: int = 12) -> dict:
    """End to end through the running service: POST /api/incidents -> planner.variants received on the WebSocket."""
    c = httpx.AsyncClient(base_url=base, timeout=30)
    rng = random.Random(7)
    types = ["obstacle", "segment_closed", "speed_restriction", "train_failure", "train_delay", "signal_failure"]
    from app.railcore.infra import get_world

    world = get_world()
    out = []
    async with WsClients(base, 1) as wsc:
        await c.post("/api/sim/clock", json={"speed": 60})
        for k in range(n):
            await asyncio.sleep(4)
            state = (await c.get("/api/state")).json()["field"]
            req = make_req_live(rng, state, world, types[k % len(types)])
            wsc.events.clear()
            t0 = time.time()
            r = await c.post("/api/incidents", json=req)
            if r.status_code != 200:
                continue
            iid = r.json()["id"]
            got = None
            while time.time() - t0 < 15:
                for tw, m in list(wsc.events):
                    if m.get("type") == "planner.variants" and iid in m["payload"].get("incident_ids", []) \
                            and any(v["status"] == "proposed" for v in m["payload"]["variants"]):
                        got = (tw, m)
                        break
                if got:
                    break
                await asyncio.sleep(0.05)
            if got:
                out.append({"type": req["type"], "ms": round((got[0] - t0) * 1000),
                            "solve_ms": got[1]["payload"].get("solve_ms")})
                print(f"  A1 live {req['type']:18} {out[-1]['ms']} ms", flush=True)
            for v in (await c.get("/api/variants")).json():
                if v["status"] == "proposed":
                    await c.post(f"/api/plan/variants/{v['id']}/reject")
            await c.post(f"/api/incidents/{iid}/resolve")
            await asyncio.sleep(2)
            for v in (await c.get("/api/variants")).json():
                if v["status"] == "proposed":
                    await c.post(f"/api/plan/variants/{v['id']}/reject")
    await c.aclose()
    return {"event_to_ws_ms": summary([x["ms"] for x in out]), "rows": out}


def make_req_live(rng, state: dict, world, kind: str) -> dict:
    req = {"type": kind, "est_min_min": rng.randint(10, 30)}
    req["est_max_min"] = req["est_min_min"] + rng.randint(0, 20)
    trains = [t for t in state["trains"] if t["on_field"]]
    if kind == "train_failure":
        on = [t["train_id"] for t in trains if t["segment_id"]]
        if not on:
            kind = req["type"] = "obstacle"
        else:
            req["train_id"] = rng.choice(on)
            return req
    if kind == "train_delay":
        at = [t["train_id"] for t in trains if t["station_id"]]
        if not at:
            kind = req["type"] = "segment_closed"
        else:
            req["train_id"] = rng.choice(at)
            return req
    if kind == "signal_failure":
        order = world.station_order
        req.update(station_id=order[rng.randrange(1, len(order) - 1)], direction=rng.choice(["odd", "even"]))
        return req
    seg = rng.choice(list(world.segments))
    a, b = world.segment_km(seg)
    req["segment_id"] = seg
    if kind == "obstacle":
        req["km"] = round(rng.uniform(a + 0.5, b - 0.5), 2)
    if kind == "speed_restriction":
        k = round(rng.uniform(a + 0.5, b - 3.5), 1)
        req.update(km_from=k, km_to=k + 3, v_kmh=rng.choice([25, 40, 60]))
    return req


async def a2_manual(base: str, n: int = 30) -> dict:
    c = httpx.AsyncClient(base_url=base, timeout=30)
    await c.post("/api/sim/clock", json={"speed": 1})
    rng = random.Random(11)
    tb, tp, tc, sp = [], [], [], []
    for k in range(n):
        st = (await c.get("/api/state")).json()
        plan, now = st["plan"], st["field"]["sim_time"]
        ends = {}
        for e in plan["entries"]:
            if e["kind"] == "dwell":
                ends.setdefault(e["train_id"], []).append(e)
        cands = [e for tid, es in ends.items() for e in es[1:-1] if e["start"] > now + 600]
        if not cands:
            break
        e = rng.choice(cands)
        t0 = time.perf_counter()
        b = await c.get("/api/plan/manual/bounds", params={"train_id": e["train_id"], "station_id": e["station_id"]})
        tb.append((time.perf_counter() - t0) * 1000)
        if b.status_code != 200 or b.json()["dep"] is None or b.json()["dep"]["locked"]:
            continue
        body = {"base_plan_version": plan["version"], "train_id": e["train_id"], "station_id": e["station_id"],
                "kind": "dep", "time": b.json()["dep"]["current"] + rng.choice([120, 300, 600, 900])}
        t0 = time.perf_counter()
        p = await c.post("/api/plan/manual/preview", json=body)
        tp.append((time.perf_counter() - t0) * 1000)
        if p.status_code == 200:
            sp.append(p.json()["compute_ms"])
        if k % 3 == 0:
            t0 = time.perf_counter()
            cm = await c.post("/api/plan/manual/commit", json=body)
            tc.append((time.perf_counter() - t0) * 1000)
            for v in cm.json().get("variants", []) if cm.status_code == 200 else []:
                await c.post(f"/api/plan/variants/{v['id']}/reject")
    await c.aclose()
    return {"bounds_ms": summary(tb), "preview_ms": summary(tp), "preview_server_compute_ms": summary(sp),
            "commit_ms": summary(tc), "note": "client-side wall time over HTTP on localhost"}


async def a3_ws(base: str, seconds: int = 60) -> dict:
    async with WsClients(base, 5) as wsc:
        await asyncio.sleep(seconds)
    return {"clients": 5, "seconds": seconds, "field_state_delivery_ms": summary(wsc.lat),
            "note": "receive time on the client minus ts_wall set when field.state was published (includes bus, "
                    "API, WebSocket send and the client's event loop) — an upper bound of the server-side part"}


async def a4_mass(base: str) -> dict:
    c = httpx.AsyncClient(base_url=base, timeout=30)
    await c.post("/api/sim/clock", json={"speed": 20})
    await asyncio.sleep(5)
    async with WsClients(base, 5) as wsc:
        t0 = time.time()
        r = await c.post("/api/scenarios/mass_incidents/run")
        created, variants = [], []
        while time.time() - t0 < 240:
            created = [(tw, m) for tw, m in wsc.events if m.get("type") == "incident.created"]
            variants = [(tw, m) for tw, m in wsc.events if m.get("type") == "planner.variants"
                        and len(m["payload"].get("incident_ids", [])) >= 8 and m["payload"]["variants"]]
            if len(created) >= 8 and variants:
                break
            await asyncio.sleep(0.5)
        lat_during = list(wsc.lat)
    last_inc = max(tw for tw, _ in created) if created else None
    first_full = min(tw for tw, _ in variants) if variants else None
    # recompute times reported by the planner itself for every batch during the burst
    solves = [m["payload"].get("solve_ms") for tw, m in wsc.events if m.get("type") == "planner.metrics"]
    for v in (await c.get("/api/variants")).json():
        if v["status"] == "proposed":
            await c.post(f"/api/plan/variants/{v['id']}/reject")
    for i in (await c.get("/api/incidents")).json():
        await c.post(f"/api/incidents/{i['id']}/resolve")
    safety = (await c.get("/api/state")).json()["field"]["safety_violations"]
    await c.aclose()
    return {"incidents_created": len(created), "run_status": r.status_code,
            "last_incident_to_variants_with_all_8_ms": round((first_full - last_inc) * 1000) if first_full and last_inc else None,
            "planner_solve_ms_during_burst": summary([s for s in solves if s is not None]),
            "ws_delivery_during_burst_ms": summary(lat_during), "safety_violations": safety,
            "note": "while variants wait for a decision the line runs at ×1 (decision hold), so 8 incidents spread over "
                    "2 simulated minutes take about 2 real minutes"}


def part_speed() -> None:
    res = {"A1_headless": a1_headless()}
    save("speed", res)
    with Server(8091) as s:
        res["A1_live"] = asyncio.run(a1_live(s.base))
    save("speed", res)
    with Server(8092) as s:
        res["A3_ws"] = asyncio.run(a3_ws(s.base))
        res["A2_manual"] = asyncio.run(a2_manual(s.base))
    save("speed", res)
    with Server(8093) as s:
        res["A4_mass"] = asyncio.run(a4_mass(s.base))
    save("speed", res)


# ---- D. the defence scenario ------------------------------------------------------------------------------------
def part_demo() -> None:
    from app.common.config import load_settings
    from app.field.sim import FieldSim
    from app.planner.pool import SolverPool, forecast_plan, solve_job
    from app.planner.snapshot import build_snapshot
    from app.planner.strategies import pick_strategies
    from app.planner.variants import generate_variants
    from app.railcore.eco import train_profile
    from app.railcore.infra import get_world
    from app.railcore.models import Plan
    from app.railcore.running_time import RunningTimes

    world, settings = get_world(), load_settings()
    rts = RunningTimes(world)
    SEED, T_OBSTACLE = 2026, 300
    out: dict = {"seed": SEED, "note": "demo_full is not a scenario file; the §19 sequence is run here with a fixed seed"}

    async def run():
        pool = SolverPool(settings["planner"]["workers"])
        await pool.warm(settings["planner"]["workers"])
        try:
            sim = FieldSim(world, rts, settings, seed=SEED)
            snap0 = build_snapshot(sim.snapshot(), world.timetable, [], None)
            plan = Plan.model_validate(solve_job(snap0.model_dump(mode="json"), "balanced", settings)["plan"])
            sim.set_plan(plan)
            out["index_start"] = plan.index.value
            sim.step(T_OBSTACLE)
            out["obstacle"] = await cards(pool, sim, plan, {"type": "obstacle", "segment_id": "R1-STP", "km": 24.5,
                                                             "est_min_min": 15, "est_max_min": 30, "actual_min": 22})
            plan = out["obstacle"].pop("_applied")
            sim.set_plan(plan)
            for fi in list(sim.active()):
                sim.resolve_incident(fi.id)
            # item 11: which freight to break — on the 8 permille climb (STP-R2, odd) soonest after the obstacle
            sim.step(600)
            cands = []
            for e in plan.entries:
                if e.kind == "run" and e.segment_id == "STP-R2" and world.trains[e.train_id].category == "freight" \
                        and world.trains[e.train_id].direction.value == "odd" and e.start > sim.now:
                    cands.append((e.start, e.train_id))
            cands.sort()
            out["freight_for_failure"] = [{"train_id": t, "enters_STP_R2_at_s": round(s), "after_demo_start_min":
                                          round((s - sim.now) / 60)} for s, t in cands[:3]]
            if cands:
                tid = cands[0][1]
                while sim.now < cands[0][0] + 240:
                    sim.step(30)
                st = sim._train_state(sim.trains[tid])
                out["failure"] = {"train_id": tid, "on": st.segment_id, "km": st.km}
                out["failure"].update(await cards(pool, sim, plan, {"type": "train_failure", "train_id": tid,
                                                                     "est_min_min": 20, "est_max_min": 45, "actual_min": 38}))
                out["failure"].pop("_applied", None)
            # item 11: the train to show in the ATO panel — the largest saving among trains with time reserve
            prof = []
            for tr in sim.trains.values():
                if tr.loc not in ("station", "segment"):
                    continue
                st = sim._train_state(tr)
                p = train_profile(world, rts, plan, st, tr.train, plan.version)
                if p is not None and p.t_target_s > p.t_min_s + 5:
                    prof.append({"train_id": tr.train.id, "segment_id": p.segment_id, "saving_pct": p.saving_pct,
                                 "energy_kwh": p.energy_kwh, "energy_min_time_kwh": p.energy_min_time_kwh,
                                 "t_target_s": p.t_target_s, "t_min_s": p.t_min_s})
            prof.sort(key=lambda x: -x["saving_pct"])
            out["ato_candidates"] = prof[:5]
        finally:
            pool.shutdown()

    async def cards(pool, sim, plan, req):
        inc = sim.create_incident(req)
        snap = build_snapshot(sim.snapshot(), world.timetable, [fi.incident for fi in sim.active()], plan)
        fc = forecast_plan(snap, settings)
        t0 = time.perf_counter()
        vs = await generate_variants(pool, world, snap, settings, plan.version, fc, pick_strategies(snap.incidents, settings))
        ms = round((time.perf_counter() - t0) * 1000)
        res = {"incident": inc.description, "variants_ms": ms, "index_now": fc.index.value if fc else None, "cards": []}
        for v in vs:
            res["cards"].append({
                "title": v.title, "recommended": v.recommended, "delta_index": v.delta_index,
                "index": v.plan.index.value, "total_delay_min": round(v.plan.kpi.total_delay_s / 60),
                "top3": [(t, round(s / 60)) for t, s in v.plan.kpi.delayed_trains[:3]],
                "if_longer_total_delay_min": round(v.plan.kpi.robust_total_delay_s / 60)
                if v.plan.kpi.robust_total_delay_s is not None else None,
                "solve_ms": v.plan.solve_ms, "solver": v.plan.solver, "explanation": v.explanation})
        best = next(v for v in vs if v.recommended)
        res["_applied"] = best.plan
        return res

    asyncio.run(run())
    save("demo", out)


# ---- C8. eco-driving energy ------------------------------------------------------------------------------------
def part_eco() -> None:
    """Every 10 min of an 8.5 h shift (seeds 120..129, no incidents): for every train on the line or at a station
    with a profile ahead, the eco profile (timed to the plan) against the minimum-time profile. Trains with time
    in reserve (t_target > t_min + 5 s) are the ones the ATO can save on."""
    from app.common.config import load_settings
    from app.field.sim import FieldSim
    from app.planner.pool import forecast_plan, solve_job
    from app.planner.snapshot import build_snapshot
    from app.railcore.eco import train_profile
    from app.railcore.infra import get_world
    from app.railcore.models import Plan
    from app.railcore.running_time import RunningTimes

    world, settings = get_world(), load_settings()
    rts = RunningTimes(world)
    seen: dict[tuple, dict] = {}
    for seed in range(120, 130):
        sim = FieldSim(world, rts, settings, seed=seed)
        plan = Plan.model_validate(solve_job(build_snapshot(sim.snapshot(), world.timetable, [], None)
                                             .model_dump(mode="json"), "balanced", settings)["plan"])
        sim.set_plan(plan)
        while sim.now < 8.5 * 3600:
            sim.step(600)
            fc = forecast_plan(build_snapshot(sim.snapshot(), world.timetable, [], plan), settings)
            if fc is not None:
                plan = fc
                sim.set_plan(plan)
            for tr in sim.trains.values():
                if tr.loc not in ("station", "segment"):
                    continue
                p = train_profile(world, rts, plan, sim._train_state(tr), tr.train, plan.version)
                if p is None:
                    continue
                key = (seed, tr.train.id, p.segment_id)  # one profile per train and segment (the first one seen)
                seen.setdefault(key, {"train": tr.train.id, "category": tr.train.category, "segment": p.segment_id,
                                      "eco_kwh": p.energy_kwh, "min_kwh": p.energy_min_time_kwh,
                                      "reserve_s": round(p.t_target_s - p.t_min_s), "saving_pct": p.saving_pct})
    rows = list(seen.values())
    reserve = [r for r in rows if r["reserve_s"] > 5]
    def tot(rs, k):
        return round(sum(r[k] for r in rs), 1)
    save("eco", {
        "profiles": len(rows), "with_reserve": len(reserve),
        "with_reserve_eco_kwh": tot(reserve, "eco_kwh"), "with_reserve_min_kwh": tot(reserve, "min_kwh"),
        "with_reserve_saving_pct": round(100 * (1 - tot(reserve, "eco_kwh") / tot(reserve, "min_kwh")), 1) if reserve else None,
        "all_eco_kwh": tot(rows, "eco_kwh"), "all_min_kwh": tot(rows, "min_kwh"),
        "all_saving_pct": round(100 * (1 - tot(rows, "eco_kwh") / tot(rows, "min_kwh")), 1) if rows else None,
        "saving_pct_per_profile_with_reserve": summary([r["saving_pct"] for r in reserve]),
        "by_category": {c: {"n": len([r for r in reserve if r["category"] == c]),
                            "saving_pct": round(100 * (1 - tot([r for r in reserve if r["category"] == c], "eco_kwh")
                                                       / max(1e-9, tot([r for r in reserve if r["category"] == c], "min_kwh"))), 1)}
                        for c in ("express", "passenger", "freight")},
        "note": "energy of the traction model in railcore/eco.py (simplified dynamics, see expert FAQ)"})


# ---- E. facts ---------------------------------------------------------------------------------------------------
def part_facts() -> None:
    from ortools.sat.python import cp_model

    from app.common.config import load_settings
    from app.field.sim import FieldSim
    from app.planner.pool import solve_job
    from app.planner.snapshot import build_snapshot
    from app.railcore.infra import get_world
    from app.railcore.models import Plan
    from app.railcore.running_time import RunningTimes

    world, settings = get_world(), load_settings()
    sizes = []
    orig = cp_model.CpSolver.Solve

    def solve(self, model, *a, **k):
        proto = model.Proto()
        sizes.append({"variables": len(proto.variables), "constraints": len(proto.constraints)})
        return orig(self, model, *a, **k)

    cp_model.CpSolver.Solve = solve
    trains_in_horizon = []
    try:
        from app.railcore.problem import build_tasks
        sim = FieldSim(world, RunningTimes(world), settings, seed=1)
        plan = None
        for t in range(0, 8 * 3600, 3600):
            if t > sim.now:
                sim.step(t - sim.now)
            snap = build_snapshot(sim.snapshot(), world.timetable, [fi.incident for fi in sim.active()], plan)
            trains_in_horizon.append(len(build_tasks(snap, world, RunningTimes(world), settings)))
            plan = Plan.model_validate(solve_job(snap.model_dump(mode="json"), "balanced", settings)["plan"])
            sim.set_plan(plan)
    finally:
        cp_model.CpSolver.Solve = orig

    machine = {"cpu": subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True).stdout.strip(),
               "cores": os.cpu_count(),
               "ram_gb": round(int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True).stdout) / 2**30),
               "os": f"{platform.system()} {platform.mac_ver()[0]}", "python": platform.python_version()}
    tt = world.timetable
    first_dep = min(t.stops[0].dep for t in tt)
    per_day = sum(1 for t in tt if first_dep <= t.stops[0].dep < first_dep + 86400)
    pkg = json.loads((ROOT / "web" / "package.json").read_text())
    import fastapi
    import ortools
    import pydantic

    tests = subprocess.run([PY, "-m", "pytest", "-q"], cwd=ROOT, capture_output=True, text=True).stdout.strip().splitlines()[-1]
    p = settings["planner"]
    save("facts", {
        "machine": machine,
        "backend": {"python": platform.python_version(), "fastapi": fastapi.__version__, "pydantic": pydantic.__version__,
                    "ortools": ortools.__version__, "server": "uvicorn"},
        "frontend": {"deps": pkg.get("dependencies", {}), "dev": pkg.get("devDependencies", {}),
                     "graph_and_map": "own SVG in React (web/src/components/TrainGraphView.jsx, MapView.jsx), no chart library"},
        "bus_in_demo": "memory (BUS=memory, app.all — every service in one process)",
        "database": "none in the demo: history is a ring buffer in the API (last 20 sim min), journal 2000 entries",
        "services": "field (simulator + DC + safety monitor), planner (CP-SAT in a process pool), api (REST + WS) — "
                    "one process: python -m app.all; RabbitMQ / TimescaleDB / separate containers are not implemented",
        "cpsat": {"time_limit_s": p["time_limit_s"], "pool_workers": p["workers"], "num_workers": p.get("cpsat_workers", 4),
                  "whatif_time_limit_s": p.get("whatif_time_limit_s"), "horizon_s": p["horizon_s"],
                  "warm_start": "evaluate() of the current plan's order as AddHint, random_seed 42"},
        "model_size_balanced_hourly_8h": {"variables": summary([s["variables"] for s in sizes]),
                                          "constraints": summary([s["constraints"] for s in sizes]),
                                          "trains_in_horizon": summary(trains_in_horizon)},
        "timetable": {"trains_total": len(tt), "hours": round((max(t.stops[-1].arr for t in tt) - first_dep) / 3600, 1),
                      "trains_per_day": per_day,
                      "by_category": {c: sum(1 for t in tt if t.category == c and first_dep <= t.stops[0].dep < first_dep + 86400)
                                      for c in ("express", "passenger", "freight")}},
        "infra": {"stations": len(world.infra.stations), "sidings": sum(1 for s in world.infra.stations if s.kind == "siding"),
                  "segments": len(world.infra.segments), "block_sections": len(world.infra.blocks),
                  "signals": len(world.infra.signals), "km": max(s.km for s in world.infra.stations)},
        "tests": tests,
        "index": {"weights": settings["index"]["weights"], "thresholds": settings["index"]["thresholds"],
                  "refs": settings["index"]["refs"], "priority_weights": settings["priority_weights"]},
        "intervals": settings["intervals"],
    })


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--part", default="all", choices=["all", "speed", "demo", "facts", "eco", "timed", "untimed"])
    a = ap.parse_args()
    for name, fn in (("facts", part_facts), ("demo", part_demo), ("speed", part_speed), ("eco", part_eco)):
        timed = name in ("demo", "speed")  # wall-clock measurements: only on an idle machine
        if a.part in ("all", name) or (a.part == "timed" and timed) or (a.part == "untimed" and not timed):
            print(f"== {name}", flush=True)
            fn()


if __name__ == "__main__":
    main()
