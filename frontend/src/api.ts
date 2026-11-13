import type { AutoDraft, Capabilities, Catalog, CatalogProduct, CategorySuggestion, ConfirmedInstructions, ExtractedRequest, InstructionProposal } from './domain';

export class RequestError extends Error {
  constructor(
    readonly code: string,
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = 'RequestError';
  }
}

export async function requestJson<T>(
  path: string,
  init: RequestInit = {},
  timeoutMs = 35_000,
): Promise<T> {
  if (!path.startsWith('/api/') || path.includes('://') || path.includes('..')) {
    throw new Error('Недопустимый адрес API');
  }
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(path, {
      ...init,
      signal: controller.signal,
      credentials: 'omit',
      cache: 'no-store',
      headers: {
        ...(init.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }),
        ...(init.headers ?? {}),
      },
    });
    const raw: unknown = await response.json().catch(() => null);
    if (!response.ok) {
      const body = raw as { code?: unknown; message?: unknown } | null;
      const code = typeof body?.code === 'string' ? body.code : 'http_error';
      const message = typeof body?.message === 'string' && body.message
        ? body.message : 'Запрос завершился ошибкой (HTTP ' + response.status + '). Проверьте результат операции перед повторной попыткой.';
      throw new RequestError(code, message, response.status);
    }
    if (raw === null || typeof raw !== 'object') {
      throw new RequestError('invalid_response', 'Не удалось прочитать ответ KPDesk. Проверьте результат операции перед повторной попыткой.', response.status);
    }
    return raw as T;
  } catch (error) {
    if (error instanceof RequestError) throw error;
    if (error instanceof Error && error.name === 'AbortError') {
      throw new RequestError('timeout', 'KPDesk не ответил вовремя. Проверьте результат операции перед повторной попыткой.', 0);
    }
    throw new RequestError('network', 'Не удалось получить ответ от KPDesk. Проверьте подключение и результат операции перед повторной попыткой.', 0);
  } finally {
    clearTimeout(timeout);
  }
}

export const api = {
  capabilities: () => requestJson<Capabilities>('/api/capabilities'),
  catalog: () => requestJson<Catalog>('/api/catalog/current'),
  items: () => requestJson<{ items: CatalogProduct[] }>('/api/catalog/items'),
  importCatalog: (file: File, key: string) => {
    const form = new FormData();
    form.append('file', file);
    return requestJson<Catalog>('/api/catalog/import', {
      method: 'POST', body: form, headers: { 'Idempotency-Key': key },
    }, 60_000);
  },
  extract: (text: string, model: string) =>
    requestJson<ExtractedRequest>('/api/requests/extract', {
      method: 'POST', body: JSON.stringify({ text, model }),
    }, 60_000),
  suggestCategory: (description: string, original_category: string | null) =>
    requestJson<CategorySuggestion>('/api/matches/category-suggestion', {
      method: 'POST',
      body: JSON.stringify({ description, original_category }),
    }),
  previewInstructions: (text: string) =>
    requestJson<InstructionProposal>('/api/quotes/instructions/preview', {
      method: 'POST', body: JSON.stringify({ text }),
    }),
  draft: (catalogId: string, extracted: ExtractedRequest, instructions?: ConfirmedInstructions) =>
    requestJson<AutoDraft>('/api/quotes/auto-draft', {
      method: 'POST',
      body: JSON.stringify({
        catalog_version_id: catalogId, extracted_request: extracted,
        ...(instructions ? { instructions } : {}),
      }),
    }, 45_000),
};
