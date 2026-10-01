# CLAUDE.md — «Автодиспетчер»

Консультативная система поддержки решений поездного диспетчера однопутного участка: бесконфликтный график (CP-SAT), альтернативы при сбоях с оценкой последствий, what-if песочница, автоведение (Connected DAS), эмуляция диспетчерской централизации (ДЦ).

Этот файл — единственный источник правды по проекту. Если реализация расходится с файлом, прав файл; если файл неполон — выбери самое простое решение, совместимое с ним, и допиши решение в раздел 25 «Журнал решений».

- Быстрая часовая версия: **MVP.md** (та же структура пакетов, урезанный объём).
- Полная версия: фазы в разделе 22.
- Язык интерфейса — русский. Код, идентификаторы, коммиты, комментарии — английский.

---

## 1. Продукт

Диспетчер ДЦ управляет участком из 6 раздельных пунктов и ~10 поездов. Система:

1. Держит **бесконфликтный план** (кто где скрещивается/обгоняется, на какой путь станции встаёт, когда отправляется).
2. При сбое (поломка поезда, скот на пути, отказ сигнала, закрытие перегона, ограничение скорости, опоздание) за **≤ 5 с** предлагает **3 варианта плана** с цифрами: суммарная задержка, кто и насколько опоздает, остановки, энергия, индекс, устойчивость («что если ремонт затянется до максимума»), объяснение текстом.
3. **What-if**: диспетчер меняет параметр («поезд 2003 идёт 60 км/ч вместо 30», «ремонт 40 мин», «отменить поезд») и видит прогноз поверх действующего плана, не трогая его.
4. **Автоведение**: для каждого поезда рекомендованный профиль скорости (разгон → ход → накат → торможение), синхронизированный с планом: поезд приезжает ровно к моменту освобождения пути, а не стоит в ожидании скрещения.
5. **ДЦ**: мнемосхема, маршруты, стрелки, сигналы, автозадание маршрутов по утверждённому плану, ручное управление, журнал команд.
6. **Индекс качества движения** 0–100 из 5 факторов, веса и пороги настраиваются без перекомпиляции.
7. **Перемотка** последних 5–15 минут и отчёт PDF/CSV.

Система ничего не делает сама с порядком движения: все изменения порядка утверждает диспетчер (human-in-the-loop). Автоматически допускается только уточнение времён без изменения порядка.

---

## 2. Критерии жюри → что делаем

| Критерий | Вес | Чем закрываем | Где |
|---|---|---|---|
| Автодиспетчеризация, автоведение, realtime | 35% | CP-SAT с назначением путей, 3 стратегии параллельно, warm start, оценка устойчивости, CDR-прогноз конфликтов, eco-driving по оптимальным режимам, поток 2 Гц, debounce сбоев | `app/planner`, `app/ato`, `app/field` |
| UI/UX | 30% | Мнемосхема ДЦ, поездограмма с прогнозом и «призраками» вариантов, карточки решений с цифрами, what-if, профиль скорости, индекс с разбивкой, живые метрики задержки | `web/` |
| Архитектура backend | 25% | Сервисы из одного кодового базиса, шина событий за интерфейсом (memory/RabbitMQ), общая библиотека railcore, TimescaleDB, OpenAPI | `app/*`, `docker-compose.yml` |
| Демо и инженерная культура | 10% | `make up` одной командой, сценарии, нагрузочный тест, логи JSON, `/metrics`, тесты, README, архитектурная схема | `tools/`, `tests/`, `docs/` |

## 3. Фишки, которые выигрывают (обязательны)

1. **Connected DAS.** Профиль скорости считается от плана: если поезд по плану ждёт скрещения, он не мчится и не стоит, а приезжает к моменту освобождения пути. Экономия энергии видна в процентах.
2. **Устойчивость варианта.** У каждой карточки строка «если ремонт займёт максимум (45 мин): +N мин задержки».
3. **Вариант «вспомогательный локомотив».** При поломке поезда система сравнивает «ждать ремонт» и «вывезти сломанный поезд вспомогательным локомотивом» (ETA из настроек).
4. **Назначение путей → маршруты ДЦ.** План содержит путь станции для каждого поезда; ДЦ автоматически задаёт маршруты по утверждённому плану.
5. **Монитор безопасности.** Независимая проверка в симуляторе: два поезда на одном однопутном перегоне или одном пути станции = нарушение. В UI счётчик «Нарушений безопасности: 0».
6. **Призраки на поездограмме.** Наведение на вариант или what-if рисует его пунктиром поверх текущего плана, с подсветкой изменённых скрещений.
7. **Объяснимость.** Каждый вариант — 2–4 предложения на русском, построенные из данных плана (шаблоны, без LLM).
8. **Живые НФТ.** В верхней панели p95 задержки обновления UI и время последнего решения: «план пересчитан за 1.8 с».

---

## 4. Глоссарий (домен)

- **Участок** — линия из раздельных пунктов, за которую отвечает один диспетчер.
- **Раздельный пункт** — станция или разъезд, где есть несколько путей; поезда могут разъехаться.
- **Перегон** — однопутный путь между соседними раздельными пунктами. Модель блокировки — полуавтоматическая (ПАБ): **на перегоне одновременно один поезд**.
- **Нечётное направление (`odd`)** — по возрастанию км; **чётное (`even`)** — по убыванию.
- **Скрещение** — встреча встречных поездов: один ждёт на раздельном пункте, другой проходит.
- **Обгон** — быстрый попутный поезд проходит медленный, стоящий на боковом пути.
- **Главный путь** — сквозной путь станции; **боковой** — для стоянки/обгона/скрещения. Проход по боковому — с ограничением 40 км/ч, считаем как остановку.
- **ДЦ** — диспетчерская централизация: удалённое управление стрелками и сигналами. **Маршрут** — заданный диспетчером путь через стрелки станции с открытием сигнала.
- **СЦБ** — устройства безопасности. Мы их не заменяем.
- **Поездограмма** — график движения: X — время, Y — км, линия — поезд.
- **Время хода** — минимальное время проследования перегона. **Время на разгон/замедление** — надбавка, если поезд стоял в начале/останавливается в конце.
- **Автоведение (ATO advisory) / DAS** — рекомендации машинисту. **Накат (выбег)** — движение без тяги.

---

## 5. Архитектура

```mermaid
flowchart TB
  web[Web UI<br/>React] <-->|REST + WS| api[API gateway<br/>FastAPI]
  api <--> bus[(Event bus<br/>memory / RabbitMQ)]
  bus <--> field[field<br/>sim + ДЦ]
  bus <--> planner[planner<br/>CP-SAT, what-if]
  bus <--> ato[ato<br/>eco-driving]
  bus --> recorder[recorder] --> db[(TimescaleDB)]
  api --> db
  subgraph railcore [shared lib: app/railcore]
    field
    planner
    ato
  end
```

| Сервис | Отвечает за | Публикует | Слушает |
|---|---|---|---|
| `field` | Единственный источник правды о поле: движение поездов, сигналы, стрелки, маршруты, сбои, сценарии, монитор безопасности | `field.state`, `field.train_event`, `incident.*`, `dc.command_result`, `dc.log`, `safety.violation` | `cmd.field.*` |
| `planner` | План, варианты, what-if, прогноз конфликтов, индекс | `plan.approved`, `plan.refreshed`, `planner.variants`, `planner.conflicts`, `planner.whatif.result`, `kpi.index`, `planner.metrics` | `field.state`, `incident.*`, `cmd.planner.*` |
| `ato` | Профили скорости по плану | `ato.profiles` | `plan.approved`, `plan.refreshed`, `cmd.ato.*` |
| `recorder` | Запись всего в БД | — | `#` (всё) |
| `api` | REST, WS, авторизация, кэш последних состояний, отчёты, история | команды `cmd.*` | всё нужное UI |

**Режимы запуска** (переменная `BUS`):
- `BUS=memory` — все сервисы в одном процессе (`python -m app.all`), шина в памяти, БД опциональна (без неё история — кольцевой буфер в API). Для разработки и MVP.
- `BUS=rabbit` — Docker Compose: каждый сервис отдельный контейнер **из одного образа**, разные команды запуска.

Тяжёлые вычисления (CP-SAT, профили) — только в `ProcessPoolExecutor`, никогда в event loop.

### Структура репозитория

