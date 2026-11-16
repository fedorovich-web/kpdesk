import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { AccountAccess } from './AccountAccess';
import { SessionApiError, sessionApi } from '../tenant-session';
import { yandexApi, YandexApiError } from '../yandex-api';

beforeEach(() => {
  // Optional provider discovery must never forward real requests in account tests.
  vi.spyOn(yandexApi, 'providers').mockResolvedValue(false);
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  window.history.replaceState({}, '', '/');
});

describe('HTTPS-only tenant account pages', () => {
  it('renders a real registration screen without exposing a password over HTTP', () => {
    window.history.replaceState({}, '', '/auth/register');
    const fetchSpy = vi.fn();
    vi.stubGlobal('fetch', fetchSpy);
    render(<AccountAccess/>);
    expect(screen.getByRole('heading', { name: 'Создать аккаунт' })).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('HTTPS');
    expect(screen.getByLabelText('Название организации')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Зарегистрироваться' })).toBeDisabled();
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('offers resend, reset and login without mixing with old demo', () => {
    window.history.replaceState({}, '', '/auth/login');
    render(<AccountAccess/>);
    fireEvent.click(screen.getByRole('button', { name: 'Отправить письмо повторно' }));
    expect(screen.getByRole('heading', { name: 'Повторное подтверждение почты' })).toBeInTheDocument();
    expect(window.location.pathname).toBe('/auth/resend-confirmation');
    fireEvent.click(screen.getByRole('button', { name: 'Забыли пароль?' }));
    expect(screen.getByRole('heading', { name: 'Восстановление доступа' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Открыть демонстрацию' }))
      .toHaveAttribute('href', '/demo');
  });

  it('removes one-time tokens from address bar after loading email link', () => {
    window.history.replaceState({}, '', '/auth/verify-email?token=' + 'a'.repeat(43));
    render(<AccountAccess />);
    expect(window.location.search).toBe('');
    expect(screen.getByRole('button', { name: 'Подтвердить почту' })).toBeDisabled();
  });
});

describe('session HTTP adapter', () => {
  it('rejects plain HTTP before sending credentials', async () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal('fetch', fetchSpy);
    await expect(sessionApi.login('one@example.test', 'secure-password'))
      .rejects.toBeInstanceOf(SessionApiError);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('uses same-origin credential cookies and no persistent token store', async () => {
    vi.stubGlobal('window', { location: { protocol: 'https:' } });
    const fetchSpy = vi.fn().mockResolvedValue({
      ok: true, status: 200,
      json: async () => ({ csrf_token: 'in-memory-csrf', expires_at: '2026-10-10T21:00:00Z' }),
    });
    vi.stubGlobal('fetch', fetchSpy);
    const login = await sessionApi.login('owner@example.test', 'long-password');
    expect(login.csrf_token).toBe('in-memory-csrf');
    expect(fetchSpy).toHaveBeenCalledWith(
      '/api/session/login',
      expect.objectContaining({
        credentials: 'include', cache: 'no-store',
        body: JSON.stringify({ email: 'owner@example.test', password: 'long-password' }),
      }),
    );
  });
});

describe('A64 organization catalog response isolation', () => {
  it('ignores a catalog response that arrives after changing the organization', async () => {
    const originalWindow = window;
    const location = { protocol: 'https:', pathname: '/auth/organizations', search: '' };
    const securedWindow = Object.create(originalWindow) as Window;
    Object.defineProperty(securedWindow, 'location', { value: location });
    vi.stubGlobal('window', securedWindow);
    vi.spyOn(sessionApi, 'resume').mockResolvedValue({
      user_id: 'user-a', csrf_token: 'restored-csrf',
    });
    vi.spyOn(sessionApi, 'organizations').mockResolvedValue({
      organizations: [
        { id: 'org-a', name: 'Old organization', role: 'viewer' },
        { id: 'org-b', name: 'New organization', role: 'viewer' },
      ],
    });
    vi.spyOn(sessionApi, 'invitations').mockResolvedValue([]);
    vi.spyOn(sessionApi, 'listQuotes').mockResolvedValue({ items: [] });
    let resolveFirst!: (value: Awaited<ReturnType<typeof sessionApi.currentCatalog>>) => void;
    const fetchCatalog = vi.spyOn(sessionApi, 'currentCatalog')
      .mockImplementation(async id => id === 'org-a'
        ? await new Promise(resolve => { resolveFirst = resolve; })
        : { version_id: 'catalog-b', source_sha256: 'b', rows_count: 2 });
    render(<AccountAccess/>);
    const organization = await screen.findByLabelText('Организация');
    await waitFor(() => expect(fetchCatalog).toHaveBeenCalledWith('org-a'));
    fireEvent.change(organization, { target: { value: 'org-b' } });
    await waitFor(() => expect(fetchCatalog).toHaveBeenCalledWith('org-b'));
    expect(await screen.findByText('2')).toBeInTheDocument();
    await act(async () => {
      resolveFirst({ version_id: 'catalog-a', source_sha256: 'a', rows_count: 97 });
    });
    expect(screen.getByLabelText('Организация')).toHaveValue('org-b');
    expect(screen.getByText('2')).toBeInTheDocument();
    expect(screen.queryByText('97')).not.toBeInTheDocument();
  });
});

describe('A67 catalog preflight before publication', () => {
  it('blocks invalid files, requires confirmation, and binds publish to checked SHA', async () => {
    const securedWindow = Object.create(window) as Window;
    Object.defineProperty(securedWindow, 'location', {
      value: { protocol: 'https:', pathname: '/auth/login', search: '' },
    });
    vi.stubGlobal('window', securedWindow);
    vi.spyOn(sessionApi, 'login').mockResolvedValue({ csrf_token: 'test-csrf', expires_at: '2026-10-25T00:00:00Z' });
    vi.spyOn(sessionApi, 'organizations').mockResolvedValue({
      organizations: [{ id: 'org-a', name: 'Test', role: 'owner' }],
    });
    vi.spyOn(sessionApi, 'currentCatalog').mockRejectedValue(
      new SessionApiError('no_active_catalog', 'Нет каталога', 404),
    );
    vi.spyOn(sessionApi, 'invitations').mockResolvedValue([]);
    vi.spyOn(sessionApi, 'listQuotes').mockResolvedValue({ items: [] });
    const inspected = vi.spyOn(sessionApi, 'preflightCatalog')
      .mockResolvedValueOnce({
        source_sha256: 'a'.repeat(64), publishable: false, rows_count: 0,
        total_errors: 1,
        errors: [{ code: 'invalid_header', message: 'Неверные заголовки',
          row: 1, field: null }],
      }).mockResolvedValueOnce({
        source_sha256: 'b'.repeat(64), publishable: true, rows_count: 1,
        total_errors: 0, errors: [],
      });
    const published = vi.spyOn(sessionApi, 'importCatalog').mockResolvedValue({
      version_id: 'new-catalog', source_sha256: 'b'.repeat(64), rows_count: 1,
    });
    render(<AccountAccess />);
    fireEvent.change(screen.getByLabelText('Электронная почта'), {
      target: { value: 'owner@example.test' },
    });
    fireEvent.change(screen.getByLabelText('Пароль'), {
      target: { value: 'example-password' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Войти' }));
    const chooser = await screen.findByLabelText('Импорт прайс-листа');
    const button = screen.getByRole('button', { name: 'Загрузить прайс' });
    expect(button).toBeDisabled();
    fireEvent.change(chooser, { target: {
      files: [new File(['bad'], 'invalid.csv', { type: 'text/csv' })],
    } });
    fireEvent.click(screen.getByRole('button', { name: 'Проверить файл' }));
    expect(await screen.findByText(/Строка 1: Неверные заголовки/)).toBeInTheDocument();
    expect(button).toBeDisabled();
    expect(published).not.toHaveBeenCalled();

    const good = new File(['valid'], 'good.csv', { type: 'text/csv' });
    fireEvent.change(chooser, { target: { files: [good] } });
    expect(screen.queryByText(/Неверные заголовки/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Проверить файл' }));
    expect(await screen.findByText(/готов к загрузке/)).toBeInTheDocument();
    expect(button).toBeDisabled();
    fireEvent.click(screen.getByLabelText(
      'Я проверил файл и подтверждаю загрузку новой версии прайса',
    ));
    expect(button).toBeEnabled();
    fireEvent.click(button);
    await waitFor(() => expect(published).toHaveBeenCalledWith(
      'org-a', good, expect.any(String), expect.any(String), 'b'.repeat(64),
    ));
    expect(inspected).toHaveBeenCalledTimes(2);
  });
});

describe('A75 persistent HTTPS session recovery', () => {
  it('restores CSRF after a page reload without a password or localStorage', async () => {
    const securedWindow = Object.create(window) as Window;
    Object.defineProperty(securedWindow, 'location', {
      value: { protocol: 'https:', pathname: '/auth/organizations', search: '' },
    });
    vi.stubGlobal('window', securedWindow);
    const resume = vi.spyOn(sessionApi, 'resume').mockResolvedValue({
      user_id: 'user-a', csrf_token: 'recovered-csrf',
    });
    const organizations = vi.spyOn(sessionApi, 'organizations').mockResolvedValue({
      organizations: [{ id: 'org-a', name: 'Recovered organization', role: 'owner' }],
    });
    vi.spyOn(sessionApi, 'currentCatalog').mockRejectedValue(
      new SessionApiError('no_active_catalog', 'No catalog', 404),
    );
    vi.spyOn(sessionApi, 'invitations').mockResolvedValue([]);
    vi.spyOn(sessionApi, 'listQuotes').mockResolvedValue({ items: [] });
    render(<AccountAccess/>);
    await waitFor(() => expect(resume).toHaveBeenCalledOnce());
    await waitFor(() => expect(organizations).toHaveBeenCalledOnce());
    expect(await screen.findByLabelText('Организация')).toHaveValue('org-a');
    expect(screen.queryByText(/CSRF-токен хранится только в памяти/)).toBeNull();
    expect(screen.getByRole('button', { name: 'Выйти' })).toBeInTheDocument();
  });

  it('on revoked/expired sessions goes to login rather than pretending authorization', async () => {
    const securedWindow = Object.create(window) as Window;
    Object.defineProperty(securedWindow, 'location', {
      value: { protocol: 'https:', pathname: '/auth/organizations', search: '' },
    });
    vi.stubGlobal('window', securedWindow);
    vi.spyOn(sessionApi, 'resume').mockRejectedValue(
      new SessionApiError('invalid_session', 'Expired', 401),
    );
    const list = vi.spyOn(sessionApi, 'organizations');
    render(<AccountAccess/>);
    expect(await screen.findByRole('heading', { name: 'Вход в KPDesk' }))
      .toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('Сессия завершилась');
    expect(list).not.toHaveBeenCalled();
  });
});

describe('canonical authenticated workspace', () => {
  it.each(['resolve', 'reject'] as const)('ignores a late login %s after changing the auth screen', async outcome => {
    const secureWindow = Object.create(window) as Window;
    Object.defineProperty(secureWindow, 'location', {
      value: { protocol: 'https:', pathname: '/auth/login', search: '' },
    });
    vi.stubGlobal('window', secureWindow);
    let resolve!: (value: Awaited<ReturnType<typeof sessionApi.login>>) => void;
    let reject!: (reason: Error) => void;
    vi.spyOn(sessionApi, 'login').mockImplementation(() => new Promise((yes, no) => {
      resolve = yes; reject = no;
    }));
    const organizations = vi.spyOn(sessionApi, 'organizations').mockResolvedValue({ organizations: [] });
    render(<AccountAccess />);
    fireEvent.change(screen.getByLabelText('Электронная почта'), { target: { value: 'test@example.test' } });
    fireEvent.change(screen.getByLabelText('Пароль'), { target: { value: 'test-login-password' } });
    fireEvent.click(screen.getByRole('button', { name: 'Войти' }));
    fireEvent.click(screen.getByRole('button', { name: 'Забыли пароль?' }));
    await act(async () => {
      if (outcome === 'resolve') resolve({ csrf_token: 'stale-csrf', expires_at: '2026-11-12T23:00:00Z' });
      else reject(new Error('Obsolete login failure'));
    });
    expect(screen.getByRole('heading', { name: 'Восстановление доступа' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Отправить ссылку' })).toBeEnabled();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(organizations).not.toHaveBeenCalled();
  });

  it('uses /app after login while keeping tenant API and data scoped to the organization', async () => {
    const secureWindow = Object.create(window) as Window;
    Object.defineProperty(secureWindow, 'location', {
      value: { protocol: 'https:', pathname: '/auth/login', search: '' },
    });
    vi.stubGlobal('window', secureWindow);
    const navigation = vi.spyOn(window.history, 'pushState');
    vi.spyOn(sessionApi, 'login').mockResolvedValue({
      csrf_token: 'test-csrf', expires_at: '2026-10-26T23:00:00Z',
    });
    vi.spyOn(sessionApi, 'organizations').mockResolvedValue({
      organizations: [{ id: 'org-a', name: 'Test tenant', role: 'viewer' }],
    });
    vi.spyOn(sessionApi, 'currentCatalog').mockResolvedValue({
      version_id: 'cat-a', source_sha256: 'hash', rows_count: 4,
    });
    vi.spyOn(sessionApi, 'invitations').mockResolvedValue([]);
    vi.spyOn(sessionApi, 'listQuotes').mockResolvedValue({ items: [] });
    render(<AccountAccess />);
    fireEvent.change(screen.getByLabelText('Электронная почта'), {
      target: { value: 'test@example.test' },
    });
    fireEvent.change(screen.getByLabelText('Пароль'), {
      target: { value: 'test-login-password' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Войти' }));
    expect(await screen.findByRole('heading', { name: 'Личный кабинет KPDesk' }))
      .toBeInTheDocument();
    expect(navigation).toHaveBeenCalledWith({}, '', '/app');
    expect(screen.getByRole('navigation', { name: 'Разделы личного кабинета' }))
      .toHaveTextContent('Коммерческие предложения');
    expect(screen.queryByLabelText('Импорт прайс-листа')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Загрузить прайс' })).not.toBeInTheDocument();
  });
});

describe('membership refresh isolation', () => {
  it('drops revoked organization selection and never exposes its cached catalog', async () => {
    const securedWindow = Object.create(window) as Window;
    Object.defineProperty(securedWindow, 'location', {
      value: { protocol: 'https:', pathname: '/app', search: '' },
    });
    vi.stubGlobal('window', securedWindow);
    vi.spyOn(sessionApi, 'resume').mockResolvedValue({
      user_id: 'user-a', csrf_token: 'restored-csrf',
    });
    vi.spyOn(sessionApi, 'organizations')
      .mockResolvedValueOnce({ organizations: [
        { id: 'org-a', name: 'Former tenant', role: 'viewer' },
        { id: 'org-b', name: 'Current tenant', role: 'viewer' },
      ] })
      .mockResolvedValueOnce({ organizations: [
        { id: 'org-b', name: 'Current tenant', role: 'viewer' },
      ] });
    vi.spyOn(sessionApi, 'currentCatalog').mockImplementation(async org => ({
      version_id: org + '-catalog', source_sha256: org,
      rows_count: org === 'org-a' ? 97 : 2,
    }));
    vi.spyOn(sessionApi, 'invitations').mockResolvedValue([]);
    vi.spyOn(sessionApi, 'listQuotes').mockResolvedValue({ items: [] });
    render(<AccountAccess />);
    expect(await screen.findByText('97')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Обновить список компаний' }));
    await waitFor(() => expect(screen.getByLabelText('Организация')).toHaveValue('org-b'));
    expect(screen.queryByText('97')).not.toBeInTheDocument();
    expect(await screen.findByText('2')).toBeInTheDocument();
  });
});

describe('expired session protection on workspace refresh', () => {
  it('immediately clears the workspace on 401 rather than leaving stale tenant data', async () => {
    const securedWindow = Object.create(window) as Window;
    Object.defineProperty(securedWindow, 'location', {
      value: { protocol: 'https:', pathname: '/app', search: '' },
    });
    vi.stubGlobal('window', securedWindow);
    vi.spyOn(sessionApi, 'resume').mockResolvedValue({
      user_id: 'user-a', csrf_token: 'restored-csrf',
    });
    vi.spyOn(sessionApi, 'organizations')
      .mockResolvedValueOnce({ organizations: [
        { id: 'org-a', name: 'Expired tenant', role: 'viewer' },
      ] })
      .mockRejectedValueOnce(new SessionApiError('invalid_session', 'Expired', 401));
    vi.spyOn(sessionApi, 'currentCatalog').mockResolvedValue({
      version_id: 'cat-a', source_sha256: 'hash', rows_count: 97,
    });
    vi.spyOn(sessionApi, 'invitations').mockResolvedValue([]);
    vi.spyOn(sessionApi, 'listQuotes').mockResolvedValue({ items: [] });
    render(<AccountAccess />);
    expect(await screen.findByText('97')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Обновить список компаний' }));
    expect(await screen.findByRole('heading', { name: 'Вход в KPDesk' }))
      .toBeInTheDocument();
    expect(screen.queryByText('97')).not.toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('Сессия завершилась');
  });
});

describe('Yandex access integration in the existing account screens', () => {
  function securedPage(path: string) {
    window.history.replaceState({}, '', path);
    const original = window;
    const secured = Object.create(original) as Window;
    const assign = vi.fn();
    Object.defineProperty(secured, 'location', { get: () => ({
      protocol: 'https:', pathname: original.location.pathname,
      search: original.location.search, hash: original.location.hash,
      assign,
    }) });
    vi.stubGlobal('window', secured);
    vi.stubGlobal('fetch', vi.fn(() => { throw new Error('Unexpected network request'); }));
    return assign;
  }
  it.each(['/auth/login', '/auth/register'])('adds optional Yandex access on %s and preserves password access', async path => {
    securedPage(path); vi.mocked(yandexApi.providers).mockResolvedValue(true);
    render(<AccountAccess/>);
    const yandex = await screen.findByRole('link', { name: path.endsWith('register')
      ? 'Зарегистрироваться с Яндекс ID' : 'Войти с Яндекс ID' });
    expect(yandex).toHaveAttribute('href', '/api/session/yandex/start?next=/app');
    expect(screen.getByText('или с помощью почты')).toBeInTheDocument();
    const email = screen.getByLabelText('Электронная почта');
    expect(yandex.compareDocumentPosition(email) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(email).toBeInTheDocument();
    expect(screen.getByLabelText(/^Пароль/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: path.endsWith('register') ? 'Зарегистрироваться' : 'Войти' }))
      .toBeEnabled();
  });
  it.each([
    ['%2Fadmin', '/admin'], ['%2Fapp', '/app'], ['https%3A%2F%2Fevil.test', '/app'], ['%2Fadmin%2F', '/app'],
  ])('uses exact allowed next target %s', async (query, target) => {
    securedPage('/auth/login?next=' + query); vi.mocked(yandexApi.providers).mockResolvedValue(true);
    render(<AccountAccess/>);
    expect(await screen.findByRole('link', { name: 'Войти с Яндекс ID' }))
      .toHaveAttribute('href', '/api/session/yandex/start?next=' + target);
  });
  it('shows fixed linking help and removes only OAuth feedback from the current URL', () => {
    securedPage('/auth/login?token=keep&oauth_error=yandex_account_link_required&next=%2Fadmin#keep');
    render(<AccountAccess/>);
    expect(screen.getByRole('alert')).toHaveTextContent(/Войдите.*парол.*подключите Яндекс/);
    expect(window.location.search).toBe('?token=keep&next=%2Fadmin');
    expect(window.location.hash).toBe('#keep');
  });
  it('never treats a callback error query as successful authentication', () => {
    securedPage('/auth/login?oauth_error=yandex_cancelled');
    const resume = vi.spyOn(sessionApi, 'resume');
    render(<AccountAccess/>);
    expect(screen.getByRole('alert')).toHaveTextContent('отменён');
    expect(screen.queryByRole('status')).toBeNull(); expect(resume).not.toHaveBeenCalled();
  });
  it('keeps password login usable when capability discovery fails', async () => {
    securedPage('/auth/login'); vi.mocked(yandexApi.providers).mockRejectedValue(new Error('Unavailable'));
    render(<AccountAccess/>); await act(async () => {});
    expect(screen.getByRole('button', { name: 'Войти' })).toBeEnabled();
    expect(screen.queryByRole('link', { name: 'Войти через Яндекс' })).toBeNull();
    expect(screen.queryByRole('alert')).toBeNull();
  });
  it('offers account-level connection even when the user has no organization', async () => {
    securedPage('/app'); vi.mocked(yandexApi.providers).mockResolvedValue(true);
    vi.spyOn(sessionApi, 'resume').mockResolvedValue({ user_id: 'user-a', csrf_token: 'a'.repeat(43) });
    vi.spyOn(sessionApi, 'organizations').mockResolvedValue({ organizations: [] });
    vi.spyOn(yandexApi, 'identity').mockResolvedValue(false);
    render(<AccountAccess/>);
    expect(await screen.findByRole('button', { name: 'Подключить Яндекс ID' })).toBeEnabled();
    expect(screen.getByRole('heading', { name: 'Личный кабинет KPDesk' })).toBeInTheDocument();
  });
  it('clears a connected identity after logout', async () => {
    securedPage('/app'); vi.mocked(yandexApi.providers).mockResolvedValue(true);
    vi.spyOn(sessionApi, 'resume').mockResolvedValue({ user_id: 'user-a', csrf_token: 'a'.repeat(43) });
    vi.spyOn(sessionApi, 'organizations').mockResolvedValue({ organizations: [] });
    vi.spyOn(yandexApi, 'identity').mockResolvedValue(true);
    vi.spyOn(sessionApi, 'logout').mockResolvedValue(null);
    render(<AccountAccess/>);
    await screen.findByText('Яндекс ID подключён.');
    fireEvent.click(screen.getByRole('button', { name: 'Выйти' }));
    await screen.findByRole('heading', { name: 'Вход в KPDesk' });
    expect(screen.queryByText('Яндекс ID подключён.')).toBeNull();
  });
  it('aborts linking as soon as logout begins, before the logout response arrives', async () => {
    const assign = securedPage('/app'); vi.mocked(yandexApi.providers).mockResolvedValue(true);
    vi.spyOn(sessionApi, 'resume').mockResolvedValue({ user_id: 'user-a', csrf_token: 'a'.repeat(43) });
    vi.spyOn(sessionApi, 'organizations').mockResolvedValue({ organizations: [] });
    vi.spyOn(yandexApi, 'identity').mockResolvedValue(false);
    let finishLink!: (url: string) => void;
    let finishLogout!: (value: null) => void;
    const link = vi.spyOn(yandexApi, 'startLink').mockImplementation(() => new Promise(resolve => { finishLink = resolve; }));
    vi.spyOn(sessionApi, 'logout').mockImplementation(() => new Promise(resolve => { finishLogout = resolve; }));
    render(<AccountAccess/>);
    fireEvent.click(await screen.findByRole('button', { name: 'Подключить Яндекс ID' }));
    const signal = link.mock.calls[0]![1]!;
    fireEvent.click(screen.getByRole('button', { name: 'Выйти' }));
    expect(signal.aborted).toBe(true);
    await act(async () => { finishLink('https://oauth.yandex.ru/authorize?ignored=stale'); finishLogout(null); });
    expect(assign).not.toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: 'Подключить Яндекс ID' })).toBeNull();
  });
  it('clears the existing session when the identity endpoint rejects it', async () => {
    securedPage('/app'); vi.mocked(yandexApi.providers).mockResolvedValue(true);
    vi.spyOn(sessionApi, 'resume').mockResolvedValue({ user_id: 'user-a', csrf_token: 'a'.repeat(43) });
    vi.spyOn(sessionApi, 'organizations').mockResolvedValue({ organizations: [] });
    vi.spyOn(yandexApi, 'identity').mockRejectedValue(new YandexApiError(401));
    render(<AccountAccess/>);
    expect(await screen.findByRole('heading', { name: 'Вход в KPDesk' })).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('Сессия завершилась');
    expect(screen.queryByRole('button', { name: 'Выйти' })).toBeNull();
  });
});
