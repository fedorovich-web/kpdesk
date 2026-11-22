import { cleanup, render, screen, fireEvent, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ServiceAdmin } from './ServiceAdmin';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

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
      input.includes('/overview') ? {
        as_of: '2026-11-22T13:00:00Z', period_days: 30, users_total: 10, users_new: 2, users_verified: 8,
        organizations_total: 6, organizations_active: 5, paid_organizations: 3, expiring_7: 1,
        plans: [], receipts: [], refunds: [], unresolved_reversals: 0, ambiguous_orders: 0,
        quotes_new: 8, organizations_with_catalog: 4, ai_attempts: 9, ai_success: 8,
      } : { total: 0, items: [] },
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
});
