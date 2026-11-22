export type Amount = { currency: string; count: number; amount_minor: string };
export type Overview = {
  as_of: string;
  period_days: 7 | 30 | 90;
  users_total: number;
  users_new: number;
  users_verified: number;
  organizations_total: number;
  organizations_active: number;
  paid_organizations: number;
  expiring_7: number;
  plans: { plan_id: string; count: number }[];
  receipts: Amount[];
  refunds: Amount[];
  unresolved_reversals: number;
  ambiguous_orders: number;
  quotes_new: number;
  organizations_with_catalog: number;
  ai_attempts: number;
  ai_success: number;
};

export type AdminRows = {
  users: {
    id: string; email: string; status: string; created_at: string;
    email_verified_at: string | null; organizations_count: number;
  };
  organizations: {
    id: string; name: string; status: string; created_at: string;
    members: number; quotes: number; plan_id: string | null; paid_through: string | null;
  };
  payments: {
    order_id: string; organization_name: string; plan_id: string; status: string;
    amount_minor: string; currency: string; created_at: string; confirmed_at: string | null;
  };
  reversals: {
    event_id: string; event_type: string; amount_minor: string; currency: string;
    received_at: string; resolved: boolean; order_id: string | null;
  };
};
export type ListSection = keyof AdminRows;
export type Page<T> = { total: number; items: T[] };

export class AdminReadError extends Error {
  constructor(readonly status: number) {
    super('Не удалось загрузить данные кабинета.');
  }
}

async function read<T>(path: string, signal: AbortSignal): Promise<T> {
  const response = await fetch('/api/admin/' + path, {
    credentials: 'same-origin', headers: { Accept: 'application/json' }, cache: 'no-store', signal,
  });
  if (!response.ok) throw new AdminReadError(response.status);
  return response.json() as Promise<T>;
}

export const serviceAdminApi = {
  capability: (signal: AbortSignal) => read<{ allowed: boolean }>('capability', signal),
  overview: (days: Overview['period_days'], signal: AbortSignal) => read<Overview>('overview?days=' + days, signal),
  list: <Section extends ListSection>(section: Section, page: number, query: string, signal: AbortSignal) => {
    const params = new URLSearchParams({ page: String(page), q: query });
    return read<Page<AdminRows[Section]>>(section + '?' + params, signal);
  },
};
