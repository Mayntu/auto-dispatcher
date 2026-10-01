const pad = (n) => String(n).padStart(2, '0');
const norm = (t) => ((Math.floor(t) % 86400) + 86400) % 86400;
export function fmtHM(t) {
  const s = norm(t);
  return `${pad(Math.floor(s / 3600))}:${pad(Math.floor(s / 60) % 60)}`;
}
export function fmtHMS(t) {
  const s = norm(t);
  return `${fmtHM(s)}:${pad(s % 60)}`;
}
export function fmtDelta(min) {
  const r = Math.round(min);
  return r > 0 ? `+${r}` : r < 0 ? `−${Math.abs(r)}` : '0';
}
/** "08:35" → секунды. */
export function parseHM(v) {
  const m = /^(\d{1,2}):(\d{2})$/.exec(v.trim());
  if (!m) return null;
  return Number(m[1]) * 3600 + Number(m[2]) * 60;
}
