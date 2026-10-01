/** Отчёты ретроспективы: CSV (Excel-совместимый) и PDF через печать браузера. */
import { describeDisruption } from '../core/disruptions';
import { CATEGORY_LABEL, destDelayMin } from '../core/qualityIndex';
import { CATEGORIES, SECTION } from '../core/section';
import { fmtHM, fmtHMS } from '../core/time';
import { runningPlan, shownIndex } from './engine';
import { getLang, t } from '../i18n';
/** Все ячейки и подписи отчёта переводятся на текущий язык интерфейса. */
const esc = (v) => `"${String(t(v)).replace(/"/g, '""')}"`;
const row = (cells) => cells.map(esc).join(';');
function trainRows(s) {
  const plan = runningPlan(s);
  return s.trains.map((t) => {
    const tp = plan.trains[t.id];
    const bp = s.baseline.trains[t.id];
    return {
      number: t.number,
      category: CATEGORIES[t.category].name,
      dir: t.dir === 1 ? 'нечётное' : 'чётное',
      planArr: tp ? fmtHM(tp.stops.at(-1).arr) : 'отменён',
      baseArr: bp ? fmtHM(bp.stops.at(-1).arr) : '—',
      delay: tp ? Math.round(destDelayMin(plan, s.baseline, t.id)) : '—',
    };
  });
}
function buildCsv(s, snaps, from, to) {
  const lines = [];
  lines.push(row(['Автодиспетчер — отчёт', SECTION.name, `${fmtHMS(from)}–${fmtHMS(to)}`]));
  lines.push('');
  lines.push(row(['Решения диспетчера']));
  lines.push(row(['Время', 'Решение', 'Индекс до', 'Индекс после', 'Взвеш. опоздание, мин', 'Кто']));
  for (const d of s.decisions)
    lines.push(row([fmtHMS(d.t), d.title, d.indexBefore, d.indexAfter, d.weightedDelayMin.toFixed(1), d.by]));
  lines.push('');
  lines.push(row(['Сбои']));
  lines.push(row(['Начало', 'Описание', 'Статус']));
  for (const d of s.disruptions)
    lines.push(
      row([
        fmtHMS(d.start),
        describeDisruption(d, s.trains),
        d.resolvedAt ? t('устранено {t}', { t: fmtHM(d.resolvedAt) }) : 'по оценке',
      ]),
    );
  lines.push('');
  lines.push(row(['Поезда']));
  lines.push(
    row(['Поезд', 'Категория', 'Направление', 'Прибытие по плану', 'Прибытие по нормативу', 'Опоздание, мин']),
  );
  for (const r of trainRows(s)) lines.push(row([r.number, r.category, r.dir, r.planArr, r.baseArr, r.delay]));
  lines.push('');
  lines.push(row(['Индекс качества (снимки)']));
  lines.push(row(['Время', 'Индекс', 'Категория', 'Конфликтов', 'Активных сбоев']));
  const step = Math.max(1, Math.floor(snaps.length / 200));
  snaps
    .filter((_, i) => i % step === 0)
    .forEach((x) => lines.push(row([fmtHMS(x.t), x.index, CATEGORY_LABEL[x.category], x.conflicts, x.disruptions])));
  lines.push('');
  lines.push(row(['Журнал событий']));
  lines.push(row(['Время', 'Уровень', 'Источник', 'Событие']));
  for (const e of s.log.filter((x) => x.t >= from && x.t <= to))
    lines.push(row([fmtHMS(e.t), e.level, e.source, e.text]));
  return '﻿' + lines.join('\r\n');
}
export function downloadCsv(s, snaps, from, to) {
  const blob = new Blob([buildCsv(s, snaps, from, to)], { type: 'text/csv;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `autodispatcher-report-${fmtHM(to).replace(':', '')}.csv`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}
const h = (v) => String(t(v)).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]);
function sparkline(snaps) {
  if (snaps.length < 2) return '';
  const W = 640;
  const H = 120;
  const t0 = snaps[0].t;
  const t1 = snaps[snaps.length - 1].t || t0 + 1;
  const pts = snaps
    .map((s) => `${(((s.t - t0) / (t1 - t0 || 1)) * W).toFixed(1)},${(H - (s.index / 100) * H).toFixed(1)}`)
    .join(' ');
  return `<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" style="border:1px solid #ccc"><polyline fill="none" stroke="#1565c0" stroke-width="2" points="${pts}"/></svg>`;
}
/** Открывает печатную форму — «Сохранить как PDF» в диалоге печати. */
export function printReport(s, snaps, from, to) {
  const idx = shownIndex(s);
  const html = `<!doctype html><html lang="${getLang()}"><head><meta charset="utf-8"><title>${h('Отчёт Автодиспетчера')} ${fmtHM(to)}</title>
<style>body{font:13px/1.45 system-ui,sans-serif;color:#111;margin:24px}h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:20px 0 6px}
table{border-collapse:collapse;width:100%}td,th{border:1px solid #bbb;padding:4px 6px;text-align:left}th{background:#f0f0f0}.muted{color:#666}</style></head><body>
<h1>${h('Автодиспетчер — отчёт по участку')}</h1>
<div class="muted">${h(SECTION.name)} · ${h(t('период {a}–{b}', { a: fmtHMS(from), b: fmtHMS(to) }))} · ${h(t('сформирован {t}', { t: new Date().toLocaleString(getLang() === 'kk' ? 'kk-KZ' : getLang() === 'en' ? 'en-GB' : 'ru-RU') }))}</div>
<h2>${h(t('Индекс качества движения: {v} ({cat})', { v: idx.value, cat: CATEGORY_LABEL[idx.category] }))}</h2>
<table><tr>${['Фактор', 'Вес', 'Оценка', 'Детали'].map((x) => `<th>${h(x)}</th>`).join('')}</tr>
${idx.factors.map((f) => `<tr><td>${h(f.name)}</td><td>${(f.weight * 100).toFixed(0)}%</td><td>${(f.score * 100).toFixed(0)}</td><td>${h(f.detail)}</td></tr>`).join('')}</table>
<h2>${h('Динамика индекса')}</h2>${sparkline(snaps)}
<h2>${h('Решения диспетчера')}</h2>
<table><tr>${['Время', 'Решение', 'Индекс', 'Взвеш. опоздание', 'Кто'].map((x) => `<th>${h(x)}</th>`).join('')}</tr>
${s.decisions.map((d) => `<tr><td>${fmtHMS(d.t)}</td><td>${h(d.title)}</td><td>${d.indexBefore} → ${d.indexAfter}</td><td>${d.weightedDelayMin.toFixed(1)} ${h('мин')}</td><td>${h(d.by)}</td></tr>`).join('') || `<tr><td colspan="5">${h('Решений не было')}</td></tr>`}</table>
<h2>${h('Сбои')}</h2>
<table><tr><th>${h('Начало')}</th><th>${h('Описание')}</th></tr>
${s.disruptions.map((d) => `<tr><td>${fmtHMS(d.start)}</td><td>${h(describeDisruption(d, s.trains))}</td></tr>`).join('') || `<tr><td colspan="2">${h('Сбоев не было')}</td></tr>`}</table>
<h2>${h('Поезда')}</h2>
<table><tr>${['Поезд', 'Категория', 'Направление', 'Прибытие (план)', 'Прибытие (норматив)', 'Опоздание, мин'].map((x) => `<th>${h(x)}</th>`).join('')}</tr>
${trainRows(s)
  .map(
    (r) =>
      `<tr><td>${r.number}</td><td>${h(r.category)}</td><td>${h(r.dir)}</td><td>${h(r.planArr)}</td><td>${r.baseArr}</td><td>${r.delay}</td></tr>`,
  )
  .join('')}</table>
<script>window.onload=()=>setTimeout(()=>window.print(),300)</script></body></html>`;
  const w = window.open('', '_blank');
  if (!w) return false;
  w.document.write(html);
  w.document.close();
  return true;
}
