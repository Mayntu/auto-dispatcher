"""Simulation reset (button and the end of the timetable) through the whole service."""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from app.all import build_app


def wait(cond, timeout=20.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.2)
    return False


def test_reset_starts_over_with_a_fresh_plan():
    with TestClient(build_app()) as c:
        assert wait(lambda: (c.get("/api/state").json().get("plan") or {}).get("version") == 1)
        c.post("/api/sim/clock", json={"speed": 60})
        assert wait(lambda: c.get("/api/state").json()["field"]["sim_time"] > 300)
        inc = c.post("/api/incidents", json={"type": "obstacle", "segment_id": "R1-STP", "est_min_min": 15,
                                             "est_max_min": 30}).json()
        assert wait(lambda: any(v["status"] == "proposed" for v in c.get("/api/variants").json()))
        assert c.post("/api/sim/reset").status_code == 200
        assert wait(lambda: (c.get("/api/state").json().get("plan") or {}).get("version") == 1
                    and c.get("/api/state").json()["field"]["sim_time"] < 600)
        st = c.get("/api/state").json()
        assert st["field"]["incidents"] == [] and inc["id"] not in str(st["field"]["incidents"])
        assert c.get("/api/variants").json() == []
        assert any(j["kind"] == "sim_reset" for j in c.get("/api/journal").json())
        assert st["field"]["safety_violations"] == 0


def test_simulation_restarts_by_itself_when_the_timetable_ends():
    app = build_app()
    with TestClient(app) as c:
        assert wait(lambda: (c.get("/api/state").json().get("plan") or {}).get("version") == 1)
        c.post("/api/sim/clock", json={"speed": 60})
        field = app.state.field
        assert wait(lambda: field.sim.now > 120)
        field.timetable_end = field.sim.now + 60  # as if the 48 h timetable ran out a minute from now
        assert wait(lambda: any(j["kind"] == "sim_reset" and "график закончился" in j["text"]
                                for j in c.get("/api/journal").json()))
        assert c.get("/api/state").json()["field"]["sim_time"] < 600
