"""Shift report (CLAUDE.md §16.4), CSV: events, decisions, delays by train, the index by minute."""

from __future__ import annotations

import csv
import io

from app.common.config import get_env


def clock(t: float) -> str:
    e = get_env().sim_epoch
    s = round(t) + e.hour * 3600 + e.minute * 60
    return f"{(s // 3600) % 24:02d}:{(s % 3600) // 60:02d}"


def build_report_csv(cache, t_from: float | None, t_to: float | None) -> str:
    def inside(t: float) -> bool:
        return (t_from is None or t >= t_from) and (t_to is None or t <= t_to)

    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Отчёт Автодиспетчера", f"период {clock(t_from) if t_from is not None else 'начало'} — "
                f"{clock(t_to) if t_to is not None else 'сейчас'}"])
    w.writerow([])
    w.writerow(["События и решения"])
    w.writerow(["время", "вид", "текст", "стратегия"])
    for j in cache.journal:
        if inside(j["time"]):
            w.writerow([clock(j["time"]), j["kind"], j["text"], j.get("strategy") or ""])
    w.writerow([])
    w.writerow(["Принятые решения"])
    w.writerow(["время", "вариант", "стратегия"])
    for j in cache.journal:
        if inside(j["time"]) and j["kind"] in ("variant_applied", "pin_set", "guard_replan"):
            w.writerow([clock(j["time"]), j["text"], j.get("strategy") or ""])
    w.writerow([])
    w.writerow(["Опоздания на конечных по текущему прогнозу"])
    w.writerow(["поезд", "опоздание, мин"])
    for tid, s in (cache.index or {}).get("kpi", {}).get("delayed_trains", []):
        w.writerow([tid, round(s / 60)])
    w.writerow([])
    w.writerow(["Индекс качества движения по минутам"])
    w.writerow(["время", "индекс", "категория"])
    for minute, value, cat in cache.index_minutes:
        if inside(minute):
            w.writerow([clock(minute), round(value, 1), {"norm": "Норма", "attention": "Внимание", "critical": "Критично"}[cat]])
    if cache.field:
        w.writerow([])
        w.writerow(["Нарушений безопасности", cache.field.get("safety_violations", 0)])
    return "﻿" + buf.getvalue()  # BOM: Excel opens Cyrillic correctly
