import { afterEach, describe, expect, it, vi } from 'vitest';
import { consumeYandexError, safeYandexAuthorizationUrl, yandexApi, YandexApiError,
  yandexErrorMessage } from './yandex-api';

const authorizationUrl = 'https://oauth.yandex.ru/authorize?response_type=code&client_id='
  + 'a'.repeat(32) + '&redirect_uri=https%3A%2F%2Fkpdesk.ru%2Fapi%2Fsession%2Fyandex%2Fcallback'
  + '&scope=login%3Aemail+login%3Ainfo&state=' + 's'.repeat(43)
  + '&code_challenge=' + 'c'.repeat(43) + '&code_challenge_method=S256';
function secure() {
  const secured = Object.create(window) as Window;
  Object.defineProperty(secured, 'location', { value: { protocol: 'https:' } });
  vi.stubGlobal('window', secured);
}
function response(body: unknown, status = 200) {
  return { ok: status === 200, status, json: async () => body };
}
afterEach(() => {
  vi.unstubAllGlobals(); vi.restoreAllMocks(); window.history.replaceState({}, '', '/');
});

describe('Yandex same-origin adapter', () => {
  it('reads a public boolean capability with no-store credentials', async () => {
    secure();
    const fetcher = vi.fn().mockResolvedValue(response({ yandex: true }));
    vi.stubGlobal('fetch', fetcher);
    expect(await yandexApi.providers()).toBe(true);
    expect(fetcher).toHaveBeenCalledWith('/api/session/providers', expect.objectContaining({
      credentials: 'same-origin', cache: 'no-store', redirect: 'error', referrerPolicy: 'no-referrer',
    }));
  });
  it.each([false, 'true', null, 1])('enables capability only for literal true: %s', async yandex => {
    secure(); vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response({ yandex })));
    expect(await yandexApi.providers()).toBe(false);
  });
  it('requires HTTPS before any request', async () => {
    const fetcher = vi.fn(); vi.stubGlobal('fetch', fetcher);
    await expect(yandexApi.identity()).rejects.toBeInstanceOf(YandexApiError);
    expect(fetcher).not.toHaveBeenCalled();
  });
  it('does not turn unauthenticated identity into an unlinked account', async () => {
    secure(); vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response({ linked: false }, 401)));
    await expect(yandexApi.identity()).rejects.toMatchObject({ status: 401 });
  });
  it.each([true, false])('returns honest linked status %s', async linked => {
    secure(); const fetcher = vi.fn().mockResolvedValue(response({ linked }));
    vi.stubGlobal('fetch', fetcher);
    expect(await yandexApi.identity()).toBe(linked);
    expect(fetcher).toHaveBeenCalledWith('/api/session/yandex/identity', expect.objectContaining({
      credentials: 'same-origin', cache: 'no-store',
    }));
  });
  it('rejects malformed identity instead of displaying unlinked', async () => {
    secure(); vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response({ linked: 'false' })));
    await expect(yandexApi.identity()).rejects.toBeInstanceOf(YandexApiError);
  });
  it('starts explicit linking with current CSRF and abort signal', async () => {
    secure(); const fetcher = vi.fn().mockResolvedValue(response({ authorization_url: authorizationUrl }));
    vi.stubGlobal('fetch', fetcher); const controller = new AbortController();
    expect(await yandexApi.startLink('x'.repeat(43), controller.signal)).toBe(authorizationUrl);
    expect(fetcher).toHaveBeenCalledWith('/api/session/yandex/link/start', expect.objectContaining({
      method: 'POST', credentials: 'same-origin', cache: 'no-store', signal: controller.signal,
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': 'x'.repeat(43) },
      body: '{"next_path":"/app"}',
    }));
  });
  it.each(['', 'bad', 'x'.repeat(44)])('rejects invalid CSRF before requesting: %s', async csrf => {
    secure(); const fetcher = vi.fn(); vi.stubGlobal('fetch', fetcher);
    await expect(yandexApi.startLink(csrf)).rejects.toBeInstanceOf(YandexApiError);
    expect(fetcher).not.toHaveBeenCalled();
  });
  it('rejects unsafe redirect returned by the backend', async () => {
    secure(); vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response({ authorization_url: 'https://evil.test/' })));
    await expect(yandexApi.startLink('x'.repeat(43))).rejects.toBeInstanceOf(YandexApiError);
  });
});

describe('Yandex authorization redirect validation', () => {
  it('accepts the fixed Yandex endpoint and callback with PKCE', () => {
    expect(safeYandexAuthorizationUrl(authorizationUrl)).toBe(authorizationUrl);
  });
  it.each([
    null, 'javascript:alert(1)', authorizationUrl.replace('https:', 'http:'),
    authorizationUrl.replace('oauth.yandex.ru', 'oauth.yandex.ru.evil.test'),
    authorizationUrl.replace('oauth.yandex.ru', 'user@oauth.yandex.ru'),
    authorizationUrl.replace('oauth.yandex.ru', 'oauth.yandex.ru:444'),
    authorizationUrl.replace('/authorize?', '/authorize/other?'), authorizationUrl + '#fragment',
    authorizationUrl.replace('kpdesk.ru', 'evil.test'), authorizationUrl.replace('response_type=code', 'response_type=token'),
    authorizationUrl.replace('S256', 'plain'), authorizationUrl.replace('state=' + 's'.repeat(43), 'state=short'),
    authorizationUrl.replace('code_challenge=' + 'c'.repeat(43), 'code_challenge=short'),
    authorizationUrl + '&state=' + 's'.repeat(43), authorizationUrl + '&next=https://evil.test',
    authorizationUrl.replace('client_id=' + 'a'.repeat(32), 'client_id=wrong'),
    authorizationUrl.replace('scope=login%3Aemail+login%3Ainfo', 'scope=other'),
  ])('rejects a mismatched or malformed provider URL %#', value => {
    expect(safeYandexAuthorizationUrl(value)).toBeNull();
  });
});

describe('OAuth error feedback', () => {
  it.each([
    'yandex_disabled', 'yandex_unavailable', 'yandex_configuration_invalid', 'yandex_provider_unavailable',
    'yandex_provider_invalid', 'yandex_profile_invalid', 'yandex_flow_expired', 'yandex_cancelled',
    'yandex_account_link_required', 'yandex_account_unavailable', 'yandex_identity_conflict',
    'yandex_rate_limited', 'yandex_email_required', 'yandex_request_invalid',
  ])('gives fixed Russian feedback for %s', code => {
    expect(yandexErrorMessage(code)).toMatch(/[а-яА-Я]/);
    expect(yandexErrorMessage(code)).not.toContain(code);
  });
  it('explains explicit linking after ordinary sign-in', () => {
    expect(yandexErrorMessage('yandex_account_link_required')).toMatch(/Войдите.*парол.*подключите Яндекс/);
  });
  it('never echoes untrusted or inherited property names', () => {
    expect(yandexErrorMessage('<script>secret</script>')).not.toContain('secret');
    expect(yandexErrorMessage('constructor')).toMatch(/[а-яА-Я]/);
  });
  it('consumes only oauth_error and preserves token, return target, fragment and history state', () => {
    window.history.replaceState({ retained: true }, '', '/auth/login?token=keep&oauth_error=yandex_cancelled&next=%2Fadmin#keep');
    expect(consumeYandexError()).toMatch(/отмен/);
    expect(window.location.search).toBe('?token=keep&next=%2Fadmin');
    expect(window.location.hash).toBe('#keep'); expect(window.history.state).toEqual({ retained: true });
    expect(consumeYandexError()).toBe('');
  });
});
