import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { SharedProposalPage } from './SharedProposalPage';

beforeEach(() => {
  window.history.replaceState(null, '', '/proposal#' + 'a'.repeat(43));
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true,
    headers: new Headers({ 'content-type': 'application/pdf' }), blob: async () => new Blob(['%PDF-fixed'], { type: 'application/pdf' }) }));
  URL.createObjectURL = vi.fn(() => 'blob:proposal');
  URL.revokeObjectURL = vi.fn();
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); window.history.replaceState(null, '', '/'); });

it('loads the fixed PDF without sending account cookies or a token in the request URL', async () => {
  render(<SharedProposalPage/>);
  expect(await screen.findByRole('link', { name: 'Скачать PDF' })).toHaveAttribute('href', 'blob:proposal');
  expect(fetch).toHaveBeenCalledWith('/api/public/proposal/pdf', expect.objectContaining({
    method: 'POST', credentials: 'omit', cache: 'no-store', referrerPolicy: 'no-referrer',
    headers: { 'X-Proposal-Token': 'a'.repeat(43) },
  }));
});

it('shows the same unavailable screen for revoked and expired links', async () => {
  vi.mocked(fetch).mockResolvedValue(new Response('{}', { status: 404 }));
  render(<SharedProposalPage/>);
  expect(await screen.findByRole('alert')).toHaveTextContent('Ссылка недоступна');
  expect(screen.queryByRole('link', { name: 'Скачать PDF' })).not.toBeInTheDocument();
});

it('does not call the API for a missing or malformed fragment', async () => {
  window.history.replaceState(null, '', '/proposal#bad');
  render(<SharedProposalPage/>);
  expect(await screen.findByRole('alert')).toHaveTextContent('Ссылка недоступна');
  expect(fetch).not.toHaveBeenCalled();
});

it('cleans up the in-memory PDF on unmount', async () => {
  const view = render(<SharedProposalPage/>);
  await screen.findByRole('link', { name: 'Скачать PDF' }); view.unmount();
  expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:proposal');
});

it('replaces the previous PDF when another fragment link opens in the same tab', async () => {
  render(<SharedProposalPage/>);
  await screen.findByRole('link', { name: 'Скачать PDF' });
  vi.mocked(fetch).mockResolvedValue(new Response('{}', { status: 404 }));
  await act(async () => {
    window.history.replaceState(null, '', '/proposal#' + 'b'.repeat(43));
    window.dispatchEvent(new HashChangeEvent('hashchange'));
  });
  expect(await screen.findByRole('alert')).toHaveTextContent('Ссылка недоступна');
  expect(screen.queryByRole('link', { name: 'Скачать PDF' })).not.toBeInTheDocument();
  expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:proposal');
});