```
autodispatcher/
  CLAUDE.md  MVP.md  README.md  Makefile  pyproject.toml  Dockerfile  docker-compose.yml  .env.example
  data/
    infra.json  rolling_stock.json  timetable.json  settings.default.json
    scenarios/*.yaml
  migrations/001_init.sql
  app/
    all.py                      # all-in-one runner (BUS=memory)
    common/   config.py logging.py metrics.py clock.py
    bus/      base.py envelope.py topics.py memory.py rabbit.py
    railcore/ models.py infra.py physics.py running_time.py eco.py evaluate.py index.py meetings.py explain.py
    field/    main.py sim.py driver.py interlocking.py incidents.py scenarios.py safety.py
    planner/  main.py model.py strategies.py variants.py conflicts.py refresh.py whatif.py pool.py
    ato/      main.py profiles.py
    recorder/ main.py writer.py
    api/      main.py auth.py ws.py state_cache.py reports.py routes/{infra,trains,plan,incidents,whatif,ato,dc,scenarios,history,reports,settings,system}.py
  web/        (Vite + React + TS)
  tests/      unit/ integration/ e2e/
  tools/      gen_timetable.py loadtest.py
  docs/       architecture.md index-formula.md demo-script.md presentation.md
```

---

## 6. Стек

- Python 3.12, `fastapi`, `uvicorn[standard]`, `pydantic>=2`, `pydantic-settings`, `ortools>=9.10`, `numpy`, `aio-pika`, `asyncpg`, `structlog`, `prometheus-client`, `pyjwt`, `pyyaml`, `weasyprint` (PDF), `pytest`, `pytest-asyncio`, `httpx`, `ruff`.
- Опционально `numba` — только если профилирование покажет, что eco-профиль > 300 мс на поезд.
- Frontend: React 18, TypeScript, Vite, Zustand, ECharts (`echarts`, `echarts-for-react`), Tailwind CSS. Мнемосхема — собственный SVG.
- Инфраструктура: RabbitMQ 3.13 (management), `timescale/timescaledb:latest-pg16`, nginx для статики фронта и прокси `/api`, `/ws`.

---

## 7. Единицы и время (жёсткие правила)

- Внутри домена: метры, секунды, м/с, кг, Н, Дж. км/ч и минуты — только на границах (JSON данных, API, UI).
- `sim_time` — float секунды от `SIM_EPOCH` (из конфига, напр. `2026-10-02T07:55:00+05:00`). В доменной логике **никогда** не использовать wall clock.
- В CP-SAT время — int секунды относительно `now` (момент решения).
- Все id — строки. Номера поездов: `101` (скорый), `341` (пассажирский), `2001` (грузовой). Нечётный номер — нечётное направление.

---

## 8. Доменная модель (`app/railcore/models.py`)

Pydantic v2, `model_config = ConfigDict(frozen=True)` для статики. Реализовать ровно эти сущности (поля можно добавлять, не удалять):

```python
class Direction(StrEnum): ODD = "odd"; EVEN = "even"          # odd = km increasing

class Track(BaseModel):            id: str; length_m: int; main: bool; platform: bool = False
class Station(BaseModel):          id: str; name: str; kind: Literal["station", "siding"]; km: float; tracks: list[Track]
class SpeedZone(BaseModel):        from_m: float; to_m: float; v_kmh: float            # from segment start (odd direction)
class GradeZone(BaseModel):        from_m: float; to_m: float; permille: float        # + = uphill in ODD direction
class Segment(BaseModel):          id: str; from_station: str; to_station: str; length_m: float; v_max_kmh: float
                                   speed_zones: list[SpeedZone] = []; grade_zones: list[GradeZone] = []
class Signal(BaseModel):           id: str; station_id: str; direction: Direction; kind: Literal["entry", "exit"]
class Switch(BaseModel):           id: str; station_id: str; throat: Literal["west", "east"]; track_id: str
class Infra(BaseModel):            stations: list[Station]; segments: list[Segment]; signals: list[Signal]; switches: list[Switch]
                                   # helpers in infra.py: station_order, segment_between, neighbors, km_of(segment, pos, dir)

class TrainCategory(BaseModel):
    id: Literal["express", "passenger", "freight"]; name: str
    mass_t: float; length_m: float; v_max_kmh: float; power_kw: float; f_start_kn: float
    b_service: float                 # m/s^2, service braking
    davis_a: float; davis_b: float; davis_c: float   # w0 = a + b*v + c*v^2, N/kN, v in km/h
    rotating_mass: float = 0.06
    priority_weight: float; min_dwell_s: int; color: str

class TimetableStop(BaseModel):
    station_id: str; arr: float | None; dep: float | None     # sim seconds; None at origin/destination
    stop: bool                                                 # scheduled commercial/technical stop
    min_dwell_s: int = 0

class Train(BaseModel):
    id: str; category: str; direction: Direction; stops: list[TimetableStop]   # every station on route, in order
    v_max_override_kmh: float | None = None; priority_override: float | None = None; cancelled: bool = False

class TrainStatus(StrEnum): WAITING = "waiting"; RUNNING = "running"; DWELLING = "dwelling"; STOPPED = "stopped"; BROKEN = "broken"; FINISHED = "finished"

class TrainState(BaseModel):
    train_id: str; status: TrainStatus
    segment_id: str | None; station_id: str | None; track_id: str | None
    pos_m: float                     # distance from segment start in direction of travel
    km: float; speed_kmh: float; regime: Literal["accel", "cruise", "coast", "brake", "stop"]
    delay_s: float                   # vs current plan (positive = late)
    next_station_id: str | None; energy_kwh: float

class IncidentType(StrEnum):
    TRAIN_FAILURE = "train_failure"; OBSTACLE = "obstacle"; SIGNAL_FAILURE = "signal_failure"
    SEGMENT_CLOSED = "segment_closed"; SPEED_RESTRICTION = "speed_restriction"
    TRAIN_DELAY = "train_delay"; TRACK_CLOSED = "track_closed"

class Incident(BaseModel):
    id: str; type: IncidentType; status: Literal["active", "resolved"]
    segment_id: str | None = None; station_id: str | None = None; train_id: str | None = None; track_id: str | None = None
    km: float | None = None; started_at: float
    est_min_s: int; est_max_s: int; est_expected_s: int          # expected = (min+max)/2 unless given
    params: dict = {}                                             # speed_kmh for restriction, delay_s, rescue_eta_s ...
    description: str
    # actual_s lives ONLY inside field service, never serialized to bus/API

class PlanEntry(BaseModel):
    train_id: str; kind: Literal["run", "dwell"]
    segment_id: str | None; station_id: str | None; track_id: str | None
    start: float; end: float; stop: bool = False       # stop=True for dwell with real stop (scheduled or unplanned)
    unplanned: bool = False

class Meeting(BaseModel):
    kind: Literal["crossing", "overtake"]; station_id: str; time: float
    waiting_train: str; passing_train: str; wait_s: float

class KPI(BaseModel):
    total_delay_s: float; weighted_delay_s: float; delayed_trains: list[tuple[str, float]]
    unplanned_stops: int; energy_kwh: float; energy_ideal_kwh: float
    conflicts: int; throughput: int; planned_throughput: int; arrival_accuracy: float
    robust_total_delay_s: float | None = None          # same order, incidents at max duration

class IndexComponent(BaseModel): key: str; label: str; score: float; weight: float; raw: float; unit: str
class IndexValue(BaseModel):      value: float; category: Literal["norm", "attention", "critical"]; components: list[IndexComponent]

class Plan(BaseModel):
    version: int; base_version: int | None; created_at: float; horizon_end: float
    entries: list[PlanEntry]; meetings: list[Meeting]; kpi: KPI; index: IndexValue
    strategy: str | None = None; solver: Literal["cpsat", "fallback", "refresh"]; solve_ms: int

class Variant(BaseModel):
    id: str; incident_ids: list[str]; base_plan_version: int; strategy: str; title: str
    plan: Plan; delta_index: float; delta_delay_s: float; explanation: list[str]
    status: Literal["proposed", "applied", "stale", "rejected"]

class Modification(BaseModel):
    kind: Literal["train_speed", "incident_duration", "train_priority", "departure_shift",
                  "add_train", "cancel_train", "segment_speed_limit", "close_segment"]
    target_id: str | None = None; value: float | None = None; params: dict = {}

class WhatIfRequest(BaseModel):  id: str; base_plan_version: int; modifications: list[Modification]
class WhatIfResult(BaseModel):
    request_id: str; plan: Plan; delta_index: float
    per_train: list[dict]            # {train_id, arr_delta_s, final_delay_s}
    changed_meetings: list[Meeting]; explanation: list[str]

class ProfilePoint(BaseModel): s_m: float; v_kmh: float; regime: Literal["accel", "cruise", "coast", "brake"]
class SpeedProfile(BaseModel):
    train_id: str; plan_version: int; segment_id: str; t_target_s: float; t_profile_s: float
    energy_kwh: float; energy_min_time_kwh: float; saving_pct: float; points: list[ProfilePoint]

class Route(BaseModel):
    id: str; station_id: str; train_id: str | None; direction: Direction
    kind: Literal["arrival", "departure", "through"]; track_id: str
    segment_id: str | None; status: Literal["set", "occupied", "released"]; set_by: Literal["auto", "manual"]
```

