// @vitest-environment-options {"url":"https://testserver/admin"}
import { cleanup, render, screen, fireEvent, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ServiceAdmin } from './ServiceAdmin';

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

const overview = {
  as_of: '2026-11-22T13:00:00Z', period_days: 30, users_total: 10, users_new: 2, users_verified: 8,
  organizations_total: 6, organizations_active: 5, paid_organizations: 3, expiring_7: 1,
  plans: [], receipts: [], refunds: [], unresolved_reversals: 0, ambiguous_orders: 0,
  quotes_new: 8, organizations_with_catalog: 4, ai_attempts: 9, ai_success: 8,
};

const client = {
  id: '58f8f469-1a84-4d8a-994a-b3884faa2eb8', email: 'client@example.test', status: 'active',
  created_at: '2026-11-20T13:00:00Z', email_verified_at: '2026-11-20T14:00:00Z', organizations_count: 1,
  is_blocked: false, can_manage: true,
};

describe('service owner dashboard', () => {
  it('offers the existing sign-in with an owner return path on 401', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(null, { status: 401 })));
    render(<ServiceAdmin />);
    expect(await screen.findByRole('link', { name: 'Войти' })).toHaveAttribute('href', '/auth/login?next=%2Fadmin');
    expect(screen.queryByRole('navigation', { name: 'Разделы кабинета владельца' })).toBeNull();
  });

  it('keeps the dashboard hidden on 403', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(null, { status: 403 })));
    render(<ServiceAdmin />);
    expect(await screen.findByRole('alert')).toHaveTextContent('доступен владельцу');
    expect(screen.queryByText('Активные платные периоды')).toBeNull();
  });

  it('shows actual counts and sends bounded search for the selected list', async () => {
    const fetch = vi.fn().mockImplementation((input: string) => Promise.resolve(Response.json(
      input.includes('/overview') ? overview : { total: 0, items: [] },
    )));
    vi.stubGlobal('fetch', fetch);
    render(<ServiceAdmin />);
    expect(await screen.findByText('Активные платные периоды')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Клиенты' }));
    await screen.findByText('Записей нет.');
    fireEvent.change(screen.getByRole('searchbox', { name: 'Поиск' }), { target: { value: 'client@example.test' } });
    fireEvent.click(screen.getByRole('button', { name: 'Найти' }));
    await waitFor(() => expect(fetch).toHaveBeenLastCalledWith(
      '/api/admin/users?page=1&q=client%40example.test', expect.objectContaining({ cache: 'no-store' }),
    ));
  });

  it('confirms a block, sends the session CSRF token, and shows the resulting status', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValueOnce(false).mockReturnValue(true);
    const fetch = vi.fn(async (input: string, options?: RequestInit) => {
      if (input.includes('/overview')) return Response.json(overview);
      if (input.endsWith('/resume')) return Response.json({ user_id: 'owner', csrf_token: 'csrf-value' });
      if (input.endsWith('/actions')) {
        if (options?.body === JSON.stringify({ action: 'unblock' })) {
          return Response.json({ id: client.id, status: 'active', is_blocked: false, sessions_revoked: 0 });
        }
        return Response.json({ id: client.id, status: 'disabled', is_blocked: true, sessions_revoked: 2 });
      }
      return Response.json({ total: 2, items: [client,
        { ...client, id: 'owner', email: 'owner@example.test', can_manage: false },
      ] });
    });
    vi.stubGlobal('fetch', fetch);
    render(<ServiceAdmin />);
    fireEvent.click(await screen.findByRole('button', { name: 'Клиенты' }));
    const block = await screen.findByRole('button', { name: /Заблокировать.*client@example.test/ });
    expect(screen.getByText('Защищённый аккаунт')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Заблокировать.*owner@example.test/ })).toBeNull();
    fireEvent.click(block);
    expect(confirm).toHaveBeenCalledWith(expect.stringContaining('client@example.test'));
    expect(fetch.mock.calls.map(([url]) => url)).not.toContain('/api/session/resume');
    fireEvent.click(block);
    expect(await screen.findByRole('button', { name: /Разблокировать.*client@example.test/ })).toBeEnabled();
    expect(screen.getByText('Заблокирован')).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('Завершено сеансов: 2');
    expect(fetch).toHaveBeenCalledWith(`/api/admin/users/${client.id}/actions`, expect.objectContaining({
      method: 'POST', headers: expect.objectContaining({ 'X-CSRF-Token': 'csrf-value' }),
      body: JSON.stringify({ action: 'block' }),
    }));
    fireEvent.click(screen.getByRole('button', { name: /Разблокировать.*client@example.test/ }));
    expect(await screen.findByRole('button', { name: /Заблокировать.*client@example.test/ })).toBeEnabled();
    expect(screen.getByRole('status')).toHaveTextContent('Аккаунт разблокирован.');
  });

  it('shows pending and failure states while keeping session revocation retryable', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    let complete!: (response: Response) => void;
    const fetch = vi.fn(async (input: string) => {
      if (input.includes('/overview')) return Response.json(overview);
      if (input.endsWith('/resume')) return Response.json({ user_id: 'owner', csrf_token: 'csrf-value' });
      if (input.endsWith('/actions')) return new Promise<Response>(resolve => { complete = resolve; });
      return Response.json({ total: 1, items: [client] });
    });
    vi.stubGlobal('fetch', fetch);
    render(<ServiceAdmin />);
    fireEvent.click(await screen.findByRole('button', { name: 'Клиенты' }));
    const revoke = await screen.findByRole('button', { name: /Завершить сеансы.*client@example.test/ });
    fireEvent.click(revoke);
    expect(revoke).toBeDisabled();
    expect(screen.getByRole('status')).toHaveTextContent('Выполняем действие');
    await waitFor(() => expect(complete).toBeDefined());
    complete(new Response(null, { status: 503 }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Не удалось выполнить действие');
    expect(revoke).toBeEnabled();
  });
});
