import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { YandexConnection, YandexLogin } from './YandexAccess';
import { yandexApi, YandexApiError } from '../yandex-api';

const csrfA = 'a'.repeat(43), csrfB = 'b'.repeat(43);
const validUrl = 'https://oauth.yandex.ru/authorize?response_type=code&client_id='
  + 'a'.repeat(32) + '&redirect_uri=https%3A%2F%2Fkpdesk.ru%2Fapi%2Fsession%2Fyandex%2Fcallback'
  + '&scope=login%3Aemail+login%3Ainfo&state=' + 's'.repeat(43)
  + '&code_challenge=' + 'c'.repeat(43) + '&code_challenge_method=S256';
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return { promise, resolve };
}
let assign: ReturnType<typeof vi.fn>;
beforeEach(() => {
  assign = vi.fn();
  const secured = Object.create(window) as Window;
  Object.defineProperty(secured, 'location', { value: { protocol: 'https:', assign } });
  vi.stubGlobal('window', secured);
  vi.stubGlobal('fetch', vi.fn(() => { throw new Error('Unexpected network request'); }));
  vi.spyOn(yandexApi, 'providers').mockResolvedValue(true);
  vi.spyOn(yandexApi, 'identity').mockResolvedValue(false);
});
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe('optional Yandex login', () => {
  it.each(['/app', '/admin'] as const)('uses only the same-origin start link to %s', async nextPath => {
    render(<YandexLogin secure nextPath={nextPath}/>);
    expect(await screen.findByRole('link', { name: 'Войти с Яндекс ID' }))
      .toHaveAttribute('href', '/api/session/yandex/start?next=' + nextPath);
    expect(yandexApi.identity).not.toHaveBeenCalled();
  });
  it('offers passwordless registration on the existing same-origin OAuth flow', async () => {
    render(<YandexLogin secure nextPath="/app" mode="register"/>);
    const link = await screen.findByRole('link', { name: 'Зарегистрироваться с Яндекс ID' });
    expect(link).toHaveAttribute('href', '/api/session/yandex/start?next=/app');
    expect(screen.getByText(/новый аккаунт создастся/i)).toBeInTheDocument();
    expect(screen.getByText('или с помощью почты')).toBeInTheDocument();
    expect(link.querySelector('.yandex-brand-icon')).toHaveTextContent('Я');
  });
  it('hides the button when the server disables the provider', async () => {
    vi.mocked(yandexApi.providers).mockResolvedValue(false);
    const { container } = render(<YandexLogin secure nextPath="/app"/>);
    await act(async () => {});
    expect(container).toBeEmptyDOMElement();
  });
  it('handles unavailable capability quietly', async () => {
    vi.mocked(yandexApi.providers).mockRejectedValue(new YandexApiError(503));
    const { container } = render(<YandexLogin secure nextPath="/app"/>);
    await act(async () => {});
    expect(container).toBeEmptyDOMElement();
  });
  it('makes no request over HTTP', () => {
    render(<YandexLogin secure={false} nextPath="/app"/>);
    expect(yandexApi.providers).not.toHaveBeenCalled();
    expect(screen.queryByRole('link', { name: 'Войти с Яндекс ID' })).toBeNull();
  });
});

