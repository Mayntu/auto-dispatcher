"""Recorder: persists everything from the bus to TimescaleDB (CLAUDE.md §17).

`field.state` is thinned to 1 Hz into `train_state`; every other event lands in `events`, and
the meaningful ones also get a typed table (`plans`, `variants`, `incidents`, `kpi`, `dc_log`).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import asyncpg

from app.bus.envelope import Envelope

log = logging.getLogger("recorder")

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Writer:
    def __init__(self, dsn: str, train_state_hz: float = 1.0):
        self.dsn = dsn
        self._min_step = 1.0 / train_state_hz
        self.pool: asyncpg.Pool | None = None
        self._last_state_wall = 0.0

    async def connect(self) -> None:
        self.pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=4)
        await self._migrate()
        log.info("recorder connected to the database")

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()

    async def _migrate(self) -> None:
        for path in sorted(MIGRATIONS.glob("*.sql")):
            await self.pool.execute(path.read_text(encoding="utf-8"))
            log.info("migration applied: %s", path.name)

    # ---- routing ----------------------------------------------------------------------------
    async def on_event(self, env: Envelope) -> None:
        t = env.type
        if t == "field.state":
            await self._train_state(env)
            await self._event(env, record=False)
            return
        if t == "kpi.index":
            await self._kpi(env)
        elif t in ("plan.approved", "plan.refreshed"):
            await self._plan(env)
        elif t == "planner.variants":
            await self._variants(env)
        elif t.startswith("incident."):
            await self._incident(env)
        elif t in ("dc.command_result", "dc.log"):
            await self._dc_log(env)
        await self._event(env)

    # ---- typed tables -----------------------------------------------------------------------
    async def _train_state(self, env: Envelope) -> None:
        import time

        wall = time.time()
        if wall - self._last_state_wall < self._min_step:
            return
        self._last_state_wall = wall
        sim = env.payload.get("sim_time") or env.sim_time
        rows = []
        for tr in env.payload.get("trains", []):
            if not tr.get("on_field"):
                continue
            rows.append((
                _now(), sim, tr["train_id"], tr["status"], tr.get("segment_id"), tr.get("station_id"),
                tr.get("track_id"), tr.get("km"), tr.get("speed_kmh"), tr.get("delay_s"),
                tr.get("regime"), tr.get("energy_kwh"),
            ))
        if rows:
            await self.pool.executemany(
                """INSERT INTO train_state (ts, sim_time, train_id, status, segment_id, station_id,
                       track_id, km, speed_kmh, delay_s, regime, energy_kwh)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)""",
                rows,
            )

    async def _kpi(self, env: Envelope) -> None:
        idx = env.payload.get("index") or {}
        await self.pool.execute(
            "INSERT INTO kpi (ts, sim_time, value, category, components) VALUES ($1,$2,$3,$4,$5)",
            _now(), env.payload.get("plan_version"), idx.get("value"), idx.get("category"),
            json.dumps(idx.get("components"), ensure_ascii=False),
        )

    async def _plan(self, env: Envelope) -> None:
        p = env.payload
        idx = p.get("index") or {}
        await self.pool.execute(
            """INSERT INTO plans (version, created_at, sim_time, solver, strategy, index_value, payload)
               VALUES ($1,$2,$3,$4,$5,$6,$7)
               ON CONFLICT (version) DO UPDATE SET payload = EXCLUDED.payload,
                   index_value = EXCLUDED.index_value, solver = EXCLUDED.solver""",
            p.get("version"), _now(), p.get("created_at"), p.get("solver"), p.get("strategy"),
            idx.get("value"), json.dumps(p, ensure_ascii=False),
        )

    async def _variants(self, env: Envelope) -> None:
        for v in env.payload.get("variants", []):
            idx = (v.get("plan") or {}).get("index") or {}
            await self.pool.execute(
                """INSERT INTO variants (id, created_at, base_plan_version, strategy, status, index_value, payload)
                   VALUES ($1,$2,$3,$4,$5,$6,$7)
                   ON CONFLICT (id) DO UPDATE SET status = EXCLUDED.status, payload = EXCLUDED.payload""",
                v.get("id"), _now(), v.get("base_plan_version"), v.get("strategy"), v.get("status"),
                idx.get("value"), json.dumps(v, ensure_ascii=False),
            )

    async def _incident(self, env: Envelope) -> None:
        p = env.payload
        await self.pool.execute(
            """INSERT INTO incidents (id, type, status, payload, updated_at) VALUES ($1,$2,$3,$4,$5)
               ON CONFLICT (id) DO UPDATE SET status = EXCLUDED.status, payload = EXCLUDED.payload,
                   updated_at = EXCLUDED.updated_at""",
            p.get("id"), p.get("type"), p.get("status"), json.dumps(p, ensure_ascii=False), _now(),
        )

    async def _dc_log(self, env: Envelope) -> None:
        p = env.payload
        await self.pool.execute(
            "INSERT INTO dc_log (ts, sim_time, actor, command, ok, reason) VALUES ($1,$2,$3,$4,$5,$6)",
            _now(), env.sim_time, env.source, json.dumps(p, ensure_ascii=False),
            bool(p.get("ok", True)), p.get("reason"),
        )

    async def _event(self, env: Envelope, record: bool = True) -> None:
        if not record:
            return
        await self.pool.execute(
            "INSERT INTO events (ts, sim_time, id, type, source, payload) VALUES ($1,$2,$3,$4,$5,$6)",
            _now(), env.sim_time, env.id, env.type, env.source, json.dumps(env.payload, ensure_ascii=False),
        )
