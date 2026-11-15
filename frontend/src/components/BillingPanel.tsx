import { useEffect, useRef, useState } from 'react';
import { BillingApiError, billingApi, isSafePaymentUrl } from '../billing-api';
import type { BillingOrder, BillingPlanId, BillingSummary } from '../billing-api';

type Props = {
  organization: { id: string; name: string; role: 'owner' | 'editor' | 'viewer' }; csrf: string;
};
type Attempt = { planId: BillingPlanId; key: string; uncertain: boolean; orderId?: string };
// Keep invoice request bindings across navigation remounts for this page lifetime.
// These are random request identifiers, never session tokens or provider data.
const invoiceAttempts = new Map<string, Attempt>();
const unresolved = (order: BillingOrder) => ['prepared', 'creating', 'pending', 'ambiguous'].includes(order.status);
const statusText: Record<BillingOrder['status'], string> = {
  prepared: 'Счёт подготовлен', creating: 'Счёт создаётся', pending: 'Ожидает оплаты',
  paid: 'Оплачено', failed: 'Оплата не выполнена', ambiguous: 'Требуется проверка оплаты', revoked: 'Оплата отменена или возвращена',
};
const money = (minor: number) => `${new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 2 }).format(minor / 100)} ₽`;
const date = (value: string) => {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? 'Дата недоступна' : parsed.toLocaleDateString('ru-RU');
};
const message = (error: unknown) => error instanceof BillingApiError ? error.message : 'Не удалось обновить данные об оплате. Повторите проверку позже.';

/** A keyed scope removes old organization content immediately, including during render. */
export function BillingPanel(props: Props) {
  return <BillingScope key={`${props.organization.id}:${props.organization.role}:${props.csrf}`}
    {...props} attempts={invoiceAttempts} />;
}

