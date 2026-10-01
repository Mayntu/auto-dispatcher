"""Generate data/infra.json — the line, its stations and the automatic block (CLAUDE.md §9.1).

    python -m tools.gen_infra

- Stations and segments from §9.1.
- Every segment is split into equal block sections, count = max(3, round(length / 2500)); ids `{segment}-B{n}`,
  n from 1 in the odd direction.
- On every inner block boundary — block signals of both directions `{segment}-P{n}{N|C}` (N odd, C even),
  kind `block`; the last block signal before a station in the direction of travel is `pre_entry`.
- Every station: entry and exit signal per direction `{station}-{dir}-{entry|exit}`; switches: one per side
  track in each throat `{station}-{track}-{W|E}`.
- Simultaneous reception of oncoming trains: stations yes, sidings no (tau_np applies).
"""

from __future__ import annotations

import json

from app.common.config import get_env

BLOCK_TARGET_M = 2500

# id, name, kind, km, tracks (id, useful length m, main, platform)
STATIONS = [
    ("SEV", "Ст. Северная", "station", 0, [("I", 1200, True, True), ("3", 1050, False, True), ("4", 1050, False, False), ("5", 850, False, False)]),
    ("R1", "Рзд. 1", "siding", 15, [("I", 1100, True, False), ("2", 1050, False, False)]),
    ("STP", "Ст. Степная", "station", 33, [("I", 1200, True, True), ("3", 1050, False, True), ("4", 900, False, False)]),
    ("R2", "Рзд. 2", "siding", 49, [("I", 1100, True, False), ("2", 1050, False, False)]),
    ("OZR", "Ст. Озёрная", "station", 66, [("I", 1200, True, True), ("3", 1050, False, True)]),
    ("YUZ", "Ст. Южная", "station", 84, [("I", 1200, True, True), ("3", 1050, False, True), ("4", 1050, False, False), ("5", 850, False, False)]),
]
SEGMENTS = [
    {"id": "SEV-R1", "from_station": "SEV", "to_station": "R1", "length_m": 15000, "v_max_kmh": 120},
    {"id": "R1-STP", "from_station": "R1", "to_station": "STP", "length_m": 18000, "v_max_kmh": 120,
     "speed_zones": [{"from_m": 9000, "to_m": 11000, "v_kmh": 70}]},
    {"id": "STP-R2", "from_station": "STP", "to_station": "R2", "length_m": 16000, "v_max_kmh": 100,
     "grade_zones": [{"from_m": 4000, "to_m": 10000, "permille": 8}]},
    {"id": "R2-OZR", "from_station": "R2", "to_station": "OZR", "length_m": 17000, "v_max_kmh": 120},
    {"id": "OZR-YUZ", "from_station": "OZR", "to_station": "YUZ", "length_m": 18000, "v_max_kmh": 120,
     "grade_zones": [{"from_m": 6000, "to_m": 12000, "permille": -5}]},
]


def blocks_and_signals(seg: dict) -> tuple[list[dict], list[dict]]:
    length = seg["length_m"]
    n = max(3, round(length / BLOCK_TARGET_M))
    edges = [round(length * k / n) for k in range(n + 1)]
    blocks = [{"id": f"{seg['id']}-B{k + 1}", "segment_id": seg["id"], "index": k + 1,
               "from_m": edges[k], "to_m": edges[k + 1]} for k in range(n)]
    signals = []
    for k in range(1, n):  # inner boundaries
        # odd trains run towards larger m: the signal at boundary k guards block k+1; the last one before the
        # station at the segment end is the pre-entry signal
        signals.append({"id": f"{seg['id']}-P{k}N", "direction": "odd", "kind": "pre_entry" if k == n - 1 else "block",
                        "segment_id": seg["id"], "pos_m": edges[k]})
        # even trains run towards smaller m: the signal at boundary k guards block k; the last one before the
        # station at the segment start is the pre-entry signal
        signals.append({"id": f"{seg['id']}-P{k}C", "direction": "even", "kind": "pre_entry" if k == 1 else "block",
                        "segment_id": seg["id"], "pos_m": edges[k]})
    return blocks, signals


def build() -> dict:
    stations = [{"id": i, "name": name, "kind": kind, "km": km, "simultaneous_reception": kind == "station",
                 "tracks": [{"id": t, "length_m": length, "main": main, "platform": platform}
                            for t, length, main, platform in tracks]}
                for i, name, kind, km, tracks in STATIONS]
    blocks, signals = [], []
    for st in stations:
        for d in ("odd", "even"):
            for k in ("entry", "exit"):
                signals.append({"id": f"{st['id']}-{d}-{k}", "direction": d, "kind": k, "station_id": st["id"]})
    for seg in SEGMENTS:
        b, s = blocks_and_signals(seg)
        blocks += b
        signals += s
    switches = [{"id": f"{st['id']}-{t['id']}-{w}", "station_id": st["id"], "throat": "west" if w == "W" else "east",
                 "track_id": t["id"]} for st in stations for t in st["tracks"] if not t["main"] for w in ("W", "E")]
    return {"stations": stations, "segments": SEGMENTS, "blocks": blocks, "signals": signals, "switches": switches}


def main() -> None:
    infra = build()
    out = get_env().data_dir / "infra.json"
    out.write_text(json.dumps(infra, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {out}: {len(infra['stations'])} stations, {len(infra['segments'])} segments, "
          f"{len(infra['blocks'])} block sections, {len(infra['signals'])} signals")


if __name__ == "__main__":
    main()