describe('account-level Yandex connection', () => {
  it('shows a connected identity without an unlink action', async () => {
    vi.mocked(yandexApi.identity).mockResolvedValue(true);
    render(<YandexConnection secure csrfToken={csrfA}/>);
    expect(await screen.findByText('Яндекс ID подключён.')).toBeInTheDocument();
    expect(screen.queryByRole('button')).toBeNull();
  });
  it('offers explicit connection only after a confirmed unlinked read', async () => {
    const identity = deferred<boolean>(); vi.mocked(yandexApi.identity).mockReturnValue(identity.promise);
    render(<YandexConnection secure csrfToken={csrfA}/>);
    expect(await screen.findByText('Проверяем подключение Яндекс ID…')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Подключить Яндекс ID' })).toBeNull();
    await act(async () => identity.resolve(false));
    expect(screen.getByRole('button', { name: 'Подключить Яндекс ID' })).toBeEnabled();
  });
  it.each([null, '', 'short'])('makes no authenticated request without current CSRF: %s', async csrf => {
    const { container } = render(<YandexConnection secure csrfToken={csrf}/>);
    await act(async () => {});
    expect(yandexApi.identity).not.toHaveBeenCalled(); expect(container).toBeEmptyDOMElement();
  });
  it('hides connection when provider capability is disabled', async () => {
    vi.mocked(yandexApi.providers).mockResolvedValue(false);
    const { container } = render(<YandexConnection secure csrfToken={csrfA}/>);
    await act(async () => {});
    expect(yandexApi.identity).not.toHaveBeenCalled(); expect(container).toBeEmptyDOMElement();
  });
  it.each([401, 403])('never presents an auth failure %s as an unlinked identity', async status => {
    vi.mocked(yandexApi.identity).mockRejectedValue(new YandexApiError(status));
    const expired = vi.fn();
    render(<YandexConnection secure csrfToken={csrfA} onSessionExpired={expired}/>);
    expect(await screen.findByRole('alert')).toHaveTextContent(/Войдите.*снова/);
    expect(screen.queryByRole('button', { name: 'Подключить Яндекс ID' })).toBeNull();
    expect(expired).toHaveBeenCalledTimes(status === 401 ? 1 : 0);
  });
  it('displays a failed identity check honestly without exposing server text', async () => {
    vi.mocked(yandexApi.identity).mockRejectedValue(new Error('private-provider-detail'));
    render(<YandexConnection secure csrfToken={csrfA}/>);
    expect(await screen.findByRole('alert')).toHaveTextContent('Не удалось проверить');
    expect(screen.queryByText(/private-provider-detail/)).toBeNull();
    expect(screen.queryByRole('button', { name: 'Подключить Яндекс ID' })).toBeNull();
  });
  it('sends the current scope and disables repeat linking while pending', async () => {
    const start = deferred<string>(); const link = vi.spyOn(yandexApi, 'startLink').mockReturnValue(start.promise);
    render(<YandexConnection secure csrfToken={csrfA}/>);
    fireEvent.click(await screen.findByRole('button', { name: 'Подключить Яндекс ID' }));
    expect(screen.getByRole('button', { name: 'Подключаем Яндекс ID…' })).toBeDisabled();
    expect(link).toHaveBeenCalledWith(csrfA, expect.any(AbortSignal));
    await act(async () => start.resolve(validUrl));
    expect(assign).toHaveBeenCalledOnce(); expect(assign).toHaveBeenCalledWith(validUrl);
  });
  it('rejects arbitrary navigation even when returned from the link adapter', async () => {
    vi.spyOn(yandexApi, 'startLink').mockResolvedValue('https://evil.test/');
    render(<YandexConnection secure csrfToken={csrfA}/>);
    fireEvent.click(await screen.findByRole('button', { name: 'Подключить Яндекс ID' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Не удалось начать');
    expect(assign).not.toHaveBeenCalled();
  });
  it.each([401, 403])('shows expired/forbidden linking %s without a fake connection', async status => {
    vi.spyOn(yandexApi, 'startLink').mockRejectedValue(new YandexApiError(status));
    render(<YandexConnection secure csrfToken={csrfA}/>);
    fireEvent.click(await screen.findByRole('button', { name: 'Подключить Яндекс ID' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(/Войдите.*снова/);
    expect(screen.queryByText('Яндекс ID подключён.')).toBeNull(); expect(assign).not.toHaveBeenCalled();
  });
  it('ignores a previous account identity result and aborts its request', async () => {
    const first = deferred<boolean>(), second = deferred<boolean>();
    vi.mocked(yandexApi.identity).mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    const { rerender } = render(<YandexConnection secure csrfToken={csrfA}/>);
    await waitFor(() => expect(yandexApi.identity).toHaveBeenCalledOnce());
    const firstSignal = vi.mocked(yandexApi.identity).mock.calls[0]![0]!;
    rerender(<YandexConnection secure csrfToken={csrfB}/>);
    await waitFor(() => expect(yandexApi.identity).toHaveBeenCalledTimes(2));
    expect(firstSignal.aborted).toBe(true);
    await act(async () => first.resolve(true));
    expect(screen.queryByText('Яндекс ID подключён.')).toBeNull();
    await act(async () => second.resolve(false));
    expect(screen.getByRole('button', { name: 'Подключить Яндекс ID' })).toBeEnabled();
  });
  it('clears a previously connected identity synchronously on account change', async () => {
    vi.mocked(yandexApi.identity).mockResolvedValueOnce(true).mockReturnValueOnce(deferred<boolean>().promise);
    const { rerender } = render(<YandexConnection secure csrfToken={csrfA}/>);
    await screen.findByText('Яндекс ID подключён.');
    rerender(<YandexConnection secure csrfToken={csrfB}/>);
    expect(screen.queryByText('Яндекс ID подключён.')).toBeNull();
  });
  it.each(['changed', 'logout', 'unmount'])('ignores a late link completion after %s', async change => {
    const start = deferred<string>(); const link = vi.spyOn(yandexApi, 'startLink').mockReturnValue(start.promise);
    const { rerender, unmount } = render(<YandexConnection secure csrfToken={csrfA}/>);
    fireEvent.click(await screen.findByRole('button', { name: 'Подключить Яндекс ID' }));
    const signal = link.mock.calls[0]![1]!;
    if (change === 'unmount') unmount();
    else rerender(<YandexConnection secure csrfToken={change === 'logout' ? null : csrfB}/>);
    expect(signal.aborted).toBe(true);
    await act(async () => start.resolve(validUrl));
    expect(assign).not.toHaveBeenCalled();
    expect(screen.queryByText('Яндекс ID подключён.')).toBeNull();
  });
});
