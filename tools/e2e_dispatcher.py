"""Live end-to-end check through the real server, acting as a dispatcher.

Starts nothing: run `python -m app.all` first (fresh, sim time ~0). Then:

    python -m tools.e2e_dispatcher [--speed 60]

A monitor watches /api/state every second (safety, stalls, forecast growth, promise vs reality) and a WS
client measures delivery latency, while a scripted dispatcher creates incidents, applies variants (right
away, late, the worst one), runs what-if, asks for ATO profiles and also does wrong things on purpose
(stale apply, unknown ids, bad payloads). Prints a PASS/FAIL report.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time

import httpx
import websockets

BASE = "http://127.0.0.1:8000"


def set_base(url: str) -> None:
    global BASE
    BASE = url.rstrip("/")


class Report:
    def __init__(self) -> None:
        self.fails: list[str] = []
        self.notes: list[str] = []

    def check(self, ok: bool, what: str) -> bool:
        (self.notes if ok else self.fails).append(("ok   " if ok else "FAIL ") + what)
        print(("  ok   " if ok else "  FAIL ") + what, flush=True)
        return ok


R = Report()


class Monitor:
    def __init__(self, c: httpx.AsyncClient):
        self.c = c
        self.state: dict | None = None
        self.stop = False
        self.max_viol = 0
        self.last_move_t = 0.0
        self.last_pos = None
        self.stall = False
        self.quiet_since = None
        self.quiet_delay = None
        self.max_quiet_growth = 0.0
        self.http_5xx = 0

    async def run(self) -> None:
        while not self.stop:
            try:
                t0 = time.time()
                r = await self.c.get("/api/state")
                if time.time() - t0 > 2:
                    print(f"  ! /api/state answered in {time.time() - t0:.1f} s", flush=True)
                if r.status_code >= 500:
                    self.http_5xx += 1
                s = r.json()
                self.state = s
                f = s["field"]
                self.max_viol = max(self.max_viol, f["safety_violations"])
                pos = tuple(round(t["km"], 2) for t in f["trains"])
                on_field = any(t["on_field"] for t in f["trains"])
                if pos != self.last_pos:
                    self.last_pos, self.last_move_t = pos, f["sim_time"]
                elif on_field and not f["incidents"] and not f["paused"] and f["sim_time"] - self.last_move_t > 1800:
                    self.stall = True
                if s["index"] and not f["incidents"]:
                    # forecast delay of the SAME trains (trains entering the horizon bring their own delay)
                    d = dict(s["index"]["kpi"]["delayed_trains"])
                    if self.quiet_since is None:
                        self.quiet_since, self.quiet_delay = f["sim_time"], d
                    elif f["sim_time"] - self.quiet_since >= 1800:
                        common = set(d) & set(self.quiet_delay)
                        growth = sum(d[t] - self.quiet_delay[t] for t in common) / 60
                        self.max_quiet_growth = max(self.max_quiet_growth, growth)
                        self.quiet_since, self.quiet_delay = f["sim_time"], d
                elif f["incidents"]:
                    self.quiet_since = None
            except Exception as e:  # noqa: BLE001
                print("monitor:", e)
            await asyncio.sleep(1.0)

    @property
    def now(self) -> float:
        return self.state["field"]["sim_time"] if self.state else 0.0


async def ws_probe(stats: dict) -> None:
    """A UI-like WS client: reconnects on failure (the server may still be starting), counts messages and
    measures delivery latency of field.state."""
    while not stats.get("stop"):
        try:
            async with websockets.connect(BASE.replace("http", "ws") + "/ws", max_size=None) as ws:
                first = json.loads(await ws.recv())
                stats["snapshot"] = stats.get("snapshot", True) and first.get("type") == "snapshot"
                while not stats.get("stop"):
                    m = json.loads(await asyncio.wait_for(ws.recv(), 10))
                    stats["msgs"] = stats.get("msgs", 0) + 1
                    if m.get("type") == "field.state":
                        stats.setdefault("lat", []).append((time.time() - m["ts_wall"]) * 1000)
        except Exception as e:  # noqa: BLE001
            stats["reconnects"] = stats.get("reconnects", 0) + 1
            stats["last_error"] = repr(e)
            await asyncio.sleep(1)


async def wait_sim(mon: Monitor, t: float) -> None:
    """Wait for a sim time; pending decisions that pop up meanwhile are declined (keep the current plan),
    otherwise the line would stay at ×1."""
    while mon.now < t:
        for v in (await mon.c.get("/api/variants")).json():
            if v["status"] == "proposed":
                await mon.c.post(f"/api/plan/variants/{v['id']}/reject")
        await asyncio.sleep(0.5)


async def wait_variants(c: httpx.AsyncClient, after_version: int | None, incident_id: str | None, timeout=8.0,
                        exclude: set | None = None):
    """Proposed variants for the current plan version (and the incident, if given); returns (variants, seconds)."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        vs = (await c.get("/api/variants")).json()
        live = [v for v in vs if v["status"] == "proposed" and v["id"] not in (exclude or set())]
        if live and (incident_id is None or incident_id in live[0]["incident_ids"]) \
                and (after_version is None or live[0]["base_plan_version"] >= after_version):
            return live, time.time() - t0
        await asyncio.sleep(0.1)
    return [], time.time() - t0