function BillingScope({ organization, csrf, attempts }: Props & { attempts: Map<string, Attempt> }) {
  const [summary, setSummary] = useState<BillingSummary | null>(null);
  const [consent, setConsent] = useState(false);
  const [pending, setPending] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [uncertain, setUncertain] = useState(attempts.get(organization.id)?.uncertain ?? false);
  const controller = useRef<AbortController | null>(null);
  const sequence = useRef(0);
  const busy = useRef(false);
  const owner = organization.role === 'owner';

  const refresh = async () => {
    const active = controller.current;
    if (!active || active.signal.aborted) return;
    const epoch = ++sequence.current;
    setLoading(true);
    try {
      const result = await billingApi.summary(organization.id, active.signal);
      if (!active.signal.aborted && epoch === sequence.current) {
        setSummary(result);
        setError('');
        const attempt = attempts.get(organization.id);
        if (attempt?.orderId && result.orders.some(order => order.order_id === attempt.orderId && !unresolved(order))) {
          attempts.delete(organization.id);
          setUncertain(false);
        }
      }
    } catch (err) {
      if (!active.signal.aborted && epoch === sequence.current) setError(message(err));
    } finally {
      if (!active.signal.aborted && epoch === sequence.current) setLoading(false);
    }
  };

  useEffect(() => {
    const active = new AbortController();
    controller.current = active;
    void refresh();
    return () => { active.abort(); sequence.current++; };
    // The keyed component owns exactly one organization/session/role scope.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const create = async (planId: BillingPlanId) => {
    const active = controller.current;
    if (!active || active.signal.aborted || busy.current || !owner || !consent || !csrf
      || !summary?.enabled || !summary.available || uncertain || summary.orders.some(unresolved)) return;
    busy.current = true;
    setPending(true); setError('');
    const prior = attempts.get(organization.id);
    const attempt = prior?.planId === planId ? prior : { planId, key: crypto.randomUUID(), uncertain: false };
    // Persist uncertainty before awaiting: a scope change can abort an accepted POST.
    attempt.uncertain = true;
    attempts.set(organization.id, attempt);
    try {
      const order = await billingApi.createOrder(organization.id, planId, csrf, attempt.key, active.signal);
      if (active.signal.aborted) return;
      if (!order?.order_id || !Object.hasOwn(statusText, order.status)) throw new Error('Invalid order response');
      attempt.uncertain = false;
      attempt.orderId = order.order_id;
      setSummary(current => current ? { ...current, orders: [order, ...current.orders.filter(item => item.order_id !== order.order_id)] } : current);
      if (!unresolved(order)) attempts.delete(organization.id);
    } catch (err) {
      if (active.signal.aborted) return;
      if (err instanceof BillingApiError && err.status >= 400 && err.status < 500 && err.status !== 408) {
        attempts.delete(organization.id);
        setError(message(err));
      } else {
        setUncertain(true);
        setError('Результат создания счёта неизвестен. Обновите данные об оплате и проверьте существующий счёт. Новый счёт не создаётся автоматически.');
      }
    } finally {
      busy.current = false;
      if (!active.signal.aborted) setPending(false);
    }
  };

  const reconcile = async (order: BillingOrder) => {
    const active = controller.current;
    if (!active || active.signal.aborted || busy.current || !owner || !csrf) return;
    busy.current = true; setPending(true); setError('');
    try {
      const result = await billingApi.reconcileOrder(organization.id, order.order_id, csrf, active.signal);
      if (active.signal.aborted) return;
      setSummary(current => current ? { ...current, orders: current.orders.map(item => item.order_id === result.order_id ? result : item) } : current);
      if (!unresolved(result)) { attempts.delete(organization.id); setUncertain(false); }
      await refresh();
    } catch (err) {
      if (!active.signal.aborted) setError(message(err));
    } finally {
      busy.current = false;
      if (!active.signal.aborted) setPending(false);
    }
  };

  const connected = summary?.enabled && summary.available;
  const subscription = summary?.subscription;
  const activeSubscription = subscription && new Date(subscription.ends_at).getTime() > Date.now();
  const currentPlan = summary?.plans.find(plan => plan.id === subscription?.plan_id);
  const blocked = pending || loading || uncertain || !!summary?.orders.some(unresolved);

  return <section className="access-catalog-state billing-panel" aria-label="Тариф и оплата">
    <div className="billing-panel-heading"><div><h2>Тариф и оплата</h2><p>{organization.name}</p></div>
      <button className="button button-white" type="button" disabled={loading || pending} onClick={() => void refresh()}>Обновить данные об оплате</button></div>
    {loading && <p role="status">Загружаем данные об оплате…</p>}
    {error && <p role="alert">{error}</p>}
    {summary && <>
      {!connected && <p role="status">Оплата пока не подключена</p>}
      {subscription ? <p>{activeSubscription ? 'Доступ оплачен' : 'Срок доступа истёк'}: {currentPlan?.name ?? subscription.plan_id}. До {date(subscription.ends_at)}.</p>
        : <p>Оплаченного периода пока нет.</p>}
      <p>Существующие КП и скачивание PDF остаются доступны после окончания оплаченного периода.</p>
      <dl className="billing-usage" aria-label="Использование организации">
        <div><dt>Новые КП: {summary.usage.new_quotes}{currentPlan ? ` / ${currentPlan.new_quotes}` : ''}</dt><dd>За оплаченный период</dd></div>
        <div><dt>Товары каталога: {summary.usage.catalog_items}{currentPlan ? ` / ${currentPlan.catalog_items}` : ''}</dt><dd>В организации</dd></div>
        <div><dt>Участники: {summary.usage.members}{currentPlan ? ` / ${currentPlan.people_limit}` : ''}</dt><dd>В организации</dd></div>
      </dl>
      <ul className="billing-plans" aria-label="Тарифы организации">{summary.plans.map(plan => <li key={plan.id}>
        <h3>{plan.name}</h3><p className="billing-price">{money(plan.price_minor)}</p><p>За {plan.days} дней доступа</p>
        <p>Участники: {plan.people_limit}<br />Новые КП: {plan.new_quotes}<br />Товары каталога: {plan.catalog_items}</p>
        {owner && connected && <button className="button button-primary" type="button"
          disabled={blocked || !consent || !csrf || (!!activeSubscription && subscription.plan_id !== plan.id)}
          onClick={() => void create(plan.id)}>{activeSubscription && subscription.plan_id === plan.id ? 'Продлить' : 'Оплатить'} {plan.name}</button>}
      </li>)}</ul>
      {connected && owner && <label className="billing-consent"><input type="checkbox" checked={consent} disabled={pending}
        onChange={event => setConsent(event.target.checked)} />Разовая оплата доступа на 30 дней без автосписаний</label>}
      {connected && !owner && <p>Оплату выполняет владелец организации.</p>}
      {activeSubscription && connected && <p>Продление текущего тарифа добавляет 30 дней. Смена тарифа доступна после окончания оплаченного периода; лимит текущего периода при продлении не сбрасывается.</p>}
      {!!summary.orders.length && <div className="billing-orders"><h3>История оплаты</h3><ul>{summary.orders.map(order => <li key={order.order_id}>
        <div><strong>{summary.plans.find(plan => plan.id === order.plan_id)?.name ?? order.plan_id} · {money(order.amount_minor)}</strong>
          <p>{date(order.created_at)} · <span>{statusText[order.status]}</span></p></div>
        {owner && connected && unresolved(order) && <div className="billing-order-actions">
          {order.payment_url && isSafePaymentUrl(order.payment_url) && order.status === 'pending'
            && <a className="button button-primary" href={order.payment_url} target="_blank" rel="noopener noreferrer">Перейти к оплате</a>}
          {order.payment_url && !isSafePaymentUrl(order.payment_url) && <p>Ссылка оплаты недоступна. Проверьте статус счёта.</p>}
          <button className="button button-white" type="button" disabled={pending || loading || !csrf} onClick={() => void reconcile(order)}>Проверить оплату</button>
        </div>}
      </li>)}</ul></div>}
    </>}
  </section>;
}
