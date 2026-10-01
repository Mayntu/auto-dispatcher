# Контракт бэкенд ↔ фронтенд (после realism-шага 5)

Источник правды по моделям — `app/railcore/models.py` и Swagger на `http://localhost:8000/docs`. Здесь — то, что нужно, чтобы собрать экран, и **заранее зафиксированный формат полей автоблокировки** (появятся в realism-шагах 3–4).

## Время

- Все времена — **секунды симуляции** от `SIM_EPOCH` (`GET /api/infra` → `sim_epoch`, сейчас `07:55`). Отображать как `ЧЧ:ММ` = `SIM_EPOCH + t`. Wall clock в интерфейсе не показывать нигде (§27.5).
- Единственное wall-время — `ts_wall` в конверте WS: только для замера задержки доставки `Date.now()/1000 − ts_wall`.

## REST (`/api`, без авторизации в MVP)

| Метод | Путь | Ответ / назначение |
|---|---|---|
| GET | `/api/infra` | `{infra: {stations, segments, signals, switches}, categories, timetable, sim_epoch, thresholds, station_order, segment_times}` — статичные данные, грузить один раз |
| GET | `/api/state` | Снимок: `{field, plan, variants, index, fact, journal}` (то же, что WS `snapshot`) |
| GET | `/api/plan` | Текущий утверждённый план `Plan` |
| GET / POST | `/api/incidents` | Активные сбои / создать: `{type: obstacle\|train_failure\|segment_closed, segment_id?, train_id?, km?, est_min_min, est_max_min}` → `Incident`; 422 при ошибке |
| POST | `/api/incidents/{id}/resolve` | Снять сбой; 404 если не активен |
| GET | `/api/variants` | Текущая пачка вариантов `Variant[]` (пусто = «Активных решений нет») |
| POST | `/api/plan/apply` | `{variant_id, base_plan_version}` → `{ok, plan_version, index, total_delay_s}`; **409** — вариант устарел (`detail` — текст для диспетчера, система сама пересчитывает); 404 — нет такого |
| POST | `/api/plan/variants/{id}/reject` | Отклонить; 409 если уже не активен |
| POST | `/api/plan/replan` | «Пересчитать план» без сбоя; варианты придут по WS |
| GET | `/api/journal?limit=200` | Журнал решений `JournalEntry[]` |
| POST | `/api/whatif` | `{modifications: [{kind: train_speed, target_id, value(км/ч)} \| {kind: incident_duration, target_id, value(мин)}]}` → `WhatIfResult` (≤ 3 с) |
| GET | `/api/ato/{train_id}` | `SpeedProfile` на текущий/ближайший перегон; 404 если поезд завершил маршрут |
| POST | `/api/sim/clock` | `{paused?, speed?}` |
| GET | `/api/system/metrics` | `{last_solve_ms, ui_p95_ms, safety_violations}` |

## WebSocket `/ws`

1. При подключении сервер шлёт `{"type": "snapshot", "payload": {field, plan, variants, index, fact, journal}}`. `fact` — `{train_id: [[t, km], …]}` за последние 3 ч (фактические нитки ГИД).
2. Дальше — конверты `{id, type, source, corr_id, ts_wall, sim_time, schema, payload}`:

| `type` | Частота | `payload` |
|---|---|---|
| `field.state` | 2 Гц (при медленном клиенте — только последний) | состояние поля, см. ниже |
| `kpi.index` | 1 Гц | `{index: IndexValue, kpi: KPI, plan_version, last_solve_ms, forecast_ok, plan_broken}` — **текущий** индекс (шапка) |
| `plan.approved` | по событию | `Plan` — новая версия (смена порядка, решение диспетчера) |
| `plan.refreshed` | по событию | `Plan` — та же версия, уточнены времена (без смены порядка) |
| `planner.variants` | по событию и 1 Гц, пока есть предложенные | `{incident_ids, base_plan_version, solve_ms, variants: Variant[]}` — **заменяет** пачку целиком |
| `journal.entry` | по событию | `JournalEntry` |
| `incident.created` / `incident.resolved` | по событию | `Incident` (показать «считаем варианты…» до `planner.variants`) |
| `planner.metrics`, `dc.command_result`, `safety.violation`, `field.train_event` | по событию | служебные |

Клиент → сервер: `{"op": "latency", "p95_ms": N}` раз в 5 с.

## `field.state`

