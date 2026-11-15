import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { billingApi } from '../billing-api';
import type { BillingOrder, BillingSummary } from '../billing-api';
import { BillingPanel } from './BillingPanel';

const organization = { id: 'org-one', name: 'Первая', role: 'owner' as const };
let organizationNumber = 0;
const order: BillingOrder = { order_id: 'one', plan_id: 'start', status: 'pending', amount_minor: 29900, currency: 'RUB', payment_url: 'https://pay.lava.top/pay/one', created_at: '2026-11-14T00:00:00Z' };
const summary: BillingSummary = {
  available: true, enabled: true,
  plans: [{ id: 'start', name: 'Старт', price_minor: 29900, people_limit: 1, new_quotes: 30, catalog_items: 500, days: 30 }, { id: 'team', name: 'Команда', price_minor: 49900, people_limit: 5, new_quotes: 100, catalog_items: 2000, days: 30 }, { id: 'business', name: 'Бизнес', price_minor: 99000, people_limit: 15, new_quotes: 300, catalog_items: 5000, days: 30 }],
  subscription: null, usage: { new_quotes: 2, catalog_items: 15, members: 1 }, orders: [],
};
beforeEach(() => {
  organization.id = `org-${++organizationNumber}`;
  vi.spyOn(billingApi, 'summary').mockResolvedValue(structuredClone(summary));
  vi.spyOn(billingApi, 'createOrder').mockResolvedValue({ ...order });
  vi.spyOn(billingApi, 'reconcileOrder').mockResolvedValue({ ...order, status: 'paid', payment_url: null });
});
afterEach(() => { vi.restoreAllMocks(); window.history.replaceState({}, '', '/'); });
const mount = () => render(<BillingPanel organization={organization} csrf="csrf" />);
const confirm = async () => fireEvent.click(await screen.findByRole('checkbox', { name: 'Разовая оплата доступа на 30 дней без автосписаний' }));

it('shows server prices and usage, requires consent, opens payment only via explicit link', async () => {
  mount();
  const start = await screen.findByRole('button', { name: 'Оплатить Старт' });
  expect(start).toBeDisabled();
  expect(screen.getByText('299 ₽')).toBeInTheDocument();
  expect(screen.getByText('499 ₽')).toBeInTheDocument();
  expect(screen.getByText('990 ₽')).toBeInTheDocument();
  expect(screen.getByText(/Новые КП: 2/)).toBeInTheDocument();
  await confirm(); fireEvent.click(start);
  const link = await screen.findByRole('link', { name: 'Перейти к оплате' });
  expect(link).toHaveAttribute('href', order.payment_url);
  expect(link).toHaveAttribute('rel', 'noopener noreferrer');
  expect(billingApi.createOrder).toHaveBeenCalledWith(organization.id, 'start', 'csrf', expect.stringMatching(/^[a-f\d-]{36}$/), expect.any(AbortSignal));
  expect(billingApi.createOrder).toHaveBeenCalledTimes(1);
  expect(window.location.pathname).toBe('/');
});

it.each(['editor', 'viewer'] as const)('allows %s to read but not create or reconcile', async role => {
  vi.mocked(billingApi.summary).mockResolvedValue({ ...summary, orders: [order] });
  render(<BillingPanel organization={{ ...organization, role }} csrf="csrf" />);
  await screen.findByText('Ожидает оплаты');
  expect(screen.queryByRole('checkbox')).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: /^Оплатить/ })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: /Проверить оплату/ })).not.toBeInTheDocument();
  expect(screen.queryByRole('link', { name: 'Перейти к оплате' })).not.toBeInTheDocument();
});

it.each([{ enabled: false, available: true }, { enabled: true, available: false }])('shows truthful unavailable billing without checkout %j', async status => {
  vi.mocked(billingApi.summary).mockResolvedValue({ ...summary, ...status });
  mount(); await screen.findByText('Оплата пока не подключена');
  expect(screen.queryByRole('checkbox')).not.toBeInTheDocument();
});

it('never trusts the return query and changes paid state only after manual server readback', async () => {
  window.history.replaceState({}, '', '/?billing_return=paid&billing_order=one');
  vi.mocked(billingApi.summary).mockResolvedValueOnce({ ...summary, orders: [{ ...order, status: 'ambiguous' }] })
    .mockResolvedValue({ ...summary, orders: [{ ...order, status: 'paid', payment_url: null }] });
  mount(); await screen.findByText('Требуется проверка оплаты');
  expect(screen.queryByText('Оплачено')).not.toBeInTheDocument();
  expect(billingApi.reconcileOrder).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'Проверить оплату' }));
  await screen.findByText('Оплачено');
  expect(billingApi.reconcileOrder).toHaveBeenCalledTimes(1);
  expect(billingApi.createOrder).not.toHaveBeenCalled();
});