STALE_SEEN: set = set()
SCHED_ARR: dict = {}


async def apply(c: httpx.AsyncClient, mon: Monitor, v: dict, label: str, check_promise: bool) -> dict | None:
    plan = (await c.get("/api/plan")).json()
    r = await c.post("/api/plan/apply", json={"variant_id": v["id"], "base_plan_version": plan["version"]})
    if r.status_code == 409:
        detail = r.json()["detail"]
        STALE_SEEN.update(x["id"] for x in (await c.get("/api/variants")).json())
        if not check_promise:
            R.check(True, f"{label}: устаревший вариант корректно отклонён (409): {detail}")
            return None
        # at x60 two real seconds are two minutes on the line: a variant may legitimately go stale.
        # Then the system must re-plan by itself and the fresh variant must apply.
        R.check("пересчитываю" in detail, f"{label}: отклонён как устаревший, система пересчитывает: {detail}")
        vs, dt = await wait_variants(c, plan["version"], None, exclude={v["id"]} | set(STALE_SEEN))
        if not R.check(bool(vs), f"{label}: свежие варианты пришли за {dt:.1f} с"):
            return None
        fresh = max(vs, key=lambda x: x["plan"]["index"]["value"])
        r = await c.post("/api/plan/apply", json={"variant_id": fresh["id"], "base_plan_version": plan["version"]})
        if not R.check(r.status_code == 200, f"{label}: свежий вариант применён -> {r.status_code}"):
            return None
        v = fresh
    if not R.check(r.status_code == 200, f"{label}: применение -> {r.status_code}"):
        return None
    body = r.json()
    await asyncio.sleep(3)
    idx_now = mon.state["index"]["index"]["value"]
    if check_promise:
        R.check(abs(idx_now - body["index"]) <= 6,
                f"{label}: обещано {v['plan']['index']['value']:.0f}, после пересчёта {body['index']:.0f}, "
                f"прогноз через 3 с {idx_now:.0f}")
    return body


