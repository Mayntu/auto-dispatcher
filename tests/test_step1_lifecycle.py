"""Step 1 DoD (tasks/01-realism.md, CLAUDE.md §27.5): variant lifecycle and decision journal through the API.

incident -> variants (line runs at ×1 while a decision is pending) -> apply -> clear the incident ->
"no active decisions", the decision is in the journal, every time is simulation time.
"""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from app.all import build_app


def wait(cond, timeout=20.0, step=0.2):
    t0 = time.time()
    while time.time() - t0 < timeout:
        value = cond()
        if value:
            return value
        time.sleep(step)
    raise AssertionError("condition not reached in time")


def proposed(c: TestClient) -> list[dict]:
    return [v for v in c.get("/api/variants").json() if v["status"] == "proposed"]


def test_incident_apply_clear_journal():
    with TestClient(build_app()) as c:
        c.post("/api/sim/clock", json={"speed": 60})
        wait(lambda: c.get("/api/state").json()["field"]["sim_time"] >= 900)
        inc = c.post("/api/incidents", json={"type": "obstacle", "segment_id": "R1-STP", "km": 24.5,
                                             "est_min_min": 15, "est_max_min": 30}).json()

        vs = wait(lambda: proposed(c))
        assert len(vs) == 3
        assert sum(v["recommended"] for v in vs) == 1, "exactly one recommendation"
        assert all(v["score"] is not None for v in vs), "one yardstick for every card"
        assert all(v["plan"]["kpi"]["robust_total_delay_s"] is not None for v in vs), "range 15–30: show the risk"
        field = wait(lambda: (f := c.get("/api/state").json()["field"]) and f["decision_hold"] and f)
        assert field["effective_speed"] == 1, "line runs in real time while a decision is pending"

        best = next(v for v in vs if v["recommended"])
        version = c.get("/api/plan").json()["version"]
        r = c.post("/api/plan/apply", json={"variant_id": best["id"], "base_plan_version": version})
        assert r.status_code == 200, r.text
        wait(lambda: not c.get("/api/state").json()["field"]["decision_hold"])

        assert c.post(f"/api/incidents/{inc['id']}/resolve").status_code == 200
        # after the incident clears: quiet re-timing; a "return to schedule" card only if it really pays
        def settled():
            vs_now = c.get("/api/variants").json()
            live = [v for v in vs_now if v["status"] == "proposed"]
            for v in live:
                assert v["kind"] == "return" and v["title"] == "Возврат к графику"
                c.post(f"/api/plan/variants/{v['id']}/reject")
            return not vs_now
        wait(settled, timeout=30)

        journal = c.get("/api/journal").json()
        kinds = [e["kind"] for e in journal]
        for k in ("incident_created", "variants_proposed", "variant_applied", "incident_resolved",
                  "decisions_archived", "decision_hold"):
            assert k in kinds, f"journal misses {k}: {kinds}"
        applied = next(e for e in journal if e["kind"] == "variant_applied")
        assert best["title"] in applied["text"]
        sim_now = c.get("/api/state").json()["field"]["sim_time"]
        assert all(0 <= e["time"] <= sim_now for e in journal), "journal uses simulation time only"
        # the hold is lifted on the planner's next tick and reaches the field within ~1.5 s
        wait(lambda: not c.get("/api/state").json()["field"]["decision_hold"], timeout=5)


def test_no_risk_line_without_duration_range():
    with TestClient(build_app()) as c:
        c.post("/api/sim/clock", json={"speed": 60})
        wait(lambda: c.get("/api/state").json()["field"]["sim_time"] >= 900)
        c.post("/api/incidents", json={"type": "segment_closed", "segment_id": "R2-OZR",
                                       "est_min_min": 30, "est_max_min": 30})
        vs = wait(lambda: proposed(c))
        assert all(v["plan"]["kpi"]["robust_total_delay_s"] is None for v in vs), "30–30 min: no 'if it lasts longer'"
        r = c.post(f"/api/plan/variants/{vs[0]['id']}/reject")
        assert r.status_code == 200
        assert c.post(f"/api/plan/variants/{vs[0]['id']}/reject").status_code == 409
