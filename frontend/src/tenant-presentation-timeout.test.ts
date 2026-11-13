import { afterEach, expect, it, vi } from 'vitest';
import { sessionApi } from './tenant-session';
import type { TenantQuote } from './tenant-session';

afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

function stalledFetch() {
  vi.useFakeTimers();
  const securedWindow = Object.create(window) as Window;
  Object.defineProperty(securedWindow, 'location', { value: { protocol: 'https:' } });
  vi.stubGlobal('window', securedWindow);
  let signal: AbortSignal | undefined;
  const fetchRequest = vi.fn((_path: string, options: RequestInit) => {
    signal = options.signal ?? undefined;
    return new Promise((_resolve, reject) => {
      signal?.addEventListener('abort', () => reject(Object.assign(new Error('Aborted'), { name: 'AbortError' })));
    });
  });
  vi.stubGlobal('fetch', fetchRequest);
  return { fetchRequest, aborted: () => signal?.aborted };
}

it.each(['extraction', 'presentation'] as const)(
  'waits for the bounded server deadline for %s, then aborts without resending', async operation => {
    const transport = stalledFetch();
    const quote = { quote_id: 'quote', revision: 1, snapshot_sha256: 'a'.repeat(64) } as TenantQuote;
    const request = operation === 'extraction'
      ? sessionApi.extractRequest('org', 'Учебная заявка: 2 пачки бумаги', 'csrf', true)
      : sessionApi.generatePresentation('org', quote,
        { issuer_name: 'Учебная компания', recipient_name: '', manager_brief: 'Учебная задача', commercial_terms: '' }, 'csrf', true);
    const checked = request.catch((cause: unknown) => cause);
    await vi.advanceTimersByTimeAsync(120_000);
    expect(transport.aborted()).toBe(false);
    await vi.advanceTimersByTimeAsync(15_000);
    expect(await checked).toMatchObject({ code: 'timeout' });
    expect(transport.aborted()).toBe(true);
    expect(transport.fetchRequest).toHaveBeenCalledTimes(1);
  },
);

it.each(['quotes', 'deterministic instructions'] as const)(
  'retains the ordinary deadline for %s and does not resend', async operation => {
    const transport = stalledFetch();
    const request = operation === 'quotes'
      ? sessionApi.listQuotes('org')
      : sessionApi.previewQuoteInstructions('org', 'Учебная наценка 10%', 'csrf');
    const checked = request.catch((cause: unknown) => cause);
    await vi.advanceTimersByTimeAsync(35_000);
    expect(await checked).toMatchObject({ code: 'timeout' });
    expect(transport.aborted()).toBe(true);
    expect(transport.fetchRequest).toHaveBeenCalledTimes(1);
  },
);

it('reports an interrupted provider operation without automatically resending it', async () => {
  const securedWindow = Object.create(window) as Window;
  Object.defineProperty(securedWindow, 'location', { value: { protocol: 'https:' } });
  vi.stubGlobal('window', securedWindow);
  const fetchRequest = vi.fn().mockRejectedValue(new TypeError('Connection interrupted'));
  vi.stubGlobal('fetch', fetchRequest);
  await expect(sessionApi.extractRequest('org', 'Учебная заявка', 'csrf', true))
    .rejects.toMatchObject({ code: 'network' });
  expect(fetchRequest).toHaveBeenCalledTimes(1);
});

it('keeps the operation key after network failure and rotates it after success', async () => {
  const securedWindow = Object.create(window) as Window;
  Object.defineProperty(securedWindow, 'location', { value: { protocol: 'https:' } });
  vi.stubGlobal('window', securedWindow);
  const fetchRequest = vi.fn()
    .mockRejectedValueOnce(new TypeError('Interrupted'))
    .mockResolvedValue({ ok: true, status: 200, json: async () => ({ source: 'openrouter' }) });
  vi.stubGlobal('fetch', fetchRequest);
  await expect(sessionApi.extractRequest('idempotency-org', 'Синтетическая заявка', 'csrf', true))
    .rejects.toMatchObject({ code: 'network' });
  await sessionApi.extractRequest('idempotency-org', 'Синтетическая заявка', 'csrf', true);
  await sessionApi.extractRequest('idempotency-org', 'Синтетическая заявка', 'csrf', true);
  const keys = fetchRequest.mock.calls.map(([, options]) =>
    (options.headers as Record<string, string>)['Idempotency-Key']);
  expect(keys[0]).toMatch(/^[0-9a-f-]{36}$/);
  expect(keys[0]).toBe(keys[1]);
  expect(keys[2]).not.toBe(keys[1]);
});

it.each([
  { status: 200, body: '<html>Broken response</html>', code: 'invalid_response' },
  { status: 200, body: '[]', code: 'invalid_response' },
  { status: 502, body: '<html>Proxy error</html>', code: 'http_error' },
  { status: 504, body: '<html>Gateway timeout</html>', code: 'http_error' },
  { status: 408, body: '', code: 'http_error' },
  { status: 204, body: null, code: 'invalid_response' },
])('keeps the operation identity after an uncertain HTTP $status response', async ({ status, body, code }) => {
  const securedWindow = Object.create(window) as Window;
  Object.defineProperty(securedWindow, 'location', { value: { protocol: 'https:' } });
  vi.stubGlobal('window', securedWindow);
  const fetchRequest = vi.fn()
    .mockResolvedValueOnce(new Response(body, { status }))
    .mockImplementation(async () => new Response(JSON.stringify({ source: 'openrouter' }), { status: 200 }));
  vi.stubGlobal('fetch', fetchRequest);
  const invoke = () => sessionApi.extractRequest('uncertain-http-' + status, 'Учебная заявка', 'csrf', true);
  await expect(invoke()).rejects.toMatchObject({ code });
  await invoke();
  await invoke();
  const keys = fetchRequest.mock.calls.map(([, options]) =>
    (options.headers as Record<string, string>)['Idempotency-Key']);
  expect(keys[1]).toBe(keys[0]);
  expect(keys[2]).not.toBe(keys[1]);
});