async def scenario(c: httpx.AsyncClient, mon: Monitor, speed: float) -> None:
    print("\n# 0. старт")
    R.check((await c.get("/health")).status_code == 200, "health")
    R.check((await c.get("/docs")).status_code == 200, "Swagger /docs")
    infra = (await c.get("/api/infra")).json()
    R.check(len(infra["timetable"]) >= 100 and len(infra["infra"]["stations"]) == 6,
            f"infra: 6 пунктов, непрерывное движение — {len(infra['timetable'])} поездов на 48 ч")
    SCHED_ARR.update({t["id"]: t["stops"][-1]["arr"] for t in infra["timetable"]})
    plan = (await c.get("/api/plan")).json()
    R.check(plan["solver"] in ("cpsat", "refresh") and plan["index"]["value"] >= 95, f"план v{plan['version']}, индекс {plan['index']['value']}")
    await c.post("/api/sim/clock", json={"speed": speed, "paused": False})

    print("\n# 1. ошибки диспетчера и плохие запросы")
    bad = [
        ("POST", "/api/incidents", {"type": "obstacle", "segment_id": "NOPE", "est_min_min": 5, "est_max_min": 10}, 422),
        ("POST", "/api/incidents", {"type": "train_failure", "train_id": "9999", "est_min_min": 5, "est_max_min": 10}, 422),
        ("POST", "/api/incidents", {"type": "obstacle", "segment_id": "SEV-R1", "est_min_min": 30, "est_max_min": 10}, 422),
        ("POST", "/api/incidents", {"type": "signal_failure", "station_id": "STP", "est_min_min": 5, "est_max_min": 10}, 422),
        ("POST", "/api/incidents", {"type": "obstacle"}, 422),
        ("POST", "/api/incidents/nope/resolve", None, 404),
        ("POST", "/api/plan/apply", {"variant_id": "nope", "base_plan_version": 1}, 404),
        ("POST", "/api/whatif", {"modifications": [{"kind": "add_train"}]}, 422),
        ("POST", "/api/whatif", {"modifications": [{"kind": "train_speed", "target_id": "9999", "value": 60}]}, 422),
        ("GET", "/api/ato/9999", None, 404),
    ]
    for method, url, body, want in bad:
        r = await c.request(method, url, json=body)
        R.check(r.status_code == want, f"{method} {url} {json.dumps(body, ensure_ascii=False)[:70]} -> {r.status_code} (ждали {want})")

    print("\n# 2. what-if и автоведение в штатном режиме")
    t0 = time.time()
    r = await c.post("/api/whatif", json={"modifications": [{"kind": "train_speed", "target_id": "2003", "value": 60}]})
    R.check(r.status_code == 200 and time.time() - t0 <= 3.5, f"what-if 2003→60 км/ч: {r.status_code}, {time.time() - t0:.1f} с, Δ {r.json().get('delta_index')}")
    R.check((await c.get("/api/plan")).json()["version"] == plan["version"], "what-if не меняет действующий план")
    visible = [t["train_id"] for t in mon.state["field"]["trains"]]
    codes = [(await c.get(f"/api/ato/{tid}")).status_code for tid in visible]
    R.check(all(code in (200, 404) for code in codes), f"ATO по всем поездам: {codes}")

    print("\n# 3. скот на перегоне — применяем лучший сразу")
    await wait_sim(mon, 2700)
    v0 = (await c.get("/api/plan")).json()["version"]
    t0 = time.time()
    inc = (await c.post("/api/incidents", json={"type": "obstacle", "segment_id": "R1-STP", "km": 24.5,
                                                 "est_min_min": 15, "est_max_min": 30})).json()
    vs, dt = await wait_variants(c, v0, inc["id"])
    R.check(len(vs) == 3 and dt <= 5, f"3 варианта за {dt:.1f} с (≤ 5)")
    R.check(all(v["explanation"] for v in vs), "у каждого варианта есть объяснение")
    R.check(sum(v["recommended"] for v in vs) == 1, "ровно один рекомендуемый вариант")
    await asyncio.sleep(1.2)
    f = mon.state["field"]
    R.check(f["decision_hold"] and f["effective_speed"] == 1, f"пока ждём решения, время идёт ×1 (сейчас ×{f['effective_speed']})")
    best = max(vs, key=lambda v: v["plan"]["index"]["value"])
    await apply(c, mon, best, "скот/лучший", check_promise=True)
    other = next(v for v in vs if v["id"] != best["id"])
    r = await c.post("/api/plan/apply", json={"variant_id": other["id"], "base_plan_version": v0})
    R.check(r.status_code in (404, 409), f"применение другого варианта старой версии -> {r.status_code} (404/409)")
    r = await c.post("/api/incidents/" + inc["id"] + "/resolve")
    R.check(r.status_code == 200, "сбой снят диспетчером досрочно")
    r = await c.post("/api/incidents/" + inc["id"] + "/resolve")
    R.check(r.status_code == 404, f"повторное снятие -> {r.status_code} (404)")
    await asyncio.sleep(4)
    vs = [v for v in (await c.get("/api/variants")).json() if v["status"] == "proposed"]
    if vs:
        R.check(all(v["title"] == "Возврат к графику" for v in vs), "после снятия предложен «Возврат к графику»")
        await apply(c, mon, vs[0], "возврат к графику", check_promise=True)
    else:
        R.check(True, "после снятия: решений не требуется (план уточнён по времени)")
    await asyncio.sleep(2)
    R.check((await c.get("/api/variants")).json() == [], "панель решений пуста: «Активных решений нет»")
    kinds = [e["kind"] for e in (await c.get("/api/journal")).json()]
    R.check("variant_applied" in kinds and "incident_resolved" in kinds, "решение и снятие сбоя записаны в журнал")

    print("\n# 4. поломка 2003 в пути — вспомогательный локомотив")
    await wait_sim(mon, 4500)
    st = next(t for t in mon.state["field"]["trains"] if t["train_id"] == "2003")
    v0 = (await c.get("/api/plan")).json()["version"]
    r = await c.post("/api/incidents", json={"type": "train_failure", "train_id": "2003", "est_min_min": 20, "est_max_min": 45})
    if R.check(r.status_code == 200, f"поломка 2003 ({st['status']}, {st['segment_id'] or st['station_id']})"):
        inc = r.json()
        vs, dt = await wait_variants(c, v0, inc["id"])
        R.check(len(vs) == 3 and dt <= 5, f"3 варианта за {dt:.1f} с; стратегии {[v['strategy'] for v in vs]}")
        rescue = next((v for v in vs if v["strategy"] == "rescue"), None)
        R.check(rescue is not None, "есть вариант «Вспомогательный локомотив»")
        if vs:
            await apply(c, mon, rescue or vs[0], "поломка/локомотив", check_promise=True)

    print("\n# 5. закрытие перегона — применяем ХУДШИЙ вариант и с опозданием 10 мин")
    await wait_sim(mon, 6000)
    v0 = (await c.get("/api/plan")).json()["version"]
    r = await c.post("/api/incidents", json={"type": "segment_closed", "segment_id": "R2-OZR", "est_min_min": 30, "est_max_min": 30})
    inc = r.json()
    vs, dt = await wait_variants(c, v0, inc["id"])
    R.check(len(vs) == 3 and dt <= 5, f"3 варианта за {dt:.1f} с")
    await asyncio.sleep(15)  # a slow dispatcher; the line runs at ×1 meanwhile
    worst = min(vs, key=lambda v: v["plan"]["index"]["value"]) if vs else None
    if worst:
        await apply(c, mon, worst, "закрытие/худший/поздно", check_promise=False)
    r = await c.post("/api/whatif", json={"modifications": [{"kind": "incident_duration", "target_id": inc["id"], "value": 60}]})
    R.check(r.status_code == 200, f"what-if «закрытие продлится 60 мин»: Δ {r.json().get('delta_index')}")

    print("\n# 6. массовые сбои: 8 штук за 2 минуты")
    await wait_sim(mon, 8400)
    v0 = (await c.get("/api/plan")).json()["version"]
    reqs = [
        {"type": "obstacle", "segment_id": "SEV-R1", "km": 7, "est_min_min": 10, "est_max_min": 20},
        {"type": "obstacle", "segment_id": "OZR-YUZ", "km": 75, "est_min_min": 10, "est_max_min": 25},
        {"type": "segment_closed", "segment_id": "STP-R2", "est_min_min": 15, "est_max_min": 15},
        {"type": "train_failure", "train_id": "2004", "est_min_min": 10, "est_max_min": 30},
        {"type": "train_failure", "train_id": "2005", "est_min_min": 10, "est_max_min": 30},
        {"type": "obstacle", "segment_id": "R1-STP", "km": 20, "est_min_min": 5, "est_max_min": 15},
        {"type": "segment_closed", "segment_id": "R2-OZR", "est_min_min": 10, "est_max_min": 20},
        {"type": "train_failure", "train_id": "2006", "est_min_min": 5, "est_max_min": 15},
    ]
    created = []
    for q in reqs:
        r = await c.post("/api/incidents", json=q)
        if r.status_code == 200:
            created.append(r.json()["id"])
        await asyncio.sleep(0.2)
    R.check(len(created) >= 6, f"создано {len(created)} сбоев из 8")
    vs, dt = await wait_variants(c, v0, created[-1] if created else None, timeout=10)
    R.check(len(vs) == 3 and dt <= 5, f"варианты по всем сбоям за {dt:.1f} с после последнего")
    if vs:
        await apply(c, mon, max(vs, key=lambda v: v["plan"]["index"]["value"]), "массовые/лучший", check_promise=True)

    print("\n# 7. пауза/скорость и ручной пересчёт без сбоев")
    await c.post("/api/sim/clock", json={"paused": True})
    await asyncio.sleep(1.5)  # let the monitor see the paused state first
    t = mon.now
    await asyncio.sleep(2.5)
    R.check(abs(mon.now - t) < 1, "пауза останавливает время")
    await c.post("/api/sim/clock", json={"paused": False, "speed": speed})
    while mon.state["field"]["incidents"]:
        for v in (await c.get("/api/variants")).json():  # keep the current plan, so the line is not held at ×1
            if v["status"] == "proposed":
                await c.post(f"/api/plan/variants/{v['id']}/reject")
        await asyncio.sleep(1)
    await wait_sim(mon, mon.now + 300)
    v0 = (await c.get("/api/plan")).json()["version"]
    r = await c.post("/api/plan/replan")
    R.check(r.status_code == 200, "ручной «Пересчитать план»")
    vs, dt = await wait_variants(c, v0, None)
    R.check(len(vs) == 3, f"варианты пересчёта за {dt:.1f} с")
    if vs:
        await apply(c, mon, max(vs, key=lambda v: v["plan"]["index"]["value"]), "пересчёт/лучший", check_promise=True)

    print("\n# 8. движение продолжается: доводим до 15:30 по часам симуляции")
    while mon.now < 7.5 * 3600 and not mon.stall:
        for v in (await c.get("/api/variants")).json():
            if v["status"] == "proposed":  # nobody at the desk: decline, so the line goes back to ×60
                await c.post(f"/api/plan/variants/{v['id']}/reject")
        await asyncio.sleep(2)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--speed", type=float, default=60)
    ap.add_argument("--base", default=BASE, help="server URL, e.g. http://127.0.0.1:8010")
    args = ap.parse_args()
    set_base(args.base)
    transport = httpx.AsyncHTTPTransport(retries=2)
    async with httpx.AsyncClient(base_url=BASE, timeout=20, transport=transport) as c:
        mon = Monitor(c)
        mt = asyncio.create_task(mon.run())
        ws_stats: dict = {}
        wt = asyncio.create_task(ws_probe(ws_stats))
        await asyncio.sleep(2)
        t0 = time.time()
        try:
            await scenario(c, mon, args.speed)
        finally:
            mon.stop, ws_stats["stop"] = True, True
            await asyncio.sleep(1.2)
            mt.cancel()
            wt.cancel()
        f = mon.state["field"]
        due_stuck = [t["train_id"] for t in f["trains"]
                     if t["status"] != "finished" and SCHED_ARR.get(t["train_id"], 1e9) <= f["sim_time"] - 2 * 3600]
        print("\n# итог")
        R.check(mon.max_viol == 0, f"нарушений безопасности: {mon.max_viol}")
        R.check(not mon.stall, "нет остановки движения (блокировки) без сбоев")
        R.check(not due_stuck, f"все поезда, которым по графику пора было прибыть (2+ ч назад), прибыли {due_stuck or ''}")
        R.check(f["counters"]["in_transit"] + f["counters"]["at_stations"] > 0, f"движение продолжается: {f['counters']}")
        R.check(len(f.get("blocks", [])) == 33 and len(f.get("directions", {})) == 5
                and sum(s["kind"] in ("block", "pre_entry") for s in f["signals"]) == 56,
                "в field.state автоблокировка: 33 блок-участка, 56 проходных/предвходных, направления 5 перегонов")
        R.check(mon.max_quiet_growth <= 10, f"без сбоев прогноз не растёт: макс. рост {mon.max_quiet_growth:.1f} мин за 30 мин")
        R.check(mon.http_5xx == 0, f"ответов 5xx: {mon.http_5xx}")
        lat = sorted(ws_stats.get("lat", [0]))
        p95 = lat[int(len(lat) * 0.95) - 1] if lat else 0
        R.check(ws_stats.get("snapshot") and ws_stats.get("msgs", 0) > 100 and p95 < 500,
                f"WS: snapshot при подключении, {ws_stats.get('msgs', 0)} сообщений, p95 доставки {p95:.0f} мс, "
                f"переподключений {ws_stats.get('reconnects', 0)}")
        print(f"\n{len(R.notes)} ok, {len(R.fails)} FAIL за {time.time() - t0:.0f} с")
        for x in R.fails:
            print("  ", x)


if __name__ == "__main__":
    asyncio.run(main())
