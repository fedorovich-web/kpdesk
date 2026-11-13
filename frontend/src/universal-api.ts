/** Isolated universal workflow. Decimal values cross this boundary as strings. */
import type { PartyDetails } from './tenant-session';
export type UniversalRequirement = {
  name: string; value: string | number; unit?: string | null; operator: 'eq' | 'min' | 'max'; source_quote: string;
};
export type UniversalRequestLine = {
  description: string; source_quote: string; quantity?: string | null; sale_unit?: string | null;
  sku?: string | null; brand?: string | null; model?: string | null; requirements: UniversalRequirement[];
};
export type UniversalRequest = { lines: UniversalRequestLine[] };
export type UniversalProduct = {
  sku: string; name: string; category?: string | null; brand?: string | null; model?: string | null;
  sale_unit: string; price_rub: string; price_quantity: string; quantity_step?: string | null;
  specifications: Array<{ name: string; value: string | number; unit?: string | null }>;
};
export type UniversalCatalog = { version_id: string; source_sha256: string; products: UniversalProduct[] };
export type UniversalCandidate = { sku: string; name: string; score: number; status: string;
  checks: Array<{ name: string; status: string; reason: string }>; reason_codes: string[] };
export type UniversalMatches = { strategy: 'lexical_universal_v1'; requires_manager_selection: true;
  lines: Array<{ line_number: number; candidates: UniversalCandidate[] }> };
export type UniversalNarrative = { headline: string; opening: string; solution: string; closing: string };
export type UniversalContext = {
  issuer_name: string; recipient_name: string | null; manager_brief: string | null; commercial_terms: string | null;
  issuer_details?: PartyDetails | null; recipient_details?: PartyDetails | null; issuer_contacts?: string | null;
};
export type UniversalQuoteInput = UniversalContext & { catalog_version_id: string; request: UniversalRequest;
  selections: Array<{ line_number: number; sku: string }> };
export type UniversalQuote = UniversalContext & {
  quote_id: string; revision: number; catalog_version_id: string; source_sha256: string;
  source_request: UniversalRequest; selections: Array<{ line_number: number; sku: string }>;
  total_rub: string | null; ready_for_draft: boolean; approved: false; snapshot_sha256: string;
  narrative?: UniversalNarrative | null;
  lines: Array<{ line_number: number; name?: string | null; sku?: string | null; sale_quantity?: string | null;
    sale_unit?: string | null; price_rub?: string | null; price_quantity?: string | null;
    line_total_rub?: string | null; status: string }>;
};
export class UniversalApiError extends Error {
  constructor(readonly status: number, readonly code: string, message: string) { super(message); }
}
export const universalPath = (organizationId: string, suffix: string) =>
  '/api/session/organizations/' + encodeURIComponent(organizationId) + '/universal' + suffix;

export async function universalCall<T>(organizationId: string, suffix: string, options: RequestInit = {}): Promise<T> {
  if (window.location.protocol !== 'https:') {
    throw new UniversalApiError(0, 'https_required', 'Для входа в KPDesk нужно защищённое соединение HTTPS.');
  }
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 90_000);
  try {
    const response = await fetch(universalPath(organizationId, suffix), {
      ...options, credentials: 'include', cache: 'no-store', signal: controller.signal,
    });
    const payload = response.status === 204 ? null : await response.json().catch(() => null) as unknown;
    if (!response.ok) {
      const error = payload as { code?: string; message?: string } | null;
      throw new UniversalApiError(response.status, error?.code ?? 'http_error',
        error?.message ?? 'Запрос завершился ошибкой (HTTP ' + response.status + '). Проверьте результат операции перед повторной попыткой.');
    }
    if (response.status !== 204 && (!payload || typeof payload !== 'object')) {
      throw new UniversalApiError(response.status, 'invalid_response', 'Не удалось прочитать ответ KPDesk. Проверьте результат операции перед повторной попыткой.');
    }
    return payload as T;
  } catch (error) {
    if (error instanceof UniversalApiError) throw error;
    throw new UniversalApiError(0, 'network', error instanceof Error && error.name === 'AbortError'
      ? 'KPDesk не ответил вовремя. Проверьте результат операции перед повторной попыткой.'
      : 'Нет соединения с KPDesk. Проверьте подключение и результат операции перед повторной попыткой.');
  } finally { clearTimeout(timeout); }
}
export function universalPost<T>(org: string, suffix: string, payload: object, csrf: string, key: string) {
  return universalCall<T>(org, suffix, { method: 'POST', body: JSON.stringify(payload), headers: {
    'Content-Type': 'application/json', 'X-CSRF-Token': csrf, 'Idempotency-Key': key,
  } });
}
