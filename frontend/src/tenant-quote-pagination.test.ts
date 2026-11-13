import { afterEach, expect, it, vi } from 'vitest';
import { sessionApi } from './tenant-session';

afterEach(() => vi.unstubAllGlobals());

it('keeps the first-page URL and passes the server continuation as an encoded query parameter', async () => {
  const securedWindow = Object.create(window) as Window;
  Object.defineProperty(securedWindow, 'location', { value: { protocol: 'https:' } });
  vi.stubGlobal('window', securedWindow);
  const fetch = vi.fn().mockImplementation(async () => new Response(JSON.stringify({ items: [], next_cursor: null }), {
    status: 200, headers: { 'Content-Type': 'application/json' },
  }));
  vi.stubGlobal('fetch', fetch);
  await sessionApi.listQuotes('org/example');
  expect(fetch.mock.calls[0]![0]).toBe('/api/session/organizations/org%2Fexample/quotes');
  await sessionApi.listQuotes('org/example', 'cursor&organization=other');
  expect(fetch.mock.calls[1]![0]).toBe(
    '/api/session/organizations/org%2Fexample/quotes?cursor=cursor%26organization%3Dother',
  );
});
