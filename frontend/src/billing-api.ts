export type BillingPlanId = 'start' | 'team' | 'business';
export type BillingPlan = {
  id: BillingPlanId; name: string; price_minor: number; people_limit: number;
  new_quotes: number; catalog_items: number; days: 30;
};
export type BillingOrder = {
  order_id: string; plan_id: BillingPlanId;
  status: 'prepared' | 'creating' | 'pending' | 'paid' | 'failed' | 'ambiguous' | 'revoked';
  amount_minor: number; currency: 'RUB'; payment_url: string | null; created_at: string;
};
export type BillingSummary = {
  available: boolean; enabled: boolean; plans: BillingPlan[];
  subscription: { plan_id: BillingPlanId; starts_at: string; ends_at: string } | null;
  usage: { new_quotes: number; catalog_items: number; members: number };
  orders: BillingOrder[];
};

export class BillingApiError extends Error {
  constructor(readonly status: number) {
    super(status === 401 ? 'Сессия завершилась. Войдите снова.'
      : status === 403 ? 'Недостаточно прав для оплаты организации.'
      : status === 409 ? 'Операция недоступна: проверьте текущий тариф и ранее созданные счета.'
      : 'Не удалось получить данные об оплате. Обновите данные или повторите проверку позже.');
    this.name = 'BillingApiError';
  }
}

/** Reject even normalized ports/credentials and URL-parser backslash repairs. */
export function isSafePaymentUrl(value: string): boolean {
  if (!/^https:\/\/(lava\.top|app\.lava\.top|pay\.lava\.top)(?:\/|\?|#|$)/.test(value)
    || /[\s\\\u0000-\u001f\u007f]/.test(value)) return false;
  try {
    const parsed = new URL(value);
    return parsed.protocol === 'https:' && !parsed.username && !parsed.password && !parsed.port
      && ['lava.top', 'app.lava.top', 'pay.lava.top'].includes(parsed.hostname);
  } catch { return false; }
}

const path = (organizationId: string) => `/api/session/organizations/${encodeURIComponent(organizationId)}/billing`;
async function request<T>(url: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(url, { ...options, credentials: 'include', cache: 'no-store' });
  if (!response.ok) throw new BillingApiError(response.status);
  return response.json() as Promise<T>;
}
export const billingApi = {
  summary(organizationId: string, signal?: AbortSignal): Promise<BillingSummary> {
    return request(path(organizationId), { signal });
  },
  createOrder(organizationId: string, planId: BillingPlanId, csrf: string, key: string, signal?: AbortSignal): Promise<BillingOrder> {
    return request(`${path(organizationId)}/orders`, {
      method: 'POST', signal,
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf, 'Idempotency-Key': key },
      body: JSON.stringify({ plan_id: planId }),
    });
  },
  reconcileOrder(organizationId: string, orderId: string, csrf: string, signal?: AbortSignal): Promise<BillingOrder> {
    return request(`${path(organizationId)}/orders/${encodeURIComponent(orderId)}/reconcile`, {
      method: 'POST', signal, headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf }, body: '{}',
    });
  },
};