```jsonc
{
  "sim_time": 3019.0, "speed": 20, "effective_speed": 1, "paused": false,
  "decision_hold": true,            // ждём решения диспетчера: время идёт ×1 — показать на экране
  "counters": {"in_transit": 4, "at_stations": 3, "waiting": 1, "arrived": 2},   // строка статуса §27.5
  "trains": [TrainState],
  "incidents": [Incident],           // активные
  "closed_segments": ["R1-STP"], "occupied_segments": ["SEV-R1"],
  "signals": [{"id": "SEV-odd-exit", "station_id": "SEV", "direction": "odd", "kind": "entry|exit", "aspect": "green|red", "segment_id": "SEV-R1"}],
  "safety_violations": 0, "plan_overrides": 0, "stalled_s": 0, "plan_version": 3
}
```

`TrainState`: `train_id, status (waiting|running|dwelling|stopped|broken|finished), segment_id, station_id, track_id (путь станции), pos_m, km, speed_kmh, regime, delay_s (к плану), next_station_id, energy_kwh, on_field, progress (0..1 по перегону), arrived_at, stopped`. Опоздание **к расписанию** на конечной — из `kpi.index.kpi.delayed_trains` (`[[train_id, seconds], …]`).

## `Plan` → нитки ГИД

`entries[]`: `{train_id, kind: run|dwell, segment_id|station_id, track_id, start, end, stop, unplanned}`; у `dwell` — `track_id`, путь станции, назначенный планом (с учётом полезной длины и платформ), ДЦ ставит поезд на него. Попутные поезда могут идти по перегону пакетом (два `run` одного направления перекрываются по времени). Нитка поезда: `dwell` — горизонтальный отрезок на оси пункта (`station.km`), `run` — отрезок между осями пунктов (`direction` поезда из `timetable`: `odd` — к большему км). `meetings[]`: `{kind: crossing|overtake, station_id, time, waiting_train, passing_train, wait_s}` — подсвечивать на ГИД.

## `Variant` (карточка решения)

```jsonc
{
  "id": "36f8", "title": "Минимум задержек", "strategy": "balanced",
  "kind": "incident|return|replan|broken",     // return = «Возврат к графику» после снятия сбоев
  "status": "proposed|applied|stale|rejected",
  "recommended": true,                          // ровно одна карточка; метку ставить по этому полю
  "score": 170.4,                               // единое мерило: взвешенные минуты (меньше — лучше)
  "plan": Plan,                                 // «прогноз индекса при варианте» = plan.index (подпись отличается от шапки!)
  "delta_index": 3.6, "delta_delay_s": -360,    // к текущему прогнозу (kpi.index)
  "explanation": ["…", "…"],                    // 2–4 предложения, свернуть под «Почему так»
  "updated_at": 3019.0,                         // живые карточки: цифры на этот момент (часы симуляции)
  "base_plan_version": 3, "incident_ids": ["7f70…"]
}
```

`plan.kpi.robust_total_delay_s` — строка «если сбой затянется», **только когда не `null`**. Хвост «Призрак» на ГИД: нитки `variant.plan.entries` точками.

## Автоблокировка (realism-шаги 3–5, уже отдаётся)

Формат для мнемосхемы и ГИД (§8, §9.1, §27.2):

`GET /api/infra` → `infra.blocks: [{id: "R1-STP-B3", segment_id, index, from_m, to_m}]` (координаты от начала перегона в нечётном направлении, без зазоров), `infra.signals` добавятся `{id: "R1-STP-P3N", kind: "block|pre_entry", direction, segment_id, pos_m}` (проходные и предвходные), `stations[].simultaneous_reception: bool`.

`field.state` добавится:

```jsonc
"blocks":     [{"id": "R1-STP-B3", "occupied_by": "2003" | null, "obstacle": false}],
"signals":    [... , {"id": "R1-STP-P3N", "aspect": "green|yellow|red"}],   // все сигналы, включая проходные
"directions": {"R1-STP": {"direction": "odd|even|null", "changing": false}}, // стрелка направления на перегоне
"warnings":   [{"segment_id": "SEV-R1", "from_m": 3000, "to_m": 6000, "v_kmh": 40}]  // предупреждения — ПОЯВИТСЯ в шаге 6
```

`TrainState.block_id` — блок-участок головы поезда. Настройки интервалов (`headway_s`, `tau_cross_s`, `tau_np_s`, `direction_change_s`) — в `GET /api/infra` → `intervals`.