---

## 9. Данные участка (`data/`)

Все названия вымышленные. Реальные данные не используем.

### 9.1 `infra.json` — 6 раздельных пунктов, 5 однопутных перегонов

| id | Название | Тип | км | Пути (id: длина м, *главный*, P — платформа) |
|---|---|---|---|---|
| `SEV` | Ст. Северная | station | 0 | I: 1200 *гл* P; 3: 1050 P; 4: 1050; 5: 850 |
| `R1` | Рзд. 1 | siding | 15 | I: 1100 *гл*; 2: 1050 |
| `STP` | Ст. Степная | station | 33 | I: 1200 *гл* P; 3: 1050 P; 4: 900 |
| `R2` | Рзд. 2 | siding | 49 | I: 1100 *гл*; 2: 1050 |
| `OZR` | Ст. Озёрная | station | 66 | I: 1200 *гл* P; 3: 1050 P |
| `YUZ` | Ст. Южная | station | 84 | I: 1200 *гл* P; 3: 1050 P; 4: 1050; 5: 850 |

| id | Между | Длина | v_max | Зоны |
|---|---|---|---|---|
| `SEV-R1` | SEV–R1 | 15 000 | 120 | — |
| `R1-STP` | R1–STP | 18 000 | 120 | кривая 70 км/ч на 9 000–11 000 |
| `STP-R2` | STP–R2 | 16 000 | 100 | подъём +8‰ (нечётн.) на 4 000–10 000 |
| `R2-OZR` | R2–OZR | 17 000 | 120 | — |
| `OZR-YUZ` | OZR–YUZ | 18 000 | 120 | спуск −5‰ на 6 000–12 000 |

Сигналы: на каждом пункте входной и выходной на каждое направление (`{station}-{dir}-{entry|exit}`). Стрелки: на каждый боковой путь по одной в западной и восточной горловине (`{station}-{track}-{W|E}`).

### 9.2 `rolling_stock.json`

| id | масса т | длина м | v_max | P кВт | F_пуск кН | b м/с² | a | b | c | вес приоритета | мин. стоянка с | цвет |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| express | 500 | 300 | 140 | 5000 | 250 | 0.7 | 0.9 | 0.008 | 0.00022 | 5 | 60 | `#a78bfa` |
| passenger | 700 | 450 | 120 | 4000 | 300 | 0.6 | 1.0 | 0.010 | 0.00025 | 3 | 60 | `#60a5fa` |
| freight | 4500 | 900 | 80 | 6000 | 500 | 0.3 | 0.9 | 0.007 | 0.00012 | 1 | 0 | `#f59e0b` |

Важно: грузовой 900 м не помещается на пути 850 м — проверка длины пути реальна. Подъём 8‰ режет грузовой до ~48 км/ч — физика заметна на поездограмме.

### 9.3 `timetable.json`

Генерируется `tools/gen_timetable.py` из списка ниже: время проследования = минимальное время хода (раздел 10) + 5% резерва; стоянки по категории. Расписание — желаемые времена, оно **может** содержать конфликты; первый план (v1) их разрешает.

| Поезд | Кат. | Направление | Отправление | Стоянки |
|---|---|---|---|---|
| 2002 | freight | even (YUZ→SEV) | 08:00 | — |
| 2001 | freight | odd (SEV→YUZ) | 08:05 | — |
| 101 | express | odd | 08:10 | STP 2 мин |
| 102 | express | even | 08:20 | STP 2 мин |
| 341 | passenger | odd | 08:30 | все станции 1 мин (разъезды — нет) |
| 342 | passenger | even | 08:45 | все станции 1 мин |
| 2003 | freight | odd | 08:50 | — |
| 2004 | freight | even | 09:00 | — |
| 2005 | freight | odd | 09:30 | — |
| 2006 | freight | even | 09:40 | — |

`SIM_EPOCH` = 07:55. Демо-скорость по умолчанию ×20 (2 часа ≈ 6 минут).

### 9.4 `settings.default.json` (редактируется админом, хранится в БД `settings`)

```json
{
  "index": {
    "weights": {"schedule": 0.30, "capacity": 0.20, "energy": 0.15, "conflicts": 0.20, "accuracy": 0.15},
    "thresholds": {"norm": 80, "attention": 60},
    "refs": {"delay_ref_min": 15, "conflicts_ref": 3, "accuracy_window_s": 120}
  },
  "priority_weights": {"express": 5, "passenger": 3, "freight": 1},
  "planner": {"time_limit_s": 1.5, "workers": 3, "horizon_s": 7200, "debounce_ms": 300,
              "segment_clear_s": 60, "lambda_stop": 120, "lambda_side_track": 30,
              "replan_deviation_s": 120, "rescue_eta_s": 1800},
  "sim": {"speed": 20, "tick_s": 0.5, "auto_routes": true, "ato_compliance": 1.0},
  "station": {"side_track_kmh": 40, "pass_clear_m": 300, "ready_before_dep_s": 300, "leave_after_arr_s": 300}
}
```

### 9.5 `scenarios/*.yaml`

```yaml
id: cow_on_segment
title: "Скот на перегоне Рзд. 1 — Степная"
steps:
  - at: "+00:05:00"          # sim offset from scenario start
    incident: {type: obstacle, segment_id: R1-STP, km: 24.5, est_min_min: 15, est_max_min: 30, actual_min: 22,
               description: "Скот на пути, бригада выехала"}
  - at: "+00:15:00"
    update: {est_min_min: 5, est_max_min: 10, description: "Бригада на месте"}
```

Сценарии: `cow_on_segment`, `train_failure` (2003 встаёт на `STP-R2` км 40, 20–45 мин, факт 38, `rescue_eta_s: 1800`), `signal_failure` (выходной STP чётный, 40 мин: проезд по пригласительному +180 с и 20 км/ч), `segment_closed` (`R2-OZR`, 30 мин, окно работ), `speed_restriction` (`SEV-R1` 40 км/ч на 3–6 км), `mass_incidents` (8 сбоев за 2 мин симуляции), `demo_full` (сценарий защиты, раздел 18).

---

## 10. Физика и время хода (`railcore/physics.py`, `running_time.py`)

Дискретизация по пути, шаг `ds = 25 м` (в eco-переборе 50 м).

- Вес: `W_kN = mass_t * 9.81`. Масса: `m = mass_t * 1000`, эффективная `m_eff = m * (1 + rotating_mass)`.
- Основное удельное сопротивление: `w0(v) = a + b*v + c*v²` [Н/кН], `v` в км/ч.
- Уклон: `wi = ±permille` [Н/кН] (знак по направлению движения: `+` в подъём).
- Сопротивление: `R = (w0 + wi) * W_kN` [Н].
- Тяга: `F(v) = min(F_start, P / max(v, 1 м/с))` [Н], `P` в Вт.
- Ускорения: тяга `(F − R)/m_eff`; выбег `−R/m_eff`; торможение `−b_service` (константа).
- Шаг: `v₂² = v₁² + 2·a·ds` (не ниже 0), `dt = 2·ds/(v₁+v₂)`.
- Энергия тяги: `Σ F·ds` в фазах тяги и удержания скорости (удержание: `F = max(R, 0)`), выбег и торможение — 0. Рекуперацию не считаем.

**Минимальный профиль** (классический алгоритм forward/backward):
1. `v_lim[s] = min(зона скорости, v_max перегона, v_max поезда, override, временные ограничения)`.
2. Прямой проход: разгон на максимальной тяге от `v_in`, не выше `v_lim`.
3. Обратный проход: кривая торможения от `v_out`, не выше `v_lim`.
4. `v = min(прямой, обратный)`, время `Σ dt`, режимы помечаются по тому, какая кривая активна.

`v_in/v_out`: 0, если стоянка в начале/конце; иначе скорость проследования (главный путь — `v_lim` перегона на границе).

**Таблица времён хода** (кэш `functools.lru_cache` по ключу `(category, v_max_override, segment_id, direction, restrictions_hash)`):
- `t_pp` — без остановок на концах;
- `sup_start = t(стоп, проход) − t_pp` — надбавка на разгон;
- `sup_end = t(проход, стоп) − t_pp` — надбавка на замедление;
- энергии для тех же 4 комбинаций.

Остаток пути для поезда на перегоне: тот же расчёт от текущих `pos_m` и `speed`.

