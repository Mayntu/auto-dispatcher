# Замечания к бэкенду (пишет фронт, читает и закрывает бэкенд)

Формат: дата · что делал (запрос/шаги, время симуляции) · что пришло · что ожидал · [контракт] если нужно новое поле.

## 2026-10-02 · проверка через API (коммит 039a06f, `python -m app.all`, BUS=memory)

1. **В плане у `dwell` всегда `track_id = null`.**
   `GET /api/state` при sim_time ≈ 3816 (08:58): во всех `plan.entries[kind=dwell]` и во всех `variants[].plan.entries[kind=dwell]`
   `track_id` пустой на всех пунктах (SEV, R1, STP, R2, OZR, YUZ). При этом у поля `TrainState.track_id` заполнено
   (например, 341 на STP — путь `3`). Ожидал: по контракту у `dwell` — путь, назначенный планом (с учётом полезной длины
   и платформ). Без него нельзя показать «назначенный планом путь» на мнемосхеме и в карточке станции.

2. **[контракт] Нет «Перенести в работу» для what-if.** `POST /api/whatif` отдаёт результат, но из него нельзя сделать
   вариант. Ожидал по §12.8 / §16.2: `POST /api/whatif/{id}/promote` → вариант `proposed` в `planner.variants`
   (и `request_id` в `WhatIfResult`, сейчас его нет в ответе).

3. **[контракт] Нет уточнения длительности сбоя.** В §16.2 есть `POST /api/incidents/{id}/estimate`, в API его нет.
   Нужен инструктору («дольше/короче на 10 мин») и для сценария «бригада на месте» (§9.5).

4. **[контракт] Нет сценариев.** `GET /api/scenarios`, `POST /api/scenarios/{id}/run` (§16.2) отсутствуют,
   сценарий проверки 8 («8 сбоев подряд», `mass_incidents`) можно сделать только восемью `POST /api/incidents`.

5. **[контракт] Принимаются только 3 типа сбоев.** `POST /api/incidents` с типом не из
   `obstacle | train_failure | segment_closed` → 422 «not supported in MVP». Для экрана нужны `signal_failure`,
   `speed_restriction`, `train_delay` (§11.4) — на фронте уже есть кнопки «Отказ светофора» и «Опоздание».

6. **[контракт] Нет прогноза конфликтов (CDR, §12.7).** Топика `planner.conflicts` нет, а на ГИД по §27.3 нужны
   красные маркеры конфликтов до того, как они случатся. Сейчас есть только счётчик `kpi.conflicts`.

7. **[контракт] Нет `PUT /api/settings` / `GET /api/settings`.** Экран настроек (веса и пороги индекса, приоритеты)
   не может ничего изменить на сервере; пороги есть только в `GET /api/infra → thresholds`.

8. **[контракт] Нет истории и отчётов.** `GET /api/history/frames`, `/history/events`, `/reports` (§16.2) отсутствуют —
   перемотку и отчёт фронт может строить только из того, что сам принял по WS с момента открытия страницы.

9. **WS не пересылает `incident.updated`.** В `app/api/ws.py` `FORWARD` нет `incident.updated`; когда появится
   уточнение длительности (п. 3), фронт не узнает об изменении до следующего `field.state`.

10. **Неподдерживаемый тип сбоя → вводящий в заблуждение текст ошибки.** `POST /api/incidents`
    `{"type":"train_delay","train_id":"104","est_min_min":10,"est_max_min":20}` → 422 `unknown segment`.
    Ожидал 422 «тип не поддерживается» (как для остальных типов вне MVP): в `FieldSim.create_incident` проверка
    перегона идёт раньше проверки типа в `make_incident`, диспетчер видит непонятную причину.

11. **[контракт] Нет API ручного изменения времени на ГИД и указаний диспетчера.** Фронт из ветки `frontMax`
    (перетаскивание ниток, `web/src/components/ManualDrag.jsx`) вызывает `GET /api/plan/manual/bounds?train_id&station_id`,
    `POST /api/plan/manual/preview`, `POST /api/plan/manual/commit`, `GET /api/plan/pins`, `DELETE /api/plan/pins/{id}`,
    ждёт `plan.pins` в `Plan` и событие WS `planner.pin_violated` — сейчас все пути отвечают 404. Формат, который ждёт фронт,
    — в `web/src/api/adapter.js` (`boundsFromSpec`, `previewFromSpec`, `pinFromSpec`). В памятке упомянут
    `tasks/02-manual-drag-*.md`, в репозитории его пока нет.

### Перепроверка после 48c0d88 (realism 5, 5.5, 6, 9)

- п. 1 — **исправлено**: у `dwell` в плане заполнен `track_id` (`I`, `2`, `3`, `4`).
- п. 5 — **частично**: `speed_restriction` (предупреждение) принимается и подключён на фронте; `signal_failure`, `train_delay` — по-прежнему нет.
- п. 2–4, 6–11 — без изменений (`/api/plan/pins`, `/api/scenarios`, `/api/settings` → 404; `train_delay` → 422 `unknown segment`).

### Перепроверка после af4ddd4 (manual drag, tasks/02)

- п. 1 — исправлено (см. выше). п. 11 — **API есть**: `bounds`, `preview`, `commit`, `pins` отвечают, фронт подключён.
- п. 2 — **частично**: `request_id` в `WhatIfResult` появился, `POST /api/whatif/{id}/promote` — по-прежнему 404.
- п. 3, 4, 6, 7, 8, 10 — без изменений (404 / 422 `unknown segment` для `train_delay` без `segment_id`).
- п. 5 — без изменений: `signal_failure`, `train_delay` → 422 «not supported in MVP».

12. **Указание с ГИД невозможно применить: ручной вариант сразу устаревает.** Воспроизводится 3 из 3, без активных сбоев,
    sim_time ≈ 1120–1170: `GET /api/plan/manual/bounds?train_id=2002&station_id=STP` → `POST /api/plan/manual/commit`
    `{base_plan_version: 1, train_id: "2002", station_id: "STP", kind: "dep", time: current+300}` → 200, 1 вариант `keep_order`
    (≈ 4 с). Через ~1 с симуляции журнал: «Устарели варианты: «Сохранить порядок» — положение поездов изменилось» →
    «Активных решений нет», `POST /api/plan/apply` с этим `variant_id` → **404 «Вариант не найден»**, `GET /api/plan/pins` → `[]`.
    Из интерфейса (ждали чуть дольше) — 409 «Пока вариант ждал решения, он стал хуже текущего плана (задержка 17 мин
    против 4)». Ожидал: вариант с указанием живёт, пока диспетчер не решит (живое перевремение не должно считать
    намеренную задержку «хуже текущего плана»), после `apply` — новая версия плана и `Pin` в `plan.pins`.

13. **Варианты без сбоя.** После того как ручной вариант ушёл в архив (п. 12), через ~10 с без активных сбоев приходит
    `planner.variants` с тремя обычными карточками (`balanced`/`robust`/`passenger_first`, «Предложено вариантов: 3»)
    и время снова ×1. Ожидал по §12.1: без сбоев — тихий refresh, карточек нет (или одна «Возврат к графику»).

14. **WS не пересылает `planner.pin_violated`** (в `app/api/ws.py` `FORWARD` его нет, как и `incident.updated`, п. 9) —
    фронт не узнает о нарушенном указании до следующего плана.
