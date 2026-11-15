import { afterEach, expect, it, vi } from 'vitest';
import { billingApi, isSafePaymentUrl } from './billing-api';

afterEach(() => vi.unstubAllGlobals());

it('binds billing reads and writes to cookie session organization with no cache', async () => {
  const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ order_id: 'one' }) });
  vi.stubGlobal('fetch', fetcher);
  const controller = new AbortController();
  await billingApi.summary('org/one', controller.signal);
  expect(fetcher).toHaveBeenLastCalledWith('/api/session/organizations/org%2Fone/billing', expect.objectContaining({ credentials: 'include', cache: 'no-store', signal: controller.signal }));
  await billingApi.createOrder('org', 'team', 'csrf', 'request-key');
  expect(fetcher).toHaveBeenLastCalledWith('/api/session/organizations/org/billing/orders', expect.objectContaining({ method: 'POST', body: JSON.stringify({ plan_id: 'team' }), headers: expect.objectContaining({ 'X-CSRF-Token': 'csrf', 'Idempotency-Key': 'request-key' }) }));
  await billingApi.reconcileOrder('org', 'one/two', 'csrf');
  expect(fetcher).toHaveBeenLastCalledWith('/api/session/organizations/org/billing/orders/one%2Ftwo/reconcile', expect.objectContaining({ method: 'POST', body: '{}', headers: expect.objectContaining({ 'X-CSRF-Token': 'csrf' }) }));
});

it('does not expose raw provider error text and never retries invoice requests', async () => {
  const fetcher = vi.fn().mockResolvedValue({ ok: false, status: 503, json: async () => ({ detail: 'provider secret payload' }) });
  vi.stubGlobal('fetch', fetcher);
  await expect(billingApi.createOrder('org', 'start', 'csrf', 'key')).rejects.toThrow('Не удалось');
  expect(fetcher).toHaveBeenCalledTimes(1);
});

it.each(['https://lava.top/pay/1', 'https://app.lava.top/pay/1', 'https://pay.lava.top/1'])('allows the exact payment host %s', url => {
  expect(isSafePaymentUrl(url)).toBe(true);
});

it.each(['http://lava.top/1', 'https://lava.top.evil.test/', 'https://lava.top@evil.test/', 'https://user@lava.top/', 'https://lava.top:444/', 'https://lava.top:443/', '//lava.top/1', 'javascript:alert(1)', 'https://LAVA.top/1', ' https://lava.top/1', 'https://lava.top\\@evil.test/'])('rejects an unsafe payment URL %s', url => {
  expect(isSafePaymentUrl(url)).toBe(false);
});