Тесты: время на ровном перегоне без ограничений сходится с аналитикой ±2%; тормозной путь `v²/2b` ±2%; грузовой на подъёме 8‰ уравновешивается около 45–52 км/ч.

---

## 11. Симулятор поля и ДЦ (`app/field`)

### 11.1 Цикл

- Тик каждые `tick_s = 0.5 с` реального времени; за тик `sim_dt = tick_s * speed`; внутри интегрирование подшагами по 1 с симуляции.
- После тика публикуется `field.state` (полный снимок): все `TrainState`, сигналы (аспект), стрелки (положение, замкнута), маршруты, занятость перегонов и путей, активные сбои (без `actual_s`), `sim_time`, `speed`, `paused`.
- Управление временем: пауза, скорость ×1/×10/×20/×60 (команда `cmd.field.clock`).

### 11.2 Поведение поезда (`driver.py`)

- Поезд в `WAITING` появляется на пути станции отправления за `ready_before_dep_s` до отправления.
- Отправляется, когда: `sim_time ≥ dep` по **текущему плану** и выходной сигнал открыт (маршрут отправления задан).
- На перегоне: целевая скорость = `min(v_lim(s), профиль ATO при наличии × compliance)`; если входной сигнал следующего пункта закрыт — торможение до остановки у границы перегона; если впереди препятствие — торможение до остановки за 200 м.
- Прибытие на путь станции по маршруту прибытия; стоянка ≥ `min_dwell_s` при плановой остановке; дальше по плану.
- `FINISHED` через `leave_after_arr_s` после прибытия на конечную.
- Отклонение от плана `delay_s` = фактическое время прохождения последней контрольной точки − плановое.

### 11.3 Эмуляция ДЦ (`interlocking.py`)

Маршрут задаётся, только если **все** условия верны:
- целевой путь свободен, не закрыт и не входит в другой маршрут;
- длина пути ≥ длины поезда;
- для отправления: перегон свободен и на нём нет маршрута встречного направления (ПАБ), нет активного сбоя, блокирующего перегон;
- стрелки маршрута не замкнуты другим маршрутом.

При задании: стрелки переводятся (`plus` для главного, `minus` для бокового), замыкаются, сигнал открывается (`green`, если следующий участок свободен, иначе `yellow`). Размыкание — когда хвост поезда прошёл горловину (упрощение: при прибытии на путь / отправлении на перегон). Отказ сигнала → аспект `invitation`, проезд по нему со скоростью 20 км/ч и надбавкой 180 с.

**Автозадание маршрутов** (`auto_routes=true`): по утверждённому плану маршрут прибытия задаётся, когда поезд вошёл на предыдущий перегон; маршрут отправления — за 60 с до планового отправления, если поезд первый в плановом порядке на этом перегоне. Ручные команды диспетчера имеют приоритет. Отказ ДЦ задать маршрут всегда побеждает план (безопасность выше плана), причина пишется в `dc.log`.

Команды: `set_route`, `cancel_route`, `set_auto`, `resolve_incident`, `update_incident_estimate`, `create_incident`, `run_scenario`, `clock`. Каждая команда → `dc.command_result {ok, reason}` + запись в `dc.log` (кто, что, когда, результат).

### 11.4 Сбои (`incidents.py`)

| Тип | Эффект в симуляторе |
|---|---|
| `train_failure` | Поезд тормозит до 0, статус `BROKEN`, стоит `actual_s`, перегон занят |
| `obstacle` | Перегон закрыт для новых маршрутов; поезд на перегоне останавливается перед препятствием до снятия |
| `segment_closed` | Перегон закрыт на окно |
| `signal_failure` | Сигнал → `invitation`; проезд 20 км/ч, +180 с |
| `speed_restriction` | Ограничение скорости на отрезке перегона |
| `train_delay` | Поезд задерживается на станции на `params.delay_s` |
| `track_closed` | Путь станции исключён из маршрутов |

Фактическая длительность `actual_s` берётся из сценария или равномерно из `[est_min, est_max]` и **никогда** не уходит в шину. Снятие: по истечении `actual_s`, кнопкой диспетчера или шагом сценария. Вариант «вспомогательный локомотив», если утверждён, заменяет `actual_s` на `rescue_eta_s + время вывода`.

### 11.5 Монитор безопасности (`safety.py`)

Каждый тик: на однопутном перегоне > 1 поезда; на пути станции > 1 поезда; поезд проехал закрытый сигнал (кроме пригласительного). Нарушение → `safety.violation` + лог `CRITICAL` + метрика. Цель — 0 за всё демо, счётчик в UI.

---

## 12. Планировщик (`app/planner`)

### 12.1 Триггеры

| Событие | Действие |
|---|---|
| Старт | Построить план v1 из расписания (стратегия `balanced`), опубликовать `plan.approved` |
| `incident.created/updated/resolved` | Debounce `debounce_ms`, затем варианты (12.4) → `planner.variants` |
| Отклонение поезда > `replan_deviation_s` без смены порядка | Refresh: пересчёт времён при фиксированном порядке (12.6) → `plan.refreshed` автоматически |
| Отклонение, меняющее порядок | Варианты → диспетчеру |
| `cmd.planner.whatif` | Песочница (12.7) → `planner.whatif.result` |
| `cmd.planner.apply` | Проверить `base_plan_version == current`; иначе вариант `stale`, ответ с причиной; при успехе → новая версия, `plan.approved` |
| Каждую 1 с | Индекс текущего прогноза → `kpi.index`; прогноз конфликтов (12.5) → `planner.conflicts` |

### 12.2 Модель CP-SAT (`model.py`) — ядро

Строится от «снимка сейчас»: состояние поля + активные сбои + переопределения what-if. Время — int секунды от `now`, горизонт `H = horizon_s`, верхняя граница переменных `UB = H + 7200`. Поезда: все не `FINISHED` и не отменённые, с отправлением ≤ `now + H`.

Для каждого поезда `j` и каждого пункта `i` его маршрута:
- `arr[j,i]`, `dep[j,i]` — IntVar.
- `dwell[j,i] = dep − arr`:
  - плановая остановка: `dwell ≥ min_dwell_s`;
  - проход: `dwell ≥ t_pass[j]`, где `t_pass = (length_m + pass_clear_m) / v_проследования`;
  - `stop[j,i]` — BoolVar: `dwell ≤ t_pass + UB·stop`; для плановых остановок `stop = 1`.
- Отправление: пассажирские и скорые `dep ≥ sched_dep` на плановых остановках (раньше расписания не уходим); грузовые — без ограничения снизу, кроме станции отправления.
- Станция отправления: `arr = max(now, sched_dep − ready_before_dep_s)` фиксировано; конечная: `dep = arr + leave_after_arr_s`.

Для каждого перегона `k` маршрута поезда (от пункта `i` к `i+1`):
- `run[j,k]` — интервал `[dep[j,i], arr[j,i+1]]`, длительность `≥ t_pp + sup_start·stop[j,i] + sup_end·stop[j,i+1]` и `≤ 1.3 · (t_pp + sup_start + sup_end)` (медленный ход разрешён — так план сам «замедляет» поезд вместо остановки).
- `block[j,k]` — интервал `[dep[j,i], arr[j,i+1] + segment_clear_s]` (время на освобождение перегона и смену маршрута).

Ресурсы:
- **Перегон** (ПАБ, однопутка): `AddNoOverlap(block[*,k])` по всем поездам обоих направлений + фиксированные интервалы сбоев на этом перегоне.
- **Путь станции**: для каждого `(j,i)` и каждого допустимого пути `t` (длина ≥ длины поезда, путь не закрыт) — `present[j,i,t]` и `NewOptionalIntervalVar(arr, dwell, dep, present)`; `AddExactlyOne(present[j,i,*])`; `AddNoOverlap` по каждому пути.
- Боковой путь при проходе: `stop[j,i] ≥ present[j,i,side]` (по боковому без остановки не ходим).
- Пассажирские плановые остановки — только пути с платформой.

Текущее состояние:
- Поезд на перегоне: его первый `run` начинается в `0`, длительность ≥ остатка хода (физика от текущих `pos_m`, `speed`).
- Поезд стоит на станции: `arr` = `0`, путь фиксирован (`present` = 1 для текущего).
- Поезд `BROKEN`: остаток хода + длительность ремонта по сценарию стратегии.
- Прошлое фиксировано и в модель не входит.

Сбои → ограничения (длительность `d` берётся по стратегии: `expected` / `max` / `rescue`):