it('blocks repeat creation after an uncertain POST, even after summary refresh', async () => {
  vi.mocked(billingApi.createOrder).mockRejectedValue(new TypeError('Network lost'));
  mount(); await confirm(); fireEvent.click(screen.getByRole('button', { name: 'Оплатить Старт' }));
  expect(await screen.findByRole('alert')).toHaveTextContent(/неизвестен/i);
  expect(screen.getByRole('button', { name: 'Оплатить Старт' })).toBeDisabled();
  fireEvent.click(screen.getByRole('button', { name: 'Обновить данные об оплате' }));
  await act(async () => {});
  expect(screen.getByRole('button', { name: 'Оплатить Старт' })).toBeDisabled();
  expect(billingApi.createOrder).toHaveBeenCalledTimes(1);
});

it('retains an uncertain invoice request through navigation remounts', async () => {
  vi.mocked(billingApi.createOrder).mockRejectedValue(new TypeError('Network lost'));
  const view = mount(); await confirm(); fireEvent.click(screen.getByRole('button', { name: 'Оплатить Старт' }));
  await screen.findByRole('alert'); view.unmount();
  mount(); await confirm();
  expect(screen.getByRole('button', { name: 'Оплатить Старт' })).toBeDisabled();
  expect(billingApi.createOrder).toHaveBeenCalledTimes(1);
});

it('renews with a fresh request key after the previous invoice is settled', async () => {
  mount(); await confirm(); fireEvent.click(screen.getByRole('button', { name: 'Оплатить Старт' }));
  await screen.findByRole('link', { name: 'Перейти к оплате' });
  const key = vi.mocked(billingApi.createOrder).mock.calls[0]?.[3];
  vi.mocked(billingApi.summary).mockResolvedValue({ ...summary, orders: [{ ...order, status: 'paid', payment_url: null }] });
  fireEvent.click(screen.getByRole('button', { name: 'Обновить данные об оплате' }));
  await screen.findByText('Оплачено');
  fireEvent.click(screen.getByRole('button', { name: 'Оплатить Старт' }));
  await screen.findByRole('link', { name: 'Перейти к оплате' });
  expect(billingApi.createOrder).toHaveBeenCalledTimes(2);
  expect(vi.mocked(billingApi.createOrder).mock.calls[1]?.[3]).not.toBe(key);
});

it('discards a late initial summary after organization change', async () => {
  let resolve!: (value: BillingSummary) => void;
  vi.mocked(billingApi.summary).mockImplementationOnce(() => new Promise(done => { resolve = done; }));
  const view = mount();
  const signal = vi.mocked(billingApi.summary).mock.calls[0]?.[1];
  vi.mocked(billingApi.summary).mockResolvedValue({ ...summary, enabled: false });
  view.rerender(<BillingPanel organization={{ ...organization, id: 'other-org', name: 'Вторая' }} csrf="csrf" />);
  await screen.findByText('Оплата пока не подключена');
  await act(async () => resolve({ ...summary, orders: [order] }));
  expect(signal?.aborted).toBe(true);
  expect(screen.queryByRole('link', { name: 'Перейти к оплате' })).not.toBeInTheDocument();
  expect(screen.getByText('Оплата пока не подключена')).toBeInTheDocument();
});

it('ignores a late invoice from another organization and aborts the previous request', async () => {
  let resolve!: (value: BillingOrder) => void;
  vi.mocked(billingApi.createOrder).mockImplementation(() => new Promise(done => { resolve = done; }));
  const view = mount(); await confirm(); fireEvent.click(screen.getByRole('button', { name: 'Оплатить Старт' }));
  const signal = vi.mocked(billingApi.createOrder).mock.calls[0]?.[4];
  view.rerender(<BillingPanel organization={{ ...organization, id: 'org-two', name: 'Вторая' }} csrf="other" />);
  await act(async () => resolve(order));
  expect(signal?.aborted).toBe(true);
  expect(screen.queryByRole('link', { name: 'Перейти к оплате' })).not.toBeInTheDocument();
  expect(screen.getByRole('region', { name: 'Тариф и оплата' })).toHaveTextContent('Вторая');
});

it('does not expose an unsafe returned payment URL', async () => {
  vi.mocked(billingApi.summary).mockResolvedValue({ ...summary, orders: [{ ...order, payment_url: 'https://evil.test/pay' }] });
  mount(); await screen.findByText('Ожидает оплаты');
  expect(screen.queryByRole('link', { name: 'Перейти к оплате' })).not.toBeInTheDocument();
  expect(await screen.findByText(/Ссылка оплаты недоступна/)).toBeInTheDocument();
});

it('keeps expired documents readable and blocks switching an active plan', async () => {
  vi.mocked(billingApi.summary).mockResolvedValue({ ...summary, subscription: { plan_id: 'start', starts_at: '2026-01-01T00:00:00Z', ends_at: '2099-12-31T00:00:00Z' } });
  mount(); await confirm();
  expect(screen.getByRole('button', { name: 'Продлить Старт' })).toBeEnabled();
  expect(screen.getByRole('button', { name: 'Оплатить Команда' })).toBeDisabled();
  expect(screen.getByText(/Существующие КП и скачивание PDF остаются доступны/)).toBeInTheDocument();
  const plans = screen.getByRole('list', { name: 'Тарифы организации' });
  expect(within(plans).getAllByRole('listitem')).toHaveLength(3);
});
