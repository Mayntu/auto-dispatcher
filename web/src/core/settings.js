export const FACTOR_NAMES = {
  punctuality: 'Соблюдение расписания',
  throughput: 'Использование пропускной способности',
  energy: 'Энергоэффективность ведения',
  conflicts: 'Конфликты на скрещениях и обгонах',
  accuracy: 'Точность прибытия и остановки',
};
export const DEFAULT_SETTINGS = {
  weights: { punctuality: 0.3, throughput: 0.2, energy: 0.15, conflicts: 0.2, accuracy: 0.15 },
  thresholds: { norm: 80, warn: 60 },
  categoryPriority: { express: 1, pass: 1, suburb: 2, freightFast: 3, freight: 4 },
  categoryWeight: { express: 5, pass: 3, suburb: 2, freightFast: 1.3, freight: 1 },
  headwaySec: 240,
  clearSec: 60,
  horizonMin: 120,
  planDuration: 'expected',
  optimizerIterations: 6,
  autoApplyLeadSec: 180,
  delayNormMin: 30,
  historyHours: 24,
};
const KEY = 'autodispatcher.settings.v1';
export function loadSettings() {
  try {
    const raw = localStorage.getItem(KEY);
    if (raw) {
      const saved = JSON.parse(raw);
      return {
        ...DEFAULT_SETTINGS,
        ...saved,
        categoryPriority: { ...DEFAULT_SETTINGS.categoryPriority, ...saved.categoryPriority },
        categoryWeight: { ...DEFAULT_SETTINGS.categoryWeight, ...saved.categoryWeight },
      };
    }
  } catch {
    /* хранилище недоступно — работаем на значениях по умолчанию */
  }
  return structuredClone(DEFAULT_SETTINGS);
}
export function saveSettings(s) {
  try {
    localStorage.setItem(KEY, JSON.stringify(s));
  } catch {
    /* не критично */
  }
}
export function normalizedWeights(s) {
  const keys = Object.keys(s.weights);
  const sum = keys.reduce((a, k) => a + Math.max(0, s.weights[k]), 0) || 1;
  return Object.fromEntries(keys.map((k) => [k, Math.max(0, s.weights[k]) / sum]));
}
