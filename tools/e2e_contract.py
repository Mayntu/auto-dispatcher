"""Live check of the API the frontend asked for (docs/backend-issues.md 2-10): estimate, what-if promote,
settings, new incident types, scenarios, conflict forecast, history and report.

    python -m tools.e2e_contract --base http://127.0.0.1:8000
"""
import argparse
import time

import httpx

ap = argparse.ArgumentParser()
ap.add_argument("--base", default="http://127.0.0.1:8000")
c = httpx.Client(base_url=ap.parse_args().base, timeout=30)
bad: list[str] = []


def check(ok: bool, what: str) -> None:
    print(("  ok   " if ok else "  FAIL ") + what)
    if not ok:
        bad.append(what)


def clear_cards() -> None:
    for v in c.get("/api/variants").json():
        if v["status"] == "proposed":
            c.post(f"/api/plan/variants/{v['id']}/reject")


c.post("/api/sim/clock", json={"speed": 20})
time.sleep(20)

# 3. estimate
inc = c.post("/api/incidents", json={"type": "obstacle", "segment_id": "R1-STP", "km": 24.5, "est_min_min": 15,
                                     "est_max_min": 30}).json()
time.sleep(4)
r = c.post(f"/api/incidents/{inc['id']}/estimate", json={"est_min_min": 5, "est_max_min": 10, "description": "Бригада на месте"})
check(r.status_code == 200 and r.json()["est_max_s"] == 600, f"estimate → {r.status_code}")
time.sleep(4)
j = [x["kind"] for x in c.get("/api/journal").json()[-10:]]
check("incident_updated" in j and "variants_proposed" in j, "estimate journaled and variants recomputed")
check(c.post("/api/incidents/nope/estimate", json={"est_min_min": 5, "est_max_min": 10}).status_code == 404, "estimate unknown → 404")
clear_cards()
c.post(f"/api/incidents/{inc['id']}/resolve")
time.sleep(4)
clear_cards()

# 2. what-if promote
w = c.post("/api/whatif", json={"modifications": [{"kind": "train_speed", "target_id": "2003", "value": 60}]}).json()
p = c.post(f"/api/whatif/{w['request_id']}/promote")
check(p.status_code == 200 or (p.status_code == 409 and "what-if" in p.json()["detail"]),
      f"promote of an order made for 60 km/h → {p.status_code} (409 must explain)")
clear_cards()
w = c.post("/api/whatif", json={"modifications": [{"kind": "train_speed", "target_id": "2003", "value": 80}]}).json()
p = c.post(f"/api/whatif/{w['request_id']}/promote")
check(p.status_code == 200 and p.json()["source"] == "whatif", f"what-if promote → {p.status_code}")
if p.status_code == 200:
    v = p.json()
    time.sleep(2)
    a = c.post("/api/plan/apply", json={"variant_id": v["id"], "base_plan_version": v["base_plan_version"]})
    check(a.status_code == 200, f"apply promoted what-if → {a.status_code}")
check(c.post("/api/whatif/nope/promote").status_code == 404, "promote unknown → 404")
clear_cards()

# 7. settings
s0 = c.get("/api/settings").json()
s1 = c.put("/api/settings", json={"index": {"weights": {"schedule": 2, "capacity": 1, "energy": 1, "conflicts": 1, "accuracy": 1}},
                                  "priority_weights": {"freight": 1.5}})
check(s1.status_code == 200 and abs(sum(s1.json()["index"]["weights"].values()) - 1) < 1e-3
      and s1.json()["priority_weights"]["freight"] == 1.5, "settings PUT normalises weights")
check(c.put("/api/settings", json={"index": {"thresholds": {"norm": 50, "attention": 70}}}).status_code == 422, "bad thresholds → 422")
check(c.put("/api/settings", json={"planner": {"horizon_s": 1}}).status_code == 422, "non-editable key → 422")
c.put("/api/settings", json={"index": {"weights": s0["index"]["weights"]}, "priority_weights": s0["priority_weights"]})

# 5. new incident types
st = c.get("/api/state").json()["field"]
tid = next(t["train_id"] for t in st["trains"] if t["on_field"] and t["status"] != "finished")
d = c.post("/api/incidents", json={"type": "train_delay", "train_id": tid, "est_min_min": 10, "est_max_min": 20})
check(d.status_code == 200 and d.json()["station_id"], f"train_delay → {d.status_code}")
sf = c.post("/api/incidents", json={"type": "signal_failure", "station_id": "STP", "direction": "even",
                                    "est_min_min": 20, "est_max_min": 40})
check(sf.status_code == 200, f"signal_failure → {sf.status_code}")
time.sleep(3)
sig = {x["id"]: x["aspect"] for x in c.get("/api/state").json()["field"]["signals"]}
check(sig.get("STP-even-exit") == "invitation", "failed exit signal shows the call-on aspect")
check(c.post("/api/incidents", json={"type": "track_closed", "est_min_min": 1, "est_max_min": 2}).status_code == 422,
      "unsupported type → 422")
time.sleep(5)
clear_cards()
for i in c.get("/api/incidents").json():
    c.post(f"/api/incidents/{i['id']}/resolve")
time.sleep(4)
clear_cards()

# 4. scenarios
sc = c.get("/api/scenarios").json()
check({"cow_on_segment", "train_failure", "mass_incidents"} <= {x["id"] for x in sc}, f"scenarios listed: {len(sc)}")
run = c.post("/api/scenarios/mass_incidents/run")
check(run.status_code == 200, f"run mass_incidents → {run.status_code}")
check(c.post("/api/scenarios/nope/run").status_code == 404, "unknown scenario → 404")
t0 = time.time()
n = 0
# 8 incidents over 2 simulated minutes; while cards wait for a decision the line runs at ×1 — up to ~2.5 real minutes
seen: set[str] = set()
while time.time() - t0 < 180 and len(seen) < 8:
    seen |= {i["id"] for i in c.get("/api/incidents").json()}
    time.sleep(1)
check(len(seen) == 8, f"mass_incidents created {len(seen)} incidents")
time.sleep(6)
check(any(v["status"] == "proposed" for v in c.get("/api/variants").json()), "variants for mass incidents")

# 6. conflicts
state = c.get("/api/state").json()
check("conflicts" in state and isinstance(state["conflicts"], list), f"conflict forecast in state: {len(state.get('conflicts', []))}")
clear_cards()
for i in c.get("/api/incidents").json():
    c.post(f"/api/incidents/{i['id']}/resolve")

# 8. history and report
fr = c.get("/api/history/frames", params={"step": 10}).json()
check(len(fr) > 5 and "trains" in fr[-1], f"history frames: {len(fr)}")
ev = c.get("/api/history/events").json()
check(len(ev) > 5, f"history events: {len(ev)}")
rep = c.get("/api/reports", params={"format": "csv"})
check(rep.status_code == 200 and "Индекс качества движения" in rep.text, "CSV report")
check(c.get("/api/reports", params={"format": "pdf"}).status_code == 422, "PDF → 422 with a reason")

safety = c.get("/api/state").json()["field"]["safety_violations"]
check(safety == 0, f"safety violations {safety}")
print("RESULT", "OK" if not bad else f"FAIL {bad}")
