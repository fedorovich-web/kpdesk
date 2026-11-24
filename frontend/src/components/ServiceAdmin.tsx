import { useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import { Activity, Building2, ChevronLeft, ChevronRight, CircleDollarSign, CreditCard,
  LayoutDashboard, ReceiptText, Search, Users } from 'lucide-react';
import { designAssets } from '../design-assets';
import { AdminActionError, AdminReadError, serviceAdminApi } from '../service-admin-api';
import type { AdminRows, Amount, ListSection, ModerationAction, Overview, Page } from '../service-admin-api';
import { SessionApiError } from '../tenant-session';
import '../styles/service-admin.css';

const sections = [
  ['overview', 'Обзор', LayoutDashboard],
  ['users', 'Клиенты', Users],
  ['organizations', 'Организации', Building2],
  ['payments', 'Платежи', CreditCard],
  ['reversals', 'Возвраты и споры', ReceiptText],
] as const;
type Section = typeof sections[number][0];

const numbers = new Intl.NumberFormat('ru-RU');
const dateTime = new Intl.DateTimeFormat('ru-RU', { dateStyle: 'medium', timeStyle: 'short', timeZone: 'UTC' });
const date = (value: string | null) => value === null ? '—' : dateTime.format(new Date(value));
const plans: Record<string, string> = { start: 'Start', team: 'Team', business: 'Business' };
const statuses: Record<string, string> = {
  active: 'Активен', pending: 'Ожидает', disabled: 'Отключён', suspended: 'Приостановлена',
  legacy_locked: 'Ограничена', creating: 'Создаётся', ambiguous: 'Требует проверки',
  failed: 'Ошибка', paid: 'Оплачен', refunded: 'Возврат', chargeback: 'Спор по оплате',
};
const moderationCopy = {
  block: { label: 'Заблокировать', confirm: 'Все активные сеансы завершатся, вход будет закрыт.' },
  unblock: { label: 'Разблокировать', confirm: 'Аккаунт вернётся к состоянию до блокировки.' },
  revoke_sessions: { label: 'Завершить сеансы', confirm: 'Пользователю потребуется войти заново.' },
};

function money(amount: string, currency: string) {
  const minor = amount.padStart(3, '0');
  const format = new Intl.NumberFormat('ru-RU', { style: 'currency', currency });
  return format.formatToParts(BigInt(minor.slice(0, -2)))
    .map(part => part.type === 'fraction' ? minor.slice(-2) : part.value).join('');
}

function Amounts({ entries, empty }: { entries: Amount[]; empty: string }) {
  return entries.length ? <ul className="service-admin-amounts">
    {entries.map(entry => <li key={entry.currency}>
      <strong>{money(entry.amount_minor, entry.currency)}</strong>
      <span>Операций: {numbers.format(entry.count)}</span>
    </li>)}
  </ul> : <p className="service-admin-muted">{empty}</p>;
}

function Summary({ data, open }: { data: Overview; open: (section: Section) => void }) {
  const cards = [
    { title: 'Зарегистрированные клиенты', count: data.users_total, detail: `За период: +${numbers.format(data.users_new)}`,
      icon: Users, section: 'users' },
    { title: 'Организации', count: data.organizations_total, detail: `Активных: ${numbers.format(data.organizations_active)}`,
      icon: Building2, section: 'organizations' },
    { title: 'Активные платные периоды', count: data.paid_organizations,
      detail: `Истекают за 7 дней: ${numbers.format(data.expiring_7)}`, icon: CircleDollarSign, section: 'payments' },
    { title: 'Новые КП за период', count: data.quotes_new, detail: `Организаций с прайсом: ${numbers.format(data.organizations_with_catalog)}`,
      icon: Activity, section: null },
  ] as const;
  return <>
    <div className="service-admin-metrics">
      {cards.map(({ title, count, detail, icon: Icon, section }) => <section className="service-admin-card" key={title}>
        <span className="service-admin-icon"><Icon size={19} aria-hidden="true"/></span>
        <h2>{title}</h2>
        <strong className="service-admin-number">{numbers.format(count)}</strong>
        <span className="service-admin-muted">{detail}</span>
        {section && <button type="button" aria-label={`Открыть раздел: ${title}`}
          className="service-admin-card-link" onClick={() => open(section)}><ChevronRight size={18} aria-hidden="true"/></button>}
      </section>)}
    </div>
    <div className="service-admin-details">
      <section className="service-admin-card">
        <h2>Поступления за период</h2>
        <Amounts entries={data.receipts} empty="Подтверждённых оплат за период пока нет."/>
        <h3>Подтверждённые возвраты</h3>
        <Amounts entries={data.refunds} empty="Возвратов за период пока нет."/>
      </section>
      <section className="service-admin-card">
        <h2>Доступ и использование</h2>
        <dl className="service-admin-facts">
          <div><dt>Клиенты с подтверждённой почтой</dt><dd>{numbers.format(data.users_verified)}</dd></div>
          {data.plans.map(plan => <div key={plan.plan_id}>
            <dt>Активный период {plans[plan.plan_id] ?? plan.plan_id}</dt><dd>{numbers.format(plan.count)}</dd>
          </div>)}
          <div><dt>Успешные попытки ИИ за период</dt>
            <dd>{numbers.format(data.ai_success)} из {numbers.format(data.ai_attempts)}</dd></div>
        </dl>
      </section>
      <section className="service-admin-card">
        <h2>Требуют проверки</h2>
        <dl className="service-admin-facts">
          <div><dt>Счета с неопределённым статусом</dt><dd>{numbers.format(data.ambiguous_orders)}</dd></div>
          <div><dt>Возвраты и споры без связи со счётом</dt><dd>{numbers.format(data.unresolved_reversals)}</dd></div>
        </dl>
        <button type="button" className="service-admin-button" onClick={() => open('reversals')}>Смотреть события</button>
      </section>
    </div>
    <p className="service-admin-muted service-admin-timestamp">Данные на {date(data.as_of)} UTC · один активный платный период на организацию</p>
  </>;
}

function tableRows(
  section: ListSection, page: Page<AdminRows[ListSection]>, actions: (user: AdminRows['users']) => ReactNode,
): { headings: string[]; rows: [string, ...ReactNode[]][] } {
  switch (section) {
    case 'users': return {
      headings: ['Почта', 'Статус', 'Регистрация', 'Подтверждение', 'Организации', 'Действия'],
      rows: (page.items as AdminRows['users'][]).map(u => [u.id,
        u.email, u.is_blocked ? 'Заблокирован' : statuses[u.status] ?? u.status, date(u.created_at),
        date(u.email_verified_at), numbers.format(u.organizations_count), actions(u)]),
    };
    case 'organizations': return {
      headings: ['Компания', 'Статус', 'Создана', 'Участники', 'КП', 'Тариф', 'Оплачено до'],
      rows: (page.items as AdminRows['organizations'][]).map(o => [o.id,
        o.name, statuses[o.status] ?? o.status, date(o.created_at), numbers.format(o.members), numbers.format(o.quotes),
        o.plan_id ? plans[o.plan_id] ?? o.plan_id : '—', date(o.paid_through)]),
    };
    case 'payments': return {
      headings: ['Компания', 'Тариф', 'Сумма', 'Статус', 'Создан', 'Подтверждён', 'ID счёта'],
      rows: (page.items as AdminRows['payments'][]).map(p => [p.order_id,
        p.organization_name, plans[p.plan_id] ?? p.plan_id, money(p.amount_minor, p.currency),
        statuses[p.status] ?? p.status, date(p.created_at), date(p.confirmed_at), p.order_id]),
    };
    case 'reversals': return {
      headings: ['Событие', 'Сумма', 'Получено', 'Проверка', 'ID счёта', 'ID события'],
      rows: (page.items as AdminRows['reversals'][]).map(r => [r.event_id,
        r.event_type === 'refund.success' ? 'Возврат' : 'Спор по оплате', money(r.amount_minor, r.currency),
        date(r.received_at), r.resolved ? 'Связано со счётом' : 'Требует проверки', r.order_id ?? '—', r.event_id]),
    };
  }
}

type ReadResult = { key: string; error: number } | { key: string; data: Overview | Page<AdminRows[ListSection]> };

export function ServiceAdmin() {
  const [section, setSection] = useState<Section>('overview');
  const [days, setDays] = useState<Overview['period_days']>(30);
  const [page, setPage] = useState(1);
  const [search, setSearch] = useState('');
  const [query, setQuery] = useState('');
  const [refresh, setRefresh] = useState(0);
  const [hasAccess, setHasAccess] = useState(false);
  const [result, setResult] = useState<ReadResult | null>(null);
  const [moderating, setModerating] = useState(false);
  const [actionNotice, setActionNotice] = useState<{ message: string; error?: boolean } | null>(null);
  const key = JSON.stringify([section, days, query, page, refresh]);
  const current = result?.key === key ? result : null;
  const status = current && 'error' in current ? current.error : null;

  useEffect(() => {
    function restore(event: PageTransitionEvent) {
      if (event.persisted) {
        setHasAccess(false);
        setResult(null);
        setRefresh(value => value + 1);
      }
    }
    window.addEventListener('pageshow', restore);
    return () => window.removeEventListener('pageshow', restore);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    const read = section === 'overview'
      ? serviceAdminApi.overview(days, controller.signal)
      : serviceAdminApi.list(section, page, query, controller.signal);
    void read.then(data => {
      if (controller.signal.aborted) return;
      setHasAccess(true);
      setResult({ key, data });
    }).catch((error: unknown) => {
      if (controller.signal.aborted) return;
      const code = error instanceof AdminReadError ? error.status : 500;
      if (code === 401 || code === 403) setHasAccess(false);
      setResult({ key, error: code });
    });
    return () => controller.abort();
  }, [section, days, query, page, key]);

  function open(next: Section) {
    setSection(next);
    setSearch('');
    setQuery('');
    setPage(1);
    setActionNotice(null);
  }

  async function moderate(user: AdminRows['users'], action: ModerationAction) {
    const { label, confirm } = moderationCopy[action];
    if (moderating || !window.confirm(`${label} аккаунт ${user.email}? ${confirm}`)) return;
    setModerating(true);
    setActionNotice(null);
    try {
      const update = await serviceAdminApi.userAction(user.id, action);
      setResult(previous => {
        if (previous?.key !== key || !('data' in previous) || !('items' in previous.data)) return previous;
        return { ...previous, data: { ...previous.data, items: previous.data.items.map(row =>
          'email' in row && row.id === update.id ? { ...row, status: update.status, is_blocked: update.is_blocked } : row,
        ) } };
      });
      const revoked = `Завершено сеансов: ${numbers.format(update.sessions_revoked)}.`;
      setActionNotice({ message: action === 'unblock' ? 'Аккаунт разблокирован.'
        : action === 'block' ? `Аккаунт заблокирован. ${revoked}` : revoked });
    } catch (error: unknown) {
      if ((error instanceof AdminActionError || error instanceof SessionApiError) && error.status === 401) {
        setHasAccess(false);
        setResult({ key, error: 401 });
      } else if (error instanceof AdminActionError && error.code === 'service_admin_forbidden') {
        setHasAccess(false);
        setResult({ key, error: 403 });
      } else {
        const message = error instanceof AdminActionError && error.status === 409
          ? 'Состояние аккаунта изменилось. Обновите список.'
          : 'Не удалось выполнить действие. Обновите список перед повтором.';
        setActionNotice({ message, error: true });
      }
    } finally {
      setModerating(false);
    }
  }

  function userActions(user: AdminRows['users']) {
    if (user.can_manage === false) return <span className="service-admin-muted">Защищённый аккаунт</span>;
    if (user.can_manage !== true) return '—';
    const actions: ModerationAction[] = [];
    if (user.is_blocked) actions.push('unblock');
    else if (user.status === 'active' || user.status === 'pending') actions.push('block');
    actions.push('revoke_sessions');
    return <div className="service-admin-actions">{actions.map(action => <button type="button" key={action}
      className="service-admin-button" aria-label={`${moderationCopy[action].label} — ${user.email}`}
      disabled={moderating} onClick={() => void moderate(user, action)}>{moderationCopy[action].label}</button>)}</div>;
  }

  const label = sections.find(([id]) => id === section)?.[1];
  const data = current && 'data' in current ? current.data : null;
  const listing = data && 'items' in data ? data : null;
  const table = listing && section !== 'overview' ? tableRows(section, listing, userActions) : null;

  return <div className="service-admin">
    <header className="service-admin-header"><div className="service-admin-container">
      <a href="/" aria-label="KPDesk — главная"><img src={designAssets.logo} alt="KPDesk" width={172} height={44}/></a>
      <a className="service-admin-button" href="/app">Рабочий кабинет <ChevronRight size={16} aria-hidden="true"/></a>
    </div></header>
    <main className="service-admin-container service-admin-main">
      <h1>Кабинет владельца</h1>
      {hasAccess && <nav className="service-admin-nav" aria-label="Разделы кабинета владельца">
        {sections.map(([id, title, Icon]) => <button key={id} type="button" aria-pressed={section === id}
          onClick={() => open(id)}><Icon size={17} aria-hidden="true"/>{title}</button>)}
      </nav>}
      {hasAccess && <div className="service-admin-toolbar">
        <h2>{label}</h2>
        {section === 'overview' ? <label>Период <select value={days} onChange={event => setDays(Number(event.target.value) as Overview['period_days'])}>
          <option value={7}>7 дней</option><option value={30}>30 дней</option><option value={90}>90 дней</option>
        </select></label> : <form onSubmit={event => { event.preventDefault(); setQuery(search.trim()); setPage(1); }}>
          <label><span className="service-admin-visually-hidden">Поиск</span>
            <input type="search" value={search} onChange={event => setSearch(event.target.value)} placeholder="Поиск" maxLength={100}/>
          </label>
          <button type="submit" className="service-admin-button"><Search size={16} aria-hidden="true"/>Найти</button>
        </form>}
      </div>}
      {!current && <p className="service-admin-card" role="status">Загружаем данные…</p>}
      {status === 401 && <div className="service-admin-card"><p>Войдите в KPDesk, чтобы открыть кабинет владельца.</p>
        <a className="service-admin-button service-admin-primary" href="/auth/login?next=%2Fadmin">Войти</a></div>}
      {status === 403 && <p className="service-admin-card" role="alert">Этот кабинет доступен владельцу KPDesk.</p>}
      {status && status !== 401 && status !== 403 && <div className="service-admin-card">
        <p role="alert">Не удалось загрузить данные кабинета.</p>
        <button type="button" className="service-admin-button" onClick={() => setRefresh(value => value + 1)}>Повторить</button>
      </div>}
      {data && 'as_of' in data && <Summary data={data} open={open}/>}
      {listing && table && <section className="service-admin-card" aria-label={label}>
        {section === 'users' && moderating && <p role="status">Выполняем действие…</p>}
        {section === 'users' && actionNotice && <div className="service-admin-action-notice">
          <p role={actionNotice.error ? 'alert' : 'status'}>{actionNotice.message}</p>
          {actionNotice.error && <button type="button" className="service-admin-button" onClick={() => {
            setActionNotice(null);
            setRefresh(value => value + 1);
          }}>Обновить список</button>}
        </div>}
        {table.rows.length ? <div className="service-admin-table-scroll" role="region" aria-label={`Таблица: ${label}`} tabIndex={0}>
          <table><thead><tr>{table.headings.map(heading => <th key={heading} scope="col">{heading}</th>)}</tr></thead>
            <tbody>{table.rows.map(([id, ...cells]) => <tr key={id}>
              {cells.map((cell, index) => index === 0 ? <th key={table.headings[index]} scope="row">{cell}</th>
                : <td key={table.headings[index]}>{cell}</td>)}
            </tr>)}</tbody>
          </table>
        </div> : <p>{page > 1 ? 'На этой странице записей нет.' : 'Записей нет.'}</p>}
        <div className="service-admin-pagination">
          <span>Всего: {numbers.format(listing.total)} · время UTC</span>
          {(listing.total > 25 || page > 1) && <div aria-label="Страницы">
            <button type="button" className="service-admin-button" aria-label="Предыдущая страница" disabled={page === 1}
              onClick={() => setPage(page - 1)}><ChevronLeft size={18} aria-hidden="true"/></button>
            <span>{page}</span>
            <button type="button" className="service-admin-button" aria-label="Следующая страница"
              disabled={page >= Math.min(1000, Math.ceil(listing.total / 25))}
              onClick={() => setPage(page + 1)}><ChevronRight size={18} aria-hidden="true"/></button>
          </div>}
        </div>
      </section>}
    </main>
  </div>;
}
