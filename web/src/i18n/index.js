/**
 * Переводы интерфейса: русский (исходный), казахский, английский.
 * Ключ перевода — сама русская фраза с параметрами в фигурных скобках: t('Поезд {n} стоит', { n: '701' }).
 * Ядро (планировщик, события, журнал) отдаёт не готовый текст, а сообщение m(ключ, параметры) —
 * его переводит интерфейс на том языке, который выбран сейчас. Нет перевода — показывается русский.
 */
import { useSyncExternalStore } from 'react';
import en from './en';
import kk from './kk';

export const LANGS = [
  { id: 'ru', label: 'RU', name: 'Русский' },
  { id: 'kk', label: 'KZ', name: 'Қазақша' },
  { id: 'en', label: 'EN', name: 'English' },
];

const DICT = { kk, en };
const KEY = 'autodispatcher.lang';
const listeners = new Set();

function load() {
  try {
    const v = localStorage.getItem(KEY);
    if (LANGS.some((l) => l.id === v)) return v;
  } catch {
    /* хранилище недоступно */
  }
  return 'ru';
}

let lang = typeof window === 'undefined' ? 'ru' : load();

export const getLang = () => lang;

function apply(next) {
  lang = next;
  if (typeof document !== 'undefined') document.documentElement.lang = next;
  listeners.forEach((l) => l());
}

export function setLang(next) {
  try {
    localStorage.setItem(KEY, next);
  } catch {
    /* язык просто не запомнится */
  }
  apply(next);
}

// Язык, выбранный в соседней вкладке (второй экран), применяется и здесь.
if (typeof window !== 'undefined') {
  document.documentElement.lang = lang;
  window.addEventListener('storage', (e) => {
    if (e.key === KEY && LANGS.some((l) => l.id === e.newValue)) apply(e.newValue);
  });
}

export function useLang() {
  return useSyncExternalStore(
    (fn) => {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
    getLang,
  );
}


export { m } from './msg';

export function t(msg, params) {
  if (msg === null || msg === undefined) return '';
  if (typeof msg === 'object') return t(msg.k, msg.p);
  const tpl = (lang !== 'ru' && DICT[lang]?.[msg]) || msg;
  if (!params) return tpl;
  return tpl.replace(/\{(\w+)\}/g, (_, k) => {
    const v = params[k];
    if (v === null || v === undefined) return '';
    return typeof v === 'object' ? t(v) : String(v);
  });
}