| Тип | В модели |
|---|---|
| `obstacle`, `segment_closed` | `NewFixedSizeIntervalVar(start_rel, d)` в NoOverlap перегона |
| `train_failure` | Длительность текущего `run` поезда `+ d` |
| `signal_failure` | Для поездов через пункт в нужном направлении: `dwell ≥ 180` и `stop = 1` на время сбоя |
| `speed_restriction` | Пересчёт `t_pp/sup_*` с ограничением для поездов, входящих в окно |
| `train_delay` | `dep ≥ now + delay_s` |
| `track_closed` | Путь исключён из `present` |

Целевая функция (минимизация):

```
Σ_j w_j · Σ_{i ∈ плановые остановки и конечная} late[j,i]        # late ≥ arr − sched_arr, late ≥ 0
+ λ_stop · Σ (stop[j,i] для непланово остановившихся)             # энергия и комфорт
+ λ_side · Σ present[j,i,side] для поездов без плановой остановки
+ ε · Σ arr[j, конечная]                                          # прижимает расписание, ε = 0.01
```

`w_j` — вес приоритета категории (или override). Задержку грузовых тоже считаем, с весом 1.

Решатель: `max_time_in_seconds = time_limit_s`, `num_workers = 4`, `AddHint` из текущего плана (warm start), `random_seed` фиксирован. Решения `OPTIMAL` или `FEASIBLE` принимаются; при `UNKNOWN/INFEASIBLE` — fallback (12.6) с пометкой `solver="fallback"` и текстом «Решатель не успел — показана протяжка задержек без смены порядка».

Из решения строится `Plan`: `entries` (run/dwell с путём), `meetings` (12.8), `kpi`, `index`.

### 12.3 Стратегии (`strategies.py`)

| id | Заголовок | Отличие |
|---|---|---|
| `balanced` | Минимум задержек | Базовые веса, длительность сбоя `expected` |
| `passenger_first` | Приоритет пассажирских | Веса пассажирских и скорых ×3, грузовых ×0.5 |
| `robust` | Устойчивый план | Длительность сбоя `max` |
| `fewer_stops` | Меньше остановок | `λ_stop × 4` (используется, если нет `train_failure`) |
| `rescue` | Вспомогательный локомотив | Только для `train_failure`, если `est_max > rescue_eta_s`: длительность = `rescue_eta_s + время вывода` |

Набор вариантов: `balanced` + `robust` + (`rescue` если применим, иначе `passenger_first`). Всегда ровно 3 карточки.

### 12.4 Генерация и оценка вариантов (`variants.py`, `pool.py`)

1. Собрать снимок, построить 3 задачи.
2. Решить параллельно в `ProcessPoolExecutor(max_workers=workers)` (модель сериализуется как входные данные, а не как объект CP-SAT).
3. Для каждого плана: `evaluate` при `expected` и при `max` (поле `robust_total_delay_s`), индекс, дельта к текущему прогнозу, объяснение.
4. Опубликовать `planner.variants` с `solve_ms`. Бюджет всего шага ≤ 5 с (обычно 2–3 с).

### 12.5 Оценка фиксированного порядка — «альтернативный граф» (`railcore/evaluate.py`)

Ключевая функция, используется в трёх местах: refresh, fallback, устойчивость.

Вход: порядок поездов на каждом перегоне и пути станции (из плана), времена хода, сбои с заданной длительностью. Строится граф событий (вершины — `arr/dep` поездов на пунктах), рёбра с весами:
- `dep[j,i] → arr[j,i+1]`: время хода (с надбавками);
- `arr → dep`: стоянка;
- порядок на перегоне: `arr[a, конец] + clear → dep[b, начало]` для следующего поезда `b`;
- порядок на пути станции: `dep[a] → arr[b]`;
- ограничения снизу: расписание, `now`, окна сбоев.

Самые ранние времена = длиннейший путь в DAG (топологическая сортировка). Цикл в графе = **дедлок** → вернуть ошибку с поездами цикла (в UI красным). Результат — `Plan` с KPI.

### 12.6 Refresh и fallback (`refresh.py`)

- Refresh: при отклонениях без смены порядка — `evaluate(текущий порядок)`, новая версия с `solver="refresh"`, автоматически.
- Fallback: если CP-SAT не нашёл решение — `evaluate(текущий порядок)` с новыми сбоями.

### 12.7 Прогноз конфликтов — CDR (`conflicts.py`)

Каждую секунду: «свободный прогноз» каждого поезда (поезд едет по расписанию/плану без учёта других поездов, с текущим отставанием и сбоями) → проверка пересечений на перегонах и путях. Конфликт = `{resource_id, time_from, time_to, trains[], kind: head_on|following|track}`. Число конфликтов — фактор индекса; на поездограмме — красные маркеры **до** того, как конфликт случился.

### 12.8 What-if (`whatif.py`)

1. Копия снимка; применить `modifications` (скорость → `v_max_override` и пересчёт таблицы времён; длительность сбоя; приоритет; сдвиг отправления; добавить/отменить поезд; ограничение/закрытие перегона).
2. Одно решение CP-SAT, стратегия `balanced`, warm start от текущего плана.
3. Diff с текущим планом: `per_train` (изменение прибытия на конечную, итоговая задержка), изменённые скрещения, `delta_index`.
4. Текущий план не меняется. Кнопка «Перенести в работу» делает из результата вариант со статусом `proposed`, дальше обычное `apply`.

Пример из демо: «2003 идёт 60 км/ч вместо 80» → поездограмма показывает, где поменяются скрещения и сколько потеряют встречные.

### 12.9 Скрещения и обгоны (`railcore/meetings.py`)

Из плана: два поезда встречных направлений одновременно на одном пункте = скрещение; ждущий — тот, у кого незапланированная стоянка (или дольше стоит). Попутные, у которых меняется порядок на пункте, = обгон. `wait_s` — непланированная часть стоянки.

### 12.10 Объяснения (`railcore/explain.py`)

Шаблоны, на русском, из данных плана, 2–4 предложения:
- «Грузовой 2003 ждёт на Рзд. 1 12 мин, пропуская пассажирский 341.»
- «Суммарная задержка 34 мин (−18 мин к «Устойчивому»), опаздывают 3 поезда, больше всех — 2004 (+15 мин).»
- «Если ремонт займёт максимум (45 мин): +9 мин.»
- «Индекс 74 → «Внимание» (−8 от текущего прогноза).»

---

## 13. Автоведение (`app/ato/profiles.py`, `railcore/eco.py`)

По каждому поезду и каждому перегону впереди по утверждённому плану.

**Целевое время (Connected DAS):**
- `t_target = (последнее допустимое прибытие) − dep_из_начала`;
- последнее допустимое прибытие = `min(sched_arr при плановой остановке пассажирского/скорого, plan_dep_след − min_dwell)` и не раньше планового прибытия.
- Смысл: если план держит поезд на разъезде ради скрещения, поезд приезжает к моменту отправления, а не стоит.

**Алгоритм eco-профиля** (оптимальная структура режимов: разгон → ход → накат → торможение):
1. `T_min` = время минимального профиля. Если `t_target ≤ T_min` — вернуть минимальный профиль, экономия 0.
2. Для `v_c` от `v_lim_max` вниз до 30 км/ч шагом 5 км/ч:
   - профиль `P(v_c, s_c)`: разгон до `min(v_lim, v_c)` до точки `s_c`, далее выбег (на спуске с удержанием `v_lim`), в конце — кривая торможения до `v_out`;
   - если `time(P(v_c, L)) > t_target` — стоп перебора;
   - бисекция `s_c ∈ [0, L]`, 12 итераций, чтобы `time ≈ t_target` (±2 с);
   - запомнить энергию.
3. Вернуть профиль с минимальной энергией, точки каждые 100 м с режимом, `saving_pct` относительно минимального профиля.

Производительность: пересчитывать только поезда, у которых изменились времена в плане; шаг 50 м; цель < 300 мс на поезд. Публикация `ato.profiles` по каждому поезду.

---

## 14. Индекс качества движения (`railcore/index.py`)

```
I = 100 · Σ wᵢ · sᵢ,   Σ wᵢ = 1,   sᵢ ∈ [0, 1]
```

| key | Фактор | Формула sᵢ | raw (показываем) |
|---|---|---|---|
| `schedule` | Соблюдение расписания | `1 − min(1, взвешенная средняя задержка / delay_ref)` | средняя задержка, мин |
| `capacity` | Использование пропускной способности | `min(1, поездов проследует за горизонт / по расписанию)` | поездов/ч |
| `energy` | Энергоэффективность ведения | `min(1, energy_ideal / energy_plan)` | кВт·ч |
| `conflicts` | Конфликты на скрещениях и обгонах | `1 − min(1, (конфликты прогноза + вынужденные остановки) / conflicts_ref)` | шт |
| `accuracy` | Точность прибытия и остановки | доля прибытий пассажирских в окне ±`accuracy_window_s` | % |

