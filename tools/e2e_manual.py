"""Live check of the dispatcher's instructions through the API (tasks/02 §7.3): bounds -> preview -> commit ->
apply -> the pin in the plan -> an incident keeps it -> remove it.

    python -m tools.e2e_manual --base http://127.0.0.1:8000
"""
import argparse
import time

import httpx

ap = argparse.ArgumentParser()
ap.add_argument("--base", default="http://127.0.0.1:8000")
c = httpx.Client(base_url=ap.parse_args().base, timeout=20)
bad = []
c.post("/api/sim/clock", json={"speed": 1})
st = c.get("/api/state").json(); plan = st["plan"]; v = plan["version"]; now = st["field"]["sim_time"]
d = [e for e in plan["entries"] if e["kind"]=="dwell" and e["train_id"]=="2003"]
mid = next(e for e in d if e["start"] > now + 600 and e["station_id"] not in (d[0]["station_id"], d[-1]["station_id"]))
print("2003 at", mid["station_id"], mid["start"], mid["end"], "now", now, "v", v)
t=time.perf_counter(); b = c.get("/api/plan/manual/bounds", params={"train_id":"2003","station_id":mid["station_id"]}); print("bounds", b.status_code, round((time.perf_counter()-t)*1000),"ms", b.json()["point_type"], b.json()["dep"]["min"], b.json()["dep"]["max"])
body = {"base_plan_version": v, "train_id":"2003", "station_id": mid["station_id"], "kind":"dep", "time": mid["end"]+900}
t=time.perf_counter(); p = c.post("/api/plan/manual/preview", json=body); r=p.json(); print("preview", p.status_code, round((time.perf_counter()-t)*1000),"ms server", r["compute_ms"], "affected", r["affected"][:3], "dIdx", r["delta_index"])
code = c.post("/api/plan/manual/preview", json={**body, "base_plan_version": v-1 if v>1 else 999}).status_code
print("stale", code); bad += [] if code == 409 else ["stale"]
print("invalid", c.post("/api/plan/manual/preview", json={**body, "kind":"xx"}).status_code)
print("locked origin arr", c.post("/api/plan/manual/preview", json={**body, "station_id": d[0]["station_id"], "kind":"arr"}).status_code)
t=time.perf_counter(); cm = c.post("/api/plan/manual/commit", json=body); vs = cm.json()["variants"]; print("commit", cm.status_code, round((time.perf_counter()-t)*1000),"ms", [(x["title"], x["strategy"], x["score"], x["recommended"]) for x in vs]); print("  ", vs[0]["explanation"][:2])
best = next(x for x in vs if x["recommended"])
ap = c.post("/api/plan/apply", json={"variant_id": best["id"], "base_plan_version": v}); print("apply", ap.status_code, ap.json())
time.sleep(1.5)
plan = c.get("/api/plan").json(); pins = c.get("/api/plan/pins").json(); print("pins", [(p["description"], p["status"]) for p in pins], "plan.pins", len(plan["pins"]))
dep = next(e["end"] for e in plan["entries"] if e["kind"]=="dwell" and e["train_id"]=="2003" and e["station_id"]==mid["station_id"])
print("plan dep vs pin", dep, pins[0]["time"]); bad += [] if abs(dep - pins[0]["time"]) <= 60 else ["pin not kept"]
inc = c.post("/api/incidents", json={"type":"obstacle","segment_id":"R2-OZR","est_min_min":15,"est_max_min":30}).json()
time.sleep(6)
vars_ = c.get("/api/variants").json()
for x in vars_:
    e = next((e for e in x["plan"]["entries"] if e["kind"]=="dwell" and e["train_id"]=="2003" and e["station_id"]==mid["station_id"]), None)
    print("  incident variant", x["title"], "2003 dep", e and e["end"], "pin", pins[0]["time"])
    bad += [] if e is None or abs(e["end"] - pins[0]["time"]) <= 60 else [f"incident variant {x['title']} breaks the pin"]
for x in vars_: c.post(f"/api/plan/variants/{x['id']}/reject")
c.post(f"/api/incidents/{inc['id']}/resolve")
rm = c.delete(f"/api/plan/pins/{pins[0]['id']}"); print("remove", rm.status_code, "pins now", c.get("/api/plan/pins").json())
time.sleep(6)
print("after removal variants", [(x["title"], x["source"]) for x in c.get("/api/variants").json()])
print("journal", [j["text"][:90] for j in c.get("/api/journal").json() if j["kind"].startswith("pin") or "Указание" in j["text"]])
print("safety", c.get("/api/state").json()["field"]["safety_violations"])
safety = c.get("/api/state").json()["field"]["safety_violations"]
bad += [] if safety == 0 else ["safety"]
print("RESULT", "OK" if not bad else f"FAIL {bad}")
