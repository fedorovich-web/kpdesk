import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { PublicHeader } from './PublicChrome';

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('authenticated public navigation', () => {
  it('updates both desktop header and React mobile drawer after server verification', async () => {
    const request = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValue({ status: 200 } as Response);
    render(<PublicHeader />);
    await waitFor(() => expect(screen.getByRole('link', { name: 'Личный кабинет' }))
      .toHaveAttribute('href', '/app'));
    fireEvent.click(screen.getByRole('button', { name: 'Открыть меню' }));
    const drawer = screen.getByRole('navigation', { name: 'Мобильное меню' });
    expect(drawer.querySelector('a[href="/app"]')?.textContent).toBe('Личный кабинет');
    expect(request).toHaveBeenCalledOnce();
  });

  it('keeps login available when session validation returns 401', async () => {
    const request = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValue({ status: 401 } as Response);
    render(<PublicHeader />);
    await waitFor(() => expect(request).toHaveBeenCalledOnce());
    expect(screen.getByRole('link', { name: 'Войти' })).toHaveAttribute('href', '/auth/login');
  });
});
