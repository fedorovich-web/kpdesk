export class YandexApiError extends Error {
  constructor(readonly status: number) { super('Yandex access unavailable'); }
}

const opaque = /^[A-Za-z0-9_-]{43}$/;
async function request(path: string, signal?: AbortSignal, csrf?: string): Promise<Record<string, unknown>> {
  if (window.location.protocol !== 'https:') throw new YandexApiError(0);
  if (csrf !== undefined && !opaque.test(csrf)) throw new YandexApiError(403);
  const response = await fetch(path, {
    method: csrf === undefined ? 'GET' : 'POST',
    credentials: 'same-origin', cache: 'no-store', redirect: 'error',
    referrerPolicy: 'no-referrer', signal,
    ...(csrf === undefined ? {} : {
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
      body: JSON.stringify({ next_path: '/app' }),
    }),
  });
  if (!response.ok) throw new YandexApiError(response.status);
  const data: unknown = await response.json();
  if (!data || typeof data !== 'object' || Array.isArray(data)) throw new YandexApiError(502);
  return data as Record<string, unknown>;
}

export const yandexApi = {
  async providers(signal?: AbortSignal): Promise<boolean> {
    return (await request('/api/session/providers', signal)).yandex === true;
  },
  async identity(signal?: AbortSignal): Promise<boolean> {
    const data = await request('/api/session/yandex/identity', signal);
    if (typeof data.linked !== 'boolean') throw new YandexApiError(502);
    return data.linked;
  },
  async startLink(csrf: string, signal?: AbortSignal): Promise<string> {
    const data = await request('/api/session/yandex/link/start', signal, csrf);
    const url = safeYandexAuthorizationUrl(data.authorization_url);
    if (!url) throw new YandexApiError(502);
    return url;
  },
};

/** Validate every externally supplied navigation target before leaving KPDesk. */
export function safeYandexAuthorizationUrl(value: unknown): string | null {
  if (typeof value !== 'string' || value.length > 4096
    || !value.startsWith('https://oauth.yandex.ru/authorize?')
    || /[\s#]/.test(value)) return null;
  try {
    const url = new URL(value);
    if (url.origin !== 'https://oauth.yandex.ru' || url.pathname !== '/authorize'
      || url.username || url.password || url.port || url.hash) return null;
    const params = url.searchParams;
    const keys = ['response_type', 'client_id', 'redirect_uri', 'scope', 'state',
      'code_challenge', 'code_challenge_method'];
    if ([...params.keys()].some(key => !keys.includes(key))
      || keys.some(key => params.getAll(key).length !== 1)
      || params.get('response_type') !== 'code'
      || !/^[a-fA-F0-9]{32}$/.test(params.get('client_id') ?? '')
      || params.get('redirect_uri') !== 'https://kpdesk.ru/api/session/yandex/callback'
      || params.get('scope') !== 'login:email login:info'
      || params.get('code_challenge_method') !== 'S256'
      || !opaque.test(params.get('state') ?? '')
      || !opaque.test(params.get('code_challenge') ?? '')) return null;
    return value;
  } catch { return null; }
}

const errorMessages: Record<string, string> = {
  yandex_disabled: 'Вход через Яндекс сейчас отключён. Войдите с паролем.',
  yandex_unavailable: 'Вход через Яндекс сейчас недоступен. Попробуйте позже или войдите с паролем.',
  yandex_configuration_invalid: 'Вход через Яндекс временно недоступен. Войдите с паролем.',
  yandex_provider_unavailable: 'Яндекс сейчас не отвечает. Попробуйте позже или войдите с паролем.',
  yandex_provider_invalid: 'Не удалось подтвердить ответ Яндекса. Повторите вход.',
  yandex_profile_invalid: 'Не удалось подтвердить данные аккаунта Яндекса. Войдите с паролем.',
  yandex_flow_expired: 'Время входа через Яндекс истекло. Начните вход заново.',
  yandex_cancelled: 'Вход через Яндекс отменён. Вы можете повторить его или войти с паролем.',
  yandex_account_link_required: 'Войдите в существующий аккаунт с паролем, затем подключите Яндекс в кабинете.',
  yandex_account_unavailable: 'Этот аккаунт недоступен для входа. Обратитесь в поддержку.',
  yandex_identity_conflict: 'Этот Яндекс ID уже связан с другим аккаунтом. Используйте другой способ входа.',
  yandex_rate_limited: 'Слишком много попыток входа через Яндекс. Подождите и попробуйте снова.',
  yandex_email_required: 'Для регистрации через Яндекс нужен подтверждённый адрес почты. Добавьте его в Яндекс ID или зарегистрируйтесь с почтой и паролем.',
  yandex_request_invalid: 'Не удалось подтвердить запрос входа через Яндекс. Начните вход заново.',
};
export function yandexErrorMessage(code: string): string {
  return Object.hasOwn(errorMessages, code) ? errorMessages[code]!
    : 'Не удалось завершить вход через Яндекс. Повторите вход или используйте пароль.';
}

/** Query feedback never establishes authentication. Preserve unrelated URL data. */
export function consumeYandexError(): string {
  const query = new URLSearchParams(window.location.search);
  if (!query.has('oauth_error')) return '';
  const message = yandexErrorMessage(query.get('oauth_error') ?? '');
  query.delete('oauth_error');
  const search = query.toString();
  window.history.replaceState(window.history.state, '', window.location.pathname
    + (search ? '?' + search : '') + (window.location.hash ?? ''));
  return message;
}
