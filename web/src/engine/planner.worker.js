/** Расчёт вариантов в отдельном потоке — интерфейс не проседает при массовых сбоях. */
import { computeVariants } from '../core/planner';
self.onmessage = (e) => {
  const result = computeVariants(e.data.input);
  self.postMessage({ reqId: e.data.reqId, result });
};