Категории: `≥ norm` — «Норма», `≥ attention` — «Внимание», ниже — «Критично». Индекс считается для текущего прогноза (факт + план), каждого варианта и каждого what-if. Формула и веса — в `docs/index-formula.md` и на экране настроек. Изменение весов через API применяется без перезапуска (планировщик подписан на `settings.updated`).

---

## 15. Шина событий (`app/bus`)

### 15.1 Интерфейс

```python
class EventBus(Protocol):
    async def publish(self, topic: str, payload: BaseModel | dict, *, corr_id: str | None = None) -> None: ...
    async def subscribe(self, pattern: str, handler: Callable[[Envelope], Awaitable[None]]) -> None: ...   # '*' one word, '#' many
    async def start(self) -> None: ...
    async def close(self) -> None: ...
```

Реализации: `MemoryBus` (asyncio, совпадение паттернов как в AMQP topic) и `RabbitBus` (aio-pika, один topic exchange `rail`, у каждого сервиса своя эксклюзивная очередь для событий и именованная durable-очередь для команд `cmd.<service>`). Выбор по `BUS`. Код сервисов от реализации не зависит.

### 15.2 Конверт

```json
{"id": "uuid", "type": "planner.variants", "source": "planner", "corr_id": "uuid|null",
 "ts_wall": 1727771234.123, "sim_time": 4523.0, "schema": 1, "payload": {}}
```

`ts_wall` ставит отправитель — по нему фронт меряет задержку.

### 15.3 Топики

| Топик | Отправитель | Частота | Payload |
|---|---|---|---|
| `field.state` | field | 2 Гц | снимок поля |
| `field.train_event` | field | по событию | `{train_id, kind: departed/arrived/passed/stopped_unplanned/finished, station_id, time}` |
| `incident.created` / `.updated` / `.resolved` | field | по событию | `Incident` |
| `dc.command_result`, `dc.log` | field | по событию | результат и журнал |
| `safety.violation` | field | по событию | описание нарушения |
| `plan.approved`, `plan.refreshed` | planner | по событию | `Plan` |
| `planner.variants` | planner | по событию | `{incident_ids, base_plan_version, variants: [Variant]}` |
| `planner.conflicts` | planner | 1 Гц | `[Conflict]` |
| `planner.whatif.result` | planner | по запросу | `WhatIfResult` (`corr_id` = id запроса) |
| `kpi.index` | planner | 1 Гц | `IndexValue` + `KPI` |
| `planner.metrics` | planner | по событию | `{solve_ms, strategy, status}` |
| `ato.profiles` | ato | по событию | `[SpeedProfile]` |
| `settings.updated` | api | по событию | настройки |
| `cmd.field.*` | api | — | `set_route`, `cancel_route`, `set_auto`, `create_incident`, `resolve_incident`, `update_incident_estimate`, `run_scenario`, `clock` |
| `cmd.planner.*` | api | — | `apply`, `whatif`, `promote_whatif`, `reject` |
| `cmd.ato.*` | api | — | `recompute` |

### 15.4 Правила

- Состояние — полными снимками, не дельтами. Потеря сообщения не ломает систему.
- План версионируется; `apply` с устаревшей версией отклоняется.
- Сбои склеиваются (debounce): за окно `debounce_ms` — одно решение со всеми.
- Обработчики идемпотентны по `id` конверта.
- Сообщения валидируются pydantic на входе; невалидные — в лог `ERROR`, не падаем.

---

## 16. API gateway (`app/api`)

### 16.1 Авторизация

`POST /api/v1/auth/login {login, password}` → JWT HS256, 12 ч, `role ∈ {dispatcher, admin}`. Пользователи из env. Все эндпоинты, кроме `/health`, `/metrics` и `/docs`, требуют токен; `PUT /settings` — только `admin`. WS: `/ws?token=...`.

### 16.2 REST `/api/v1`

| Метод | Путь | Назначение |
|---|---|---|
| GET | `/infra` | Топология, категории |
| GET | `/trains` | Расписание и текущее состояние |
| GET | `/plan/current` | Текущий план |
| GET | `/plan/variants` | Активные предложения |
| POST | `/plan/apply` | `{variant_id, base_plan_version}`; 409 если устарел |
| POST | `/plan/variants/{id}/reject` | Отклонить |
| GET / POST | `/incidents` | Список / создать вручную |
| POST | `/incidents/{id}/resolve` | Снять |
| POST | `/incidents/{id}/estimate` | Уточнить диапазон |
| POST | `/whatif` | `{modifications}` → `{request_id}`; результат по WS и `GET /whatif/{id}` |
| POST | `/whatif/{id}/promote` | В варианты |
| GET | `/ato/{train_id}` | Профили поезда |
| GET / POST / DELETE | `/dc/routes` | Маршруты |
| POST | `/dc/auto` | Автозадание вкл/выкл |
| GET | `/dc/log` | Журнал ДЦ |
| GET / POST | `/scenarios`, `/scenarios/{id}/run` | Сценарии |
| POST | `/sim/clock` | `{paused, speed}` |
| GET | `/history/frames?from&to&step` | Кадры для перемотки (поезда, индекс, план на момент) |
| GET | `/history/events?from&to` | События |
| GET | `/reports?from&to&format=csv\|pdf` | Отчёт |
| GET / PUT | `/settings` | Настройки |
| GET | `/system/metrics` | Метрики для UI: p95 решения, задержка, нарушения безопасности |
| GET | `/health`, `/metrics` | Проверка, Prometheus |

OpenAPI генерирует FastAPI (`/docs`); у каждого эндпоинта `summary` на русском и модели ответа.

### 16.3 WebSocket

- Сервер → клиент: конверты шины, отфильтрованные по подписке. При подключении сразу отправляется **snapshot** из `state_cache`: последний `field.state`, текущий план, варианты, индекс, конфликты, профили, активные сбои.
- Клиент → сервер: `{"op": "subscribe", "channels": [...]}`, `{"op": "ping", "t": ...}`, `{"op": "latency", "p95_ms": ...}` (фронт отчитывается о задержке рендера).
- Conflation: для `field.state` клиенту отправляется только последний снимок, если предыдущий ещё не ушёл (без очередей).

### 16.4 Отчёт

CSV: события, принятые решения (вариант, стратегия, Δиндекс), задержки по поездам, индекс по минутам. PDF (WeasyPrint из HTML-шаблона): период, итоговый индекс и разбивка, список сбоев и решений, таблица задержек, формула индекса.

---

## 17. Хранилище (`migrations/001_init.sql`, `app/recorder`)

```sql
CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE TABLE IF NOT EXISTS train_state (ts timestamptz NOT NULL, sim_time double precision, train_id text,
  status text, segment_id text, station_id text, track_id text, km double precision, speed_kmh double precision,
  delay_s double precision, regime text, energy_kwh double precision);
SELECT create_hypertable('train_state', 'ts', if_not_exists => TRUE);
CREATE TABLE IF NOT EXISTS events (ts timestamptz NOT NULL, sim_time double precision, id uuid, type text, source text, payload jsonb);
SELECT create_hypertable('events', 'ts', if_not_exists => TRUE);
CREATE TABLE IF NOT EXISTS kpi (ts timestamptz NOT NULL, sim_time double precision, value double precision, category text, components jsonb);
SELECT create_hypertable('kpi', 'ts', if_not_exists => TRUE);
CREATE TABLE IF NOT EXISTS plans (version int PRIMARY KEY, created_at timestamptz, sim_time double precision,
  solver text, strategy text, index_value double precision, payload jsonb);
CREATE TABLE IF NOT EXISTS variants (id text PRIMARY KEY, created_at timestamptz, base_plan_version int,
  strategy text, status text, index_value double precision, payload jsonb);
CREATE TABLE IF NOT EXISTS incidents (id text PRIMARY KEY, type text, status text, payload jsonb, updated_at timestamptz);
CREATE TABLE IF NOT EXISTS dc_log (ts timestamptz NOT NULL, sim_time double precision, actor text, command jsonb, ok boolean, reason text);
CREATE TABLE IF NOT EXISTS settings (key text PRIMARY KEY, value jsonb, updated_at timestamptz, updated_by text);
SELECT add_retention_policy('train_state', INTERVAL '72 hours', if_not_exists => TRUE);
SELECT add_retention_policy('events', INTERVAL '72 hours', if_not_exists => TRUE);
SELECT add_retention_policy('kpi', INTERVAL '72 hours', if_not_exists => TRUE);
```

