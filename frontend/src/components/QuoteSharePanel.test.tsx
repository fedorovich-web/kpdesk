import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { sessionApi, SessionApiError } from '../tenant-session';
import type { TenantQuote } from '../tenant-session';
import { QuoteSharePanel } from './QuoteSharePanel';

const quote: TenantQuote = { quote_id: 'q', revision: 2, snapshot_sha256: 'a'.repeat(64),
  catalog_version_id: 'cat', ready_for_draft: true, total_rub: '10.00', approved: true, lines: [] };
const share = { id: 'share1', revision: 2, created_at: '2026-11-04T12:00:00Z',
  expires_at: '2099-11-11T12:00:00Z', revoked_at: null };
beforeEach(() => {
  vi.spyOn(sessionApi, 'quoteShares').mockResolvedValue({ available: true, shares: [] });
  vi.spyOn(sessionApi, 'createQuoteShare').mockResolvedValue({ share, token: 'a'.repeat(43) });
  vi.spyOn(sessionApi, 'revokeQuoteShare').mockResolvedValue(null);
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: vi.fn().mockResolvedValue(undefined) } });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });
const mount = () => render(<QuoteSharePanel organizationId="org" quote={quote} csrf="csrf" canWrite/>);

it('creates once, copies the fragment link, and revokes without retaining the token', async () => {
  mount(); fireEvent.click(await screen.findByRole('button', { name: 'Создать ссылку для клиента' }));
  const input = await screen.findByLabelText('Ссылка для клиента');
  expect(input).toHaveValue(window.location.origin + '/proposal#' + 'a'.repeat(43));
  fireEvent.click(screen.getByRole('button', { name: 'Копировать ссылку' }));
  await screen.findByRole('status');
  expect(navigator.clipboard.writeText).toHaveBeenCalledWith(window.location.origin + '/proposal#' + 'a'.repeat(43));
  fireEvent.click(screen.getByRole('button', { name: /Отозвать ссылку/ }));
  await screen.findByText('Отозвана');
  expect(screen.queryByLabelText('Ссылка для клиента')).not.toBeInTheDocument();
});

it('hides issuance on schema 0042', async () => {
  vi.mocked(sessionApi.quoteShares).mockResolvedValue({ available: false, shares: [] });
  mount(); await act(async () => {});
  expect(screen.queryByRole('button', { name: 'Создать ссылку для клиента' })).not.toBeInTheDocument();
});

it('keeps an uncertain creation recoverable through the list without automatic retry', async () => {
  vi.mocked(sessionApi.createQuoteShare).mockRejectedValue(new Error('lost response'));
  mount(); fireEvent.click(await screen.findByRole('button', { name: 'Создать ссылку для клиента' }));
  expect(await screen.findByRole('alert')).toHaveTextContent(/обновите список/i);
  expect(sessionApi.createQuoteShare).toHaveBeenCalledTimes(1);
  expect(screen.getByRole('button', { name: 'Создать ссылку для клиента' })).toBeDisabled();
  vi.mocked(sessionApi.quoteShares).mockResolvedValue({ available: true, shares: [share] });
  fireEvent.click(screen.getByRole('button', { name: 'Обновить список ссылок' }));
  await screen.findByRole('button', { name: /Отозвать ссылку/ });
});

it('discards a late create response after the quote scope changes', async () => {
  let resolve!: (value: { share: typeof share; token: string }) => void;
  vi.mocked(sessionApi.createQuoteShare).mockImplementation(() => new Promise(done => { resolve = done; }));
  const view = mount();
  fireEvent.click(await screen.findByRole('button', { name: 'Создать ссылку для клиента' }));
  view.rerender(<QuoteSharePanel organizationId="other" quote={{ ...quote, quote_id: 'other-q' }} csrf="other" canWrite/>);
  await act(async () => resolve({ share, token: 'a'.repeat(43) }));
  expect(screen.queryByLabelText('Ссылка для клиента')).not.toBeInTheDocument();
});

it('keeps existing links revocable after the current head becomes an unapproved draft', async () => {
  vi.mocked(sessionApi.quoteShares).mockResolvedValue({ available: true, shares: [share] });
  render(<QuoteSharePanel organizationId="org" quote={{ ...quote, revision: 3, approved: false }} csrf="csrf" canWrite/>);
  fireEvent.click(await screen.findByRole('button', { name: /Отозвать ссылку/ }));
  await screen.findByText('Отозвана');
  expect(screen.queryByRole('button', { name: 'Создать ссылку для клиента' })).not.toBeInTheDocument();
});

it('treats a malformed successful response as an uncertain creation result', async () => {
  vi.mocked(sessionApi.createQuoteShare).mockRejectedValue(new SessionApiError('invalid_response', 'Некорректный ответ', 201));
  mount(); fireEvent.click(await screen.findByRole('button', { name: 'Создать ссылку для клиента' }));
  expect(await screen.findByRole('alert')).toHaveTextContent(/обновите список/i);
  expect(screen.getByRole('button', { name: 'Создать ссылку для клиента' })).toBeDisabled();
});
