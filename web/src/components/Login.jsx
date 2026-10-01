import { useState } from 'react';
import { authConfigured } from '../engine/auth';
import { PERMISSIONS, ROLES } from '../engine/roles';
import { engine } from '../engine/store';
import { t } from '../i18n';
import { LangSwitch } from './ui';
export function Login() {
  const [login, setLogin] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const submit = (e) => {
    e.preventDefault();
    if (!engine.login(login, password)) setError('Неверный логин или пароль');
  };
  return (
    <div className="login">
      <form onSubmit={submit} className="login-card">
        <div className="login-lang">
          <LangSwitch />
        </div>
        <div className="brand-name">{t('Автодиспетчер')}</div>
        <div className="muted">{t('Система поддержки решений поездного диспетчера ДЦ')}</div>
        {!authConfigured() && (
          <div className="ev warn">
            {t('Учётные записи не заданы: скопируйте .env.example в .env и перезапустите dev-сервер.')}
          </div>
        )}
        <label>
          {t('Логин')}
          <input autoFocus value={login} onChange={(e) => setLogin(e.target.value)} autoComplete="username" />
        </label>
        <label>
          {t('Пароль')}
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
          />
        </label>
        {error && <div className="crit-text small">{t(error)}</div>}
        <button className="primary" type="submit">
          {t('Войти')}
        </button>
        <div className="roles">
          {Object.entries(ROLES).map(([id, r]) => (
            <div key={id} className={`role-card ${id}`}>
              <div className="role-head">
                <b>{t(r.name)}</b>
                <span className="muted small">{r.permissions.map((p) => t(PERMISSIONS[p])).join(' + ')}</span>
              </div>
              <ul>
                {r.duties.map((d) => (
                  <li key={d}>{t(d)}</li>
                ))}
              </ul>
            </div>
          ))}
          <div className="muted small">{t('Решение всегда принимает человек — система только предлагает.')}</div>
        </div>
      </form>
    </div>
  );
}