Recorder: `field.state` пишет с прореживанием до 1 Гц через `COPY`/`executemany` батчами раз в секунду; остальные события — все. Миграции применяет recorder при старте. Без `DATABASE_URL` recorder не запускается, а API хранит кадры за 20 минут в кольцевом буфере.

---

## 18. Frontend (`web/`)

Тёмная тема (диспетчерская), основной экран 1920×1080, корректно на 1440×900. Всё на одном экране, без переходов.

### 18.1 Раскладка

```
┌ TopBar: часы симуляции [⏸ ×1 ×10 ×20 ×60] · ИНДЕКС 82 Норма · сбои 1 · p95 UI 120 мс · план 1.8 с · безопасность 0 · роль ┐
├ Мнемосхема ДЦ (во всю ширину, ~22% высоты)                                                                                 ┤
├ Поездограмма (~48%)                                                         │ Правая панель (380 px):                      ┤
│                                                                              │  Решения / What-if / Автоведение / Журнал    │
├ Индекс: 5 факторов + график за 60 мин   │ Сценарии (кнопки)   │ Перемотка   ┤
```

### 18.2 Компоненты

- **TopBar** — часы, управление скоростью, индекс с цветом категории, счётчики, живые метрики, статус соединения.
- **Mnemo (SVG)** — пункты слева направо по км; пути станций параллельными линиями с номерами; перегоны одной линией; сигналы — кружки (зелёный/жёлтый/красный/пригласительный — белый мигающий); стрелки — короткие отрезки с положением; занятые перегоны и пути подсвечены; поезда — прямоугольники с номером и цветом категории, стрелка направления; сбои — иконки (Tabler) на месте. Клик по станции → панель маршрутов (задать/отменить, автозадание); клик по поезду → выбор поезда для автоведения и what-if.
- **TrainGraph (ECharts)** — X: время от «сейчас − 30 мин» до «сейчас + 120 мин»; Y: км с подписями пунктов и горизонтальными линиями. Факт — сплошная линия, прогноз плана — пунктир, цвет по категории; вертикаль «сейчас»; сбои — заштрихованные прямоугольники (перегон × время); конфликты прогноза — красные маркеры с подсказкой; «призрак» варианта/what-if — полупрозрачные линии с подсветкой изменённых скрещений. Масштаб колесом, сдвиг перетаскиванием.
- **VariantCards** — 3 карточки: заголовок стратегии, бейдж «рекомендуем» у лучшего индекса, Δиндекс (цветом), суммарная задержка, опоздавшие поезда (топ-3), незапланированные остановки, энергия, строка устойчивости, объяснение, время решения; кнопки «Показать на графике» (hover тоже) и «Применить» (подтверждение). Клавиши `1/2/3` — выбрать, `Enter` — применить, `Esc` — снять выбор. Устаревший вариант — серый с надписью и кнопкой «Пересчитать».
- **WhatIfPanel** — выбранный поезд; слайдер скорости (20 — v_max категории); выбор сбоя и слайдер длительности; приоритет; сдвиг отправления; отменить/добавить поезд; ограничение/закрытие перегона. Кнопка «Рассчитать» → карточка результата: Δиндекс, изменения прибытия по поездам, изменённые скрещения, «призрак» на графике, кнопка «Перенести в работу».
- **AtoChart** — для выбранного поезда и текущего/следующего перегона: X — путь (км), Y — скорость; ступенчатая линия ограничений; eco-профиль, раскрашенный по режимам (разгон/ход/накат/торможение); минимальный профиль — серый пунктир; карточка: целевое время, прогноз прибытия, экономия энергии %, кВт·ч.
- **IndexPanel** — 5 горизонтальных полос факторов (оценка, вес, сырое значение с единицами), мини-график индекса за 60 мин, клик → модалка с формулой.
- **EventLog** — объединённая лента: сбои, решения, команды ДЦ, нарушения безопасности; фильтр.
- **ScenarioBar** — кнопки сценариев.
- **ReplayBar** — переключатель Live/Перемотка; ползунок за последние 15 мин; воспроизведение ×1/×4; в режиме перемотки весь экран показывает исторические кадры; кнопка «Отчёт PDF/CSV».
- **SettingsModal** (админ) — веса (сумма = 1, автонормировка), пороги, веса приоритетов, лимит времени решателя, скорость симуляции.
- **Login** — две учётки.

### 18.3 UX-правила

- Цвета: express `#a78bfa`, passenger `#60a5fa`, freight `#f59e0b`; Норма `#22c55e`, Внимание `#eab308`, Критично `#ef4444`. Цвет никогда не единственный носитель смысла: у статуса всегда есть текст или иконка.
- Три уровня важности: критичное (сбой, нарушение) — сверху и красным; требует решения (варианты) — подсветка панели «Решения»; информационное — в журнале.
- Числа всегда с единицами: «+12 мин», «74 / 100», «−18% энергии».
- При новом сбое панель «Решения» открывается сама, пока варианты считаются — скелетоны с таймером.

### 18.4 Производительность

- Входящие `field.state` складываются в store, рендер через `requestAnimationFrame` не чаще 30 к/с.
- Поездограмма обновляет только серии через `setOption(..., {lazyUpdate: true})`, без пересоздания графика.
- Задержка: `Date.now()/1000 − ts_wall` при рендере; p95 по скользящему окну 200 значений показывается в TopBar и отправляется на сервер.

### 18.5 Структура

```
web/src/
  api/ (rest.ts, ws.ts, types.ts — сгенерировать из OpenAPI через openapi-typescript или вручную)
  store/ (useLive.ts, usePlan.ts, useUi.ts — Zustand)
  components/ (TopBar, Mnemo/*, TrainGraph/*, VariantCards, WhatIfPanel, AtoChart, IndexPanel, EventLog, ScenarioBar, ReplayBar, SettingsModal, Login)
  lib/ (time.ts, colors.ts, format.ts)
```

---

## 19. Демо-сценарий защиты (`demo_full`, `docs/demo-script.md`)

1. Штатное движение (×20): мнемосхема и поездограмма, индекс «Норма», безопасность 0.
2. Скот на перегоне Рзд. 1 — Степная: сбой на схеме, прогноз конфликтов красным, индекс падает.
3. Через ~2 с — 3 карточки с цифрами, наведение показывает «призраки», применяем — маршруты ДЦ перестраиваются автоматически.
4. Поломка грузового 2003 на подъёме: «ждать ремонт» против «вспомогательный локомотив» — разница в цифрах.
5. What-if: 2003 со скоростью 60 вместо 80 — как меняются скрещения.
6. Автоведение для 2004: накат перед разъездом, экономия энергии в процентах.
7. Массовые сбои (8 штук): UI не тормозит, p95 и время решения видны в TopBar.
8. Перемотка последних 15 минут, выгрузка PDF.

---

## 20. НФТ и доказательства

| Требование | Цель | Как проверяем |
|---|---|---|
| Обновление UI от события | < 500 мс p95 | Метрика фронта в TopBar, `tools/loadtest.py` (WS-клиент считает `now − ts_wall`) |
| Пересчёт плана при сбое | ≤ 5 с p95 | `planner.metrics`, гистограмма `planner_solve_seconds` |
| 5–10 одновременных сбоев | без просадки UI | Сценарий `mass_incidents` + loadtest |
| История | 24–72 ч | Retention TimescaleDB |
| Безопасность | 0 нарушений | `safety_violations_total` |

`tools/loadtest.py`: логинится, открывает 5 WS-клиентов, запускает `mass_incidents` на ×60, 3 минуты собирает метрики, печатает таблицу p50/p95/max и пишет `docs/loadtest-result.md`.

Метрики Prometheus: `planner_solve_seconds{strategy}`, `ws_send_latency_seconds`, `bus_messages_total{topic}`, `safety_violations_total`, `sim_tick_seconds`, `ato_profile_seconds`.

Логи: structlog JSON, поля `service`, `event`, `sim_time`, `corr_id`.

---

## 21. Тесты (`make test`)

- **unit/physics**: аналитика ±2%, тормозной путь, равновесная скорость на подъёме.
- **unit/model**: на перегоне никогда два поезда; ёмкость путей; грузовой не ставится на путь 850 м; препятствие соблюдается; при скрещении пассажирского и грузового ждёт грузовой (стратегия `passenger_first`); стоянка ≥ минимальной; пассажирский не уходит раньше расписания.
- **unit/evaluate**: длиннейший путь на ручном примере; цикл → дедлок обнаружен.
- **unit/eco**: при `t_target = T_min` экономия 0; при `+10%` энергия меньше и время ±2 с.
- **unit/index**: веса, нормировка, категории.
- **unit/interlocking**: маршрут на занятый путь — отказ; отправление на перегон с встречным маршрутом — отказ; стрелки замыкаются и размыкаются.
- **integration/api**: логин, роли, `apply` устаревшего → 409, what-if → результат по WS.
- **e2e/headless**: симуляция ×1000 без UI, сценарий `cow_on_segment`, автоприменение лучшего варианта; утверждения: 0 нарушений безопасности, все поезда `FINISHED`, решение ≤ 5 с.

