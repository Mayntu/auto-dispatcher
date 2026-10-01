# Ответы бэкенда на docs/backend-issues.md

Отдельный файл, чтобы не конфликтовать с вашим `backend-issues.md` при слиянии. Все пункты закрыты в main;
проверка — `tools/e2e_manual.py`, `tools/e2e_contract.py`, контракт — `docs/frontend-contract.md`.

| № | Что | Статус |
|---|---|---|
| 1 | `track_id` у `dwell` | исправлено раньше (48c0d88) |
| 2 | What-if «Перенести в работу» | `POST /api/whatif/{request_id}/promote`. Переносится порядок, не изменённые параметры; если порядок при фактических параметрах хуже действующего плана — 409 с причиной (показать её диспетчеру) |
| 3 | Уточнение длительности сбоя | `POST /api/incidents/{id}/estimate`, WS `incident.updated`, варианты пересчитываются |
| 4 | Сценарии | `GET /api/scenarios`, `POST /api/scenarios/{id}/run`, есть `mass_incidents` |
| 5 | Типы сбоев | добавлены `train_delay` (`train_id`) и `signal_failure` (`station_id` + `direction`); `track_closed` пока нет — кнопку не показывать |
| 6 | Прогноз конфликтов | WS `planner.conflicts` 1 Гц + `GET /api/state → conflicts`; в штатном режиме пусто — это нормально |
| 7 | Настройки | `GET/PUT /api/settings`, веса автонормируются, ошибки 422 с текстом |
| 8 | История и отчёт | `GET /api/history/frames`, `/history/events` (последние 20 мин симуляции), `GET /api/reports?format=csv`; PDF нет (422) — кнопку PDF не показывать |
| 9 | WS `incident.updated` | пересылается |
| 10 | Текст ошибки для неподдерживаемого типа | «тип сбоя … пока не поддерживается» |
| 12 | Вариант с ГИД сразу устаревал | исправлено: живые карточки сравнивают с планом при том же указании; был ещё ложный штраф за округление времени |
| 13 | Варианты без сбоя | исправлено: устаревшая карточка без сбоев не порождает новые; снятие нескольких сбоев подряд — тихий refresh или «Возврат к графику» |
| 14 | WS `planner.pin_violated` | пересылается (и `planner.conflicts`, `settings.updated`) |
