"""Render docs/pitch-numbers.md from docs/pitch-numbers.json and the stress JSON files.

    python -m tools.pitch_report --reliability R.json --base B.json --balanced S.json
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

from tools.stress import verdict

ROOT = Path(__file__).resolve().parents[1]
J = ROOT / "docs" / "pitch-numbers.json"
MD = ROOT / "docs" / "pitch-numbers.md"


def reliability(runs: list[dict]) -> dict:
    bad = [(r["seed"], verdict(r)) for r in runs if verdict(r)]
    return {
        "runs": len(runs), "seeds": f"{min(r['seed'] for r in runs)}–{max(r['seed'] for r in runs)}",
        "sim_hours": round(sum(r.get("end") or 0 for r in runs) / 3600, 1),
        "incidents": sum(r["incidents"] for r in runs if isinstance(r["incidents"], int)),
        "mass_runs": sum(1 for r in runs if r["mass"]),
        "safety_violations": sum(r.get("violations", 0) or 0 for r in runs),
        "hangs": sum(1 for _, b in bad if any("STALL" in x or "NOT FINISHED" in x or "TIMEOUT" in x for x in b)),
        "failed_runs": len(bad), "failures": bad,
        "guard_releases": sum(r.get("overrides", 0) or 0 for r in runs),
        "guard_replans": sum(r.get("guard_replans", 0) or 0 for r in runs),
        "solves": sum(r["solves"] for r in runs), "fallbacks": sum(r.get("fallbacks", 0) or 0 for r in runs),
        "pins_set": sum(r.get("pins", 0) or 0 for r in runs),
        "pin_violations_in_solves": sum(r.get("pins_violated", 0) or 0 for r in runs),
        "max_solve_ms": max(r["max_solve_ms"] for r in runs),
    }


def effect(base: list[dict], sys_: list[dict]) -> dict:
    b = {r["seed"]: r for r in base if "late_total_min" in r}
    s = {r["seed"]: r for r in sys_ if "late_total_min" in r}
    seeds = sorted(set(b) & set(s))

    def tot(d, k):
        return round(sum(d[x][k] for x in seeds), 1)

    def red(k):
        tb, ts = tot(b, k), tot(s, k)
        return round(100 * (tb - ts) / tb, 1) if tb else None

    saved = [b[x]["late_total_min"] - s[x]["late_total_min"] for x in seeds]
    return {
        "seeds": len(seeds), "shift_h": 8.5,
        "base": {k: tot(b, k) for k in ("late_total_min", "late_weighted_min", "late_trains", "due_trains",
                                         "unplanned_stops", "energy_kwh")},
        "balanced": {k: tot(s, k) for k in ("late_total_min", "late_weighted_min", "late_trains", "due_trains",
                                             "unplanned_stops", "energy_kwh")},
        "reduction_pct": {k: red(k) for k in ("late_total_min", "late_weighted_min", "late_trains", "unplanned_stops",
                                              "energy_kwh")},
        "saved_min_per_shift": {"mean": round(statistics.mean(saved), 1), "median": round(statistics.median(saved), 1),
                                "worse_seeds": sum(1 for x in saved if x < 0)},
        "index_mean": {"base": round(statistics.mean(b[x]["index_mean"] for x in seeds if b[x]["index_mean"]), 1),
                       "balanced": round(statistics.mean(s[x]["index_mean"] for x in seeds if s[x]["index_mean"]), 1)},
        "index_min": {"base": round(statistics.mean(b[x]["index_min"] for x in seeds if b[x]["index_min"]), 1),
                      "balanced": round(statistics.mean(s[x]["index_min"] for x in seeds if s[x]["index_min"]), 1)},
        "base_guard_replans": sum(b[x].get("guard_replans", 0) for x in seeds),
        "per_seed": [{"seed": x, "base_late_min": b[x]["late_total_min"], "balanced_late_min": s[x]["late_total_min"],
                      "incidents": b[x]["incidents"]} for x in seeds],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reliability")
    ap.add_argument("--base")
    ap.add_argument("--balanced")
    a = ap.parse_args()
    data = json.loads(J.read_text(encoding="utf-8"))
    if a.reliability:
        data["reliability"] = reliability(json.loads(Path(a.reliability).read_text()))
    if a.base and a.balanced:
        data["effect"] = effect(json.loads(Path(a.base).read_text()), json.loads(Path(a.balanced).read_text()))
    J.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in data.items() if k in ("reliability", "effect")}, ensure_ascii=False, indent=1)[:4000])


if __name__ == "__main__":
    main()