---

## 22. Порядок реализации

Каждая фаза заканчивается зелёными тестами и коротким коммитом. Не переходи к следующей фазе с красными тестами.

| Фаза | Содержание | Definition of Done |
|---|---|---|
| 0 | MVP по `MVP.md` | Демо из MVP.md проходит |
| 1 | `railcore`: модели, infra, physics, running_time, данные, `gen_timetable.py` | Тесты физики зелёные, `timetable.json` сгенерирован |
| 2 | `bus` (memory), `field` (sim, driver, interlocking, incidents, scenarios, safety), `app.all` | Поезда едут по плану v0 (расписание) в headless-режиме, 0 нарушений |
| 3 | `planner`: model, strategies, variants, evaluate, refresh, conflicts, index, explain | e2e headless: сбой → 3 варианта ≤ 5 с, применение, 0 нарушений |
| 4 | `api`: auth, REST, WS, state_cache | Swagger полный, интеграционные тесты зелёные |
| 5 | `web` ядро: Login, TopBar, Mnemo, TrainGraph, VariantCards, ScenarioBar | Сценарий скота проходит в браузере целиком |
| 6 | `ato` + AtoChart; what-if + WhatIfPanel | Профиль и what-if работают из UI |
| 7 | RabbitBus, recorder, TimescaleDB, история, ReplayBar, отчёты, Settings | `make up` поднимает всё; перемотка и PDF работают |
| 8 | Нагрузочный тест, метрики, полировка UI, README, docs, `demo_full` | `make load` в пределах НФТ, демо проходит 3 раза подряд без сбоев |

**Распределение на троих** (после фазы 1, которую делает один человек, пока остальные берут фазы 2 и фронт на моках):
- Бэкенд-ядро: planner (фазы 3, 6-what-if), затем фаза 7.
- Симуляция и физика: railcore, field, ato (фазы 1–2, 6-ato).
- Фронтенд: фазы 5–6 UI на моковом WS (`web/src/api/mock.ts` проигрывает записанный `snapshot.json`), затем интеграция; презентация.

Контракт между ними — модели раздела 8 и топики раздела 15. Менять их только с записью в раздел 25.

---

## 23. Инфраструктура

`Dockerfile` — один образ Python для всех сервисов (`uv` или `pip`, без dev-зависимостей). `web/Dockerfile` — сборка Vite → nginx (прокси `/api` и `/ws` на `api:8000`).

`docker-compose.yml`: `rabbitmq` (3.13-management, healthcheck), `db` (timescaledb pg16, healthcheck), `field`, `planner`, `ato`, `recorder`, `api` (порт 8000), `web` (порт 8080). Команды сервисов: `python -m app.<service>.main`. `depends_on` с `condition: service_healthy`.

`.env.example`:

```
BUS=rabbit
RABBIT_URL=amqp://guest:guest@rabbitmq:5672/
DATABASE_URL=postgresql://rail:rail@db:5432/rail
SIM_EPOCH=2026-10-02T07:55:00+05:00
JWT_SECRET=change-me
DISPATCHER_LOGIN=dispatcher
DISPATCHER_PASSWORD=dispatcher
ADMIN_LOGIN=admin
ADMIN_PASSWORD=admin
LOG_LEVEL=INFO
```

`Makefile`: `dev` (BUS=memory `python -m app.all` + `npm run dev`), `up`, `down`, `logs`, `test`, `lint`, `load`, `demo` (запуск `demo_full`), `fmt`.

`README.md`: что это, скриншот, запуск одной командой, учётки, сценарии, архитектура (ссылка на `docs/architecture.md`), формула индекса, НФТ с результатами loadtest, структура репозитория.

---

## 24. Правила разработки

- Типизация везде; `ruff` без ошибок.
- Доменная логика — чистые функции в `railcore`; сервисы — тонкие обёртки над ними и шиной.
- CPU-тяжёлое — только через `planner/pool.py` или `ProcessPoolExecutor` в ato.
- Никакого wall clock в домене, никаких глобальных изменяемых синглтонов, кроме состояния сервиса в одном классе.
- Конфиг — только `app/common/config.py` (pydantic-settings) + `settings` из БД/файла.
- Каждое число в UI берётся из данных бэкенда; на фронте ничего не досчитываем «на глаз».

**Не делать:** LLM-вызовы в ядре; Kafka; Kubernetes; реальные данные; ORM (только `asyncpg`); преждевременные абстракции (ровно один перегон = один ресурс ПАБ); `localStorage` для чего-то, кроме токена и UI-настроек; изменение контрактов разделов 8 и 15 без записи в раздел 25.

**Безопасность в презентации:** прототип консультативный, не заменяет СЦБ, АЛСН/АЛС-ЕН и сертифицированные системы. Секреты только в env.

---

## 25. Журнал решений

Дописывай сюда решения, принятые по ходу (дата, что, почему).

- Однопутные перегоны моделируются как ПАБ: один поезд на перегоне. Причина — простота и соответствие однопутке; автоблокировка — вне рамок.
- Шина RabbitMQ, а не Kafka: нагрузка — десятки сообщений в секунду, нужен паттерн «команда → ответ», простота развёртывания. Интерфейс `EventBus` позволяет заменить реализацию.
- Объяснения вариантов — шаблоны, а не LLM: детерминированность и скорость.

---

## 26. Презентация (`docs/presentation.md`, 10–12 слайдов)

1. Проблема диспетчера однопутки (цифры: ручные решения, цепочки задержек).
2. Решение: три опоры + what-if.
3. Демо-скриншот рабочего места.
4. Архитектура (схема раздела 5).
5. Как думает планировщик: интервалы, перегоны, пути, сбои → CP-SAT; европейский подход «альтернативный граф».
6. Варианты и устойчивость, вспомогательный локомотив.
7. Автоведение: режимы, Connected DAS, экономия энергии.
8. Индекс качества движения: формула и веса.
9. НФТ: результаты loadtest (таблица).
10. Безопасность и рамки (консультативная система).
11. Что дальше: интеграция с ДЦ, сеть участков, автоблокировка.

### 2026-10-01 · фаза 0 (MVP)

- **Расписание разведено через CP-SAT.** Желаемое расписание из §9.3 (10 поездов с 08:00 до 09:40) сильно перегружает однопутку: даже оптимальный план даёт ~5 ч суммарной задержки, и индекс в штатном режиме «Критично». `tools/gen_timetable.py` один раз решает его с лимитом 30 с; вынужденные скрещения становятся техническими стоянками графика. Отправления сдвигаются (до ~11:50), зато план v1 строится за ~20 мс с индексом 100. MVP.md это разрешает («примерные времена»).
- **Закрытие перегона в модели — ограничение `dep ≥ конец_сбоя`, а не фиксированный интервал в NoOverlap.** Для уже активных сбоев это эквивалентно, но не даёт недопустимости, когда на перегоне уже стоит поезд. Поезд перед препятствием получает `arr ≥ конец_сбоя + остаток хода`.
- **Warm start = `evaluate()` порядка подсказки (или расписания).** Плюс `repair_hint` и UB, расширенный под подсказку. Без этого CP-SAT за 1.5 с не находил даже допустимое решение.
- **Новый топик `planner.command_result {ok, code, reason, plan_version}` (с corr_id).** Через него API получает синхронный ответ на `apply` (409 для устаревшего).
- **`TrainState` дополнен полями `on_field`, `progress`, `arrived_at`, `stopped`.** Из них планировщик восстанавливает снимок «сейчас». `SpeedProfile` дополнен `min_points`, `t_min_s`, `v_lim_kmh`, `direction`, `km_from`, `km_to` для графика.
- **База сравнения в what-if — то же CP-SAT-решение без модификаций, решённое параллельно.** Δ показывает эффект изменённого параметра, а не выигрыш от самого перепланирования.
- **Добавлена настройка `planner.rescue_haul_s = 600`** (время вывода сломанного поезда вспомогательным локомотивом).
- **Индекс:** фактор `conflicts` считает вынужденные (незапланированные) остановки в прогнозе, CDR в MVP нет; `energy` — упрощённая модель (сопротивление на ходу + кинетическая энергия на каждый разгон с остановки).
