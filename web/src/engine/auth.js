/** Базовая аутентификация прототипа. Учётные данные — только из переменных окружения (.env). */
import { ROLES } from './roles';

const env = import.meta.env ?? {};
const ACCOUNTS = [
  { login: env.VITE_DISPATCHER_LOGIN, password: env.VITE_DISPATCHER_PASSWORD, role: 'dispatcher' },
  { login: env.VITE_ADMIN_LOGIN, password: env.VITE_ADMIN_PASSWORD, role: 'admin' },
  { login: env.VITE_INSTRUCTOR_LOGIN, password: env.VITE_INSTRUCTOR_PASSWORD, role: 'instructor' },
];

const userOf = (login, role) => ({ login, role, name: ROLES[role].title });

export const authConfigured = () => ACCOUNTS.some((a) => a.login && a.password);

export function authenticate(login, password) {
  const acc = ACCOUNTS.find((a) => a.login && a.password && a.login === login.trim() && a.password === password);
  return acc ? userOf(acc.login, acc.role) : null;
}

const KEY = 'autodispatcher.session';

/** Сессия восстанавливается только для учётной записи, которая по-прежнему есть в окружении с той же ролью. */
export function restoreUser() {
  try {
    const raw = sessionStorage.getItem(KEY);
    const saved = raw ? JSON.parse(raw) : null;
    const acc = saved && ACCOUNTS.find((a) => a.login && a.login === saved.login && a.role === saved.role);
    return acc ? userOf(acc.login, acc.role) : null;
  } catch {
    return null;
  }
}

export function persistUser(u) {
  try {
    if (u) sessionStorage.setItem(KEY, JSON.stringify({ login: u.login, role: u.role }));
    else sessionStorage.removeItem(KEY);
  } catch {
    /* сессия просто не переживёт перезагрузку */
  }
}
