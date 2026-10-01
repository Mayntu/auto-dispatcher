import { useLayoutEffect, useMemo, useState } from 'react';
import { LANGS, setLang, useLang } from '../i18n';
/**
 * Размер элемента. Ref-функция подхватывает элемент, даже если он появился позже первого рендера
 * (например, в развёрнутой панели); ref.current — сам элемент.
 */
export function useSize() {
  const [el, setEl] = useState(null);
  const [size, setSize] = useState({ w: 800, h: 300 });
  const ref = useMemo(() => {
    const fn = (node) => {
      fn.current = node;
      setEl(node);
    };
    fn.current = null;
    return fn;
  }, []);
  useLayoutEffect(() => {
    if (!el) return undefined;
    const ro = new ResizeObserver(([e]) => {
      const r = e.contentRect;
      setSize((p) => (Math.abs(p.w - r.width) < 1 && Math.abs(p.h - r.height) < 1 ? p : { w: r.width, h: r.height }));
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [el]);
  return [ref, size];
}
export const CAT_CLASS = { norm: 'ok', warn: 'warn', crit: 'crit' };
export function Delta({ value, unit = '', invert = false, digits = 0 }) {
  const r = Number(value.toFixed(digits));
  if (r === 0) return <span className="delta">±0{unit}</span>;
  const good = invert ? r < 0 : r > 0;
  return (
    <span className={`delta ${good ? 'good' : 'bad'}`}>
      {r > 0 ? '+' : '−'}
      {Math.abs(r).toFixed(digits)}
      {unit}
    </span>
  );
}
export function Empty({ children }) {
  return <div className="empty">{children}</div>;
}
/** Переключатель языка интерфейса: RU / KZ / EN. */
export function LangSwitch() {
  const lang = useLang();
  return (
    <div className="lang-switch" role="group" aria-label="Language">
      {LANGS.map((l) => (
        <button key={l.id} type="button" className={lang === l.id ? 'on' : ''} title={l.name} onClick={() => setLang(l.id)}>
          {l.label}
        </button>
      ))}
    </div>
  );
}
