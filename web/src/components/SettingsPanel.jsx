import { useState } from 'react';
import { CATEGORIES } from '../core/section';
import { DEFAULT_SETTINGS, FACTOR_NAMES, normalizedWeights } from '../core/settings';
import { can, PERMISSIONS } from '../engine/roles';
import { engine } from '../engine/store';
import { t } from '../i18n';
export function SettingsPanel({ s }) {
  const [d, setD] = useState(s.settings);
  if (!can(s.user, 'settings')) {
    return <div className="empty">{t('Нужно право «{perm}» — оно есть у роли «{role}».', { perm: t(PERMISSIONS.settings), role: t('Администратор') })}</div>;
  }
  const norm = normalizedWeights(d);
  const num = (v) => (v === '' ? 0 : Number(v));
  return (
    <div className="settings">
      <div className="block-title">{t('Веса факторов индекса')}</div>
      {Object.keys(FACTOR_NAMES).map((k) => (
        <label key={k} className="slider">
          <span>{t(FACTOR_NAMES[k])}</span>
          <input
            type="range"
            min={0}
            max={1}
            step={0.05}
            value={d.weights[k]}
            onChange={(e) => setD({ ...d, weights: { ...d.weights, [k]: Number(e.target.value) } })}
          />
          <span className="mono">{(norm[k] * 100).toFixed(0)}%</span>
        </label>
      ))}
      <p className="muted small">{t('Веса нормируются: Σ wᵢ = 1.')}</p>

      <div className="block-title">{t('Пороги категорий')}</div>
      <div className="form-grid">
        <label>
          {t('«Норма» от')}
          <input
            type="number"
            value={d.thresholds.norm}
            onChange={(e) => setD({ ...d, thresholds: { ...d.thresholds, norm: num(e.target.value) } })}
          />
        </label>
        <label>
          {t('«Внимание» от')}
          <input
            type="number"
            value={d.thresholds.warn}
            onChange={(e) => setD({ ...d, thresholds: { ...d.thresholds, warn: num(e.target.value) } })}
          />
        </label>
      </div>

      <div className="block-title">{t('Категории поездов')}</div>
      <table className="wi-table">
        <thead>
          <tr>
            <th>{t('Категория')}</th>
            <th>{t('Приоритет (1 — высший)')}</th>
            <th>{t('Вес опоздания')}</th>
          </tr>
        </thead>
        <tbody>
          {Object.keys(CATEGORIES).map((c) => (
            <tr key={c}>
              <td>
                <i className="swatch" style={{ background: CATEGORIES[c].color }} /> {t(CATEGORIES[c].name)}
              </td>
              <td>
                <input
                  type="number"
                  min={1}
                  max={9}
                  value={d.categoryPriority[c]}
                  onChange={(e) =>
                    setD({ ...d, categoryPriority: { ...d.categoryPriority, [c]: num(e.target.value) } })
                  }
                />
              </td>
              <td>
                <input
                  type="number"
                  min={0}
                  step={0.1}
                  value={d.categoryWeight[c]}
                  onChange={(e) => setD({ ...d, categoryWeight: { ...d.categoryWeight, [c]: num(e.target.value) } })}
                />
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <div className="block-title">{t('Параметры оптимизации')}</div>
      <div className="form-grid">
        <label>
          {t('Межпоездной интервал, с')}
          <input type="number" value={d.headwaySec} onChange={(e) => setD({ ...d, headwaySec: num(e.target.value) })} />
        </label>
        <label>
          {t('Запас на скрещении, с')}
          <input type="number" value={d.clearSec} onChange={(e) => setD({ ...d, clearSec: num(e.target.value) })} />
        </label>
        <label>
          {t('Итераций локального поиска')}
          <input
            type="number"
            min={0}
            max={30}
            value={d.optimizerIterations}
            onChange={(e) => setD({ ...d, optimizerIterations: num(e.target.value) })}
          />
        </label>
        <label>
          {t('Длительность сбоя в плане')}
          <select value={d.planDuration} onChange={(e) => setD({ ...d, planDuration: e.target.value })}>
            <option value="expected">{t('ожидаемая (середина диапазона)')}</option>
            <option value="max">{t('худшая (верх диапазона)')}</option>
          </select>
        </label>
        <label>
          {t('Автоприменение за, с до конфликта')}
          <input
            type="number"
            value={d.autoApplyLeadSec}
            onChange={(e) => setD({ ...d, autoApplyLeadSec: num(e.target.value) })}
          />
        </label>
        <label>
          {t('Норма опоздания для индекса, мин')}
          <input
            type="number"
            value={d.delayNormMin}
            onChange={(e) => setD({ ...d, delayNormMin: Math.max(1, num(e.target.value)) })}
          />
        </label>
        <label>
          {t('Хранение истории, ч')}
          <input
            type="number"
            min={24}
            max={72}
            value={d.historyHours}
            onChange={(e) => setD({ ...d, historyHours: Math.min(72, Math.max(24, num(e.target.value))) })}
          />
        </label>
      </div>

      <div className="row-actions">
        <button className="primary" onClick={() => engine.updateSettings(d)}>
          {t('Применить без перезапуска')}
        </button>
        <button className="ghost" onClick={() => setD(structuredClone(DEFAULT_SETTINGS))}>
          {t('По умолчанию')}
        </button>
      </div>
    </div>
  );
}
