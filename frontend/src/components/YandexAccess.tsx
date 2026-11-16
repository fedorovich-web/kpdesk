import { useEffect, useRef, useState } from 'react';
import { safeYandexAuthorizationUrl, yandexApi, YandexApiError } from '../yandex-api';

function useYandexCapability(allowed: boolean): boolean {
  const [enabled, setEnabled] = useState(false);
  useEffect(() => {
    setEnabled(false);
    if (!allowed) return;
    const controller = new AbortController();
    void yandexApi.providers(controller.signal).then(available => {
      if (!controller.signal.aborted) setEnabled(available);
    }).catch(() => { /* Password access remains usable when discovery is unavailable. */ });
    return () => controller.abort();
  }, [allowed]);
  return allowed && enabled;
}

export function YandexLogin({ secure, nextPath, mode = 'login' }: {
  secure: boolean; nextPath: '/app' | '/admin'; mode?: 'login' | 'register';
}) {
  const enabled = useYandexCapability(secure);
  if (!enabled) return null;
  const register = mode === 'register';
  return <section className="yandex-login" aria-label="Быстрый вход через Яндекс ID">
    <a className="yandex-auth-button" href={'/api/session/yandex/start?next='
      + (nextPath === '/admin' ? '/admin' : '/app')} referrerPolicy="no-referrer">
      <span className="yandex-brand-icon" aria-hidden="true">Я</span>
      <span>{register ? 'Зарегистрироваться с Яндекс ID' : 'Войти с Яндекс ID'}</span>
    </a>
    <p className="yandex-auth-caption">{register
      ? 'Без пароля: новый аккаунт создастся после подтверждения в Яндексе.'
      : 'Быстрый безопасный вход без пароля KPDesk.'}</p>
    <div className="yandex-auth-divider" aria-hidden="true"><span>или с помощью почты</span></div>
  </section>;
}

type ConnectionState = {
  scope: string; phase: 'loading' | 'linked' | 'unlinked' | 'pending' | 'error'; message?: string;
};
export function YandexConnection({ secure, csrfToken, onSessionExpired }: {
  secure: boolean; csrfToken: string | null; onSessionExpired?: () => void;
}) {
  const allowed = secure && /^[A-Za-z0-9_-]{43}$/.test(csrfToken ?? '');
  const enabled = useYandexCapability(allowed);
  const scopeRef = useRef(csrfToken);
  scopeRef.current = allowed ? csrfToken : null;
  const expiredRef = useRef(onSessionExpired);
  expiredRef.current = onSessionExpired;
  const linkController = useRef<AbortController | null>(null);
  const [record, setRecord] = useState<ConnectionState | null>(null);
  // A new account never renders the previous account's connection, even before effects.
  const state: ConnectionState = record?.scope === csrfToken ? record
    : { scope: csrfToken ?? '', phase: 'loading' };

  const authFailure = (error: unknown): string | null => {
    if (!(error instanceof YandexApiError)) return null;
    if (error.status === 401) {
      expiredRef.current?.();
      return 'Сессия завершилась. Войдите в аккаунт снова.';
    }
    return error.status === 403 ? 'Не удалось подтвердить доступ. Войдите в аккаунт снова.' : null;
  };

  useEffect(() => {
    if (!allowed || !enabled || !csrfToken) return;
    const scope = csrfToken;
    const controller = new AbortController();
    const current = () => !controller.signal.aborted && scopeRef.current === scope;
    setRecord({ scope, phase: 'loading' });
    void yandexApi.identity(controller.signal).then(linked => {
      if (current()) setRecord({ scope, phase: linked ? 'linked' : 'unlinked' });
    }).catch(error => {
      if (current()) setRecord({ scope, phase: 'error', message: authFailure(error)
        ?? 'Не удалось проверить подключение Яндекс ID. Обновите страницу и попробуйте снова.' });
    });
    return () => {
      controller.abort();
      linkController.current?.abort();
      linkController.current = null;
    };
  }, [allowed, enabled, csrfToken]);

  const connect = async () => {
    if (!allowed || !enabled || !csrfToken || state.phase !== 'unlinked'
      || linkController.current) return;
    const scope = csrfToken;
    const controller = new AbortController();
    linkController.current = controller;
    const current = () => !controller.signal.aborted && scopeRef.current === scope;
    setRecord({ scope, phase: 'pending' });
    try {
      const result = await yandexApi.startLink(scope, controller.signal);
      if (!current()) return;
      const url = safeYandexAuthorizationUrl(result);
      if (!url) throw new YandexApiError(502);
      window.location.assign(url);
    } catch (error) {
      if (!current()) return;
      const message = authFailure(error);
      setRecord({ scope, phase: message ? 'error' : 'unlinked', message: message
        ?? 'Не удалось начать подключение Яндекс ID. Попробуйте ещё раз.' });
      linkController.current = null;
    }
  };

  if (!allowed || !enabled) return null;
  return <section className="yandex-connection" aria-labelledby="yandex-connection-title">
    <h2 id="yandex-connection-title">Вход в аккаунт</h2>
    {state.phase === 'loading' && <p role="status">Проверяем подключение Яндекс ID…</p>}
    {state.phase === 'linked' && <p role="status">Яндекс ID подключён.</p>}
    {state.message && <div className="alert error" role="alert">{state.message}</div>}
    {(state.phase === 'unlinked' || state.phase === 'pending') && <>
      <p>Подключите Яндекс ID, чтобы входить в этот аккаунт через Яндекс.</p>
      <button type="button" className="button button-white" disabled={state.phase === 'pending'}
        onClick={() => void connect()}>
        {state.phase === 'pending' ? 'Подключаем Яндекс ID…' : 'Подключить Яндекс ID'}
      </button>
    </>}
  </section>;
}
