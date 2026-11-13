import { RequestError } from '../api';
import type {
  AutoDraft, AutoDraftLine, Capabilities, Catalog, CatalogProduct,
  ConfirmedInstructions, ExtractedLine, ExtractedRequest, InstructionProposal, QuoteRules,
} from '../domain';

// The public demo is deliberately isolated from /api/, sessions, PostgreSQL and LLM.
// The sole catalog source is the shipped, explicitly synthetic CSV fixture.
const SAMPLE_URL = '/templates/kpdesk-portfolio-synthetic-v1.csv';
const VERSION = 'demo-synthetic-catalog-v1';
const HEADER = 'sku;name;category;sale_unit;pieces_per_sale_unit;price_rub;brand;paper_format;paper_gsm;pen_ink_color;pen_ink_type';
let active = false;
let fixture: Promise<{ raw: string; products: CatalogProduct[] }> | undefined;

function normalizeCsv(text: string): string {
  return text.replace(/^\uFEFF/, '').replace(/\r\n?/g, '\n').trimEnd();
}

function readCsvFile(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new RequestError('demo_file_read_failed', 'Не удалось прочитать CSV. Выберите файл ещё раз.', 422));
    reader.onload = () => typeof reader.result === 'string'
      ? resolve(reader.result)
      : reject(new RequestError('demo_file_read_failed', 'Не удалось прочитать CSV как текст. Скачайте учебный файл заново.', 422));
    reader.readAsText(file, 'utf-8');
  });
}

async function sample() {
  fixture ??= (async () => {
    const response = await fetch(SAMPLE_URL, { cache: 'force-cache' });
    if (!response.ok) throw new RequestError('demo_fixture_unavailable', 'Не удалось загрузить учебный прайс. Попробуйте ещё раз.', 503);
    const raw = normalizeCsv(await response.text());
    const [header, ...rows] = raw.split('\n');
    if (header !== HEADER || rows.length !== 5) {
      throw new RequestError('demo_fixture_invalid', 'Формат учебного прайса изменился. Обновите страницу и попробуйте ещё раз.', 503);
    }
    const products = rows.map(row => {
      const columns = row.split(';');
      if (columns.length !== 11 || !columns[0] || !columns[1]
        || !['paper', 'pen'].includes(columns[2] ?? '')
        || !['пачка', 'штука'].includes(columns[3] ?? '')
        || !/^\d+\.\d{2}$/.test(columns[5] ?? '')) {
        throw new RequestError('demo_fixture_invalid', 'Не удалось прочитать товар в учебном прайсе. Обновите страницу и попробуйте ещё раз.', 503);
      }
      return {
        sku: columns[0]!, name: columns[1]!, category: columns[2]!,
        sale_unit: columns[3] as 'пачка' | 'штука',
        pieces_per_sale_unit: Number(columns[4]),
        price_rub: columns[5]!, brand: columns[6] || null, attributes: {},
      } satisfies CatalogProduct;
    });
    if (new Set(products.map(product => product.sku)).size !== products.length) {
      throw new RequestError('demo_fixture_invalid', 'В учебном прайсе повторяются артикулы.', 503);
    }
    return { raw, products };
  })();
  try { return await fixture; } catch (error) { fixture = undefined; throw error; }
}

function catalogResponse(): Catalog {
  return { id: VERSION, rows_count: 5, source_sha256: 'synthetic-demo-no-sha256' };
}

function noCatalog(): never {
  throw new RequestError('no_active_catalog', 'Сначала активируйте учебный прайс.', 404);
}

const SOURCE_TEXT = 'Нужно 2 пачки бумаги А4 80 г/м², 3 синие шариковые ручки и 1 пачка бумаги А4 120 г/м².';
const DEMO_REQUEST: ExtractedRequest = {
  lines: [
    { source_quote: '2 пачки бумаги А4 80 г/м²', description: 'Бумага офисная А4 80 г/м2 500 листов',
      category: 'paper', quantity: 2, unit: 'пачка', brand: null, paper_format: 'A4',
      paper_gsm: 80, pen_ink_color: null, pen_ink_type: null, attributes: {} },
    { source_quote: '3 синие шариковые ручки', description: 'Ручка шариковая синяя 0.7 мм',
      category: 'pen', quantity: 3, unit: 'штука', brand: null, paper_format: null,
      paper_gsm: null, pen_ink_color: 'синий', pen_ink_type: 'шариковая', attributes: {} },
    { source_quote: '1 пачка бумаги А4 120 г/м²', description: 'Бумага офисная А4 120 г/м2 500 листов',
      category: 'paper', quantity: 1, unit: 'пачка', brand: null, paper_format: 'A4',
      paper_gsm: 120, pen_ink_color: null, pen_ink_type: null, attributes: {} },
  ],
};
export const exampleRequestText = SOURCE_TEXT;

const decimal = (text: string) => Number(text.replace(/\s/g, '').replace(',', '.'));
const canonicalBrand = (value: string) => value.normalize('NFKC').trim()
  .replace(/\s+/g, ' ').toLocaleLowerCase('ru-RU').replace(/ё/g, 'е');
function parseDemoRules(text: string): InstructionProposal {
  const remaining = text.trim();
  // Deliberately narrow demo-only syntax; never treated as production LLM output.
  const markup = /(?:^|[.;]\s*)Наценка\s+(\d{1,3}(?:[.,]\d{1,2})?)\s*%/i.exec(remaining);
  const budget = /(?:^|[.;]\s*)Бюджет\s+до\s+(\d[\d\s]*(?:[.,]\d{1,2})?)\s*(?:рублей|руб\.|₽)?/i.exec(remaining);
  const onlyBrand = /(?:^|[.;]\s*)(?:Только бренд|Выбирай только бренд)\s+([^.;!\n]{1,100})/i.exec(remaining);
  const excludeBrand = /(?:^|[.;]\s*)(?:Исключи бренд|Не используй бренд)\s+([^.;!\n]{1,100})/i.exec(remaining);
  const remainder = remaining.replace(markup?.[0] ?? '\u0000', ' ')
    .replace(budget?.[0] ?? '\u0000', ' ')
    .replace(onlyBrand?.[0] ?? '\u0000', ' ')
    .replace(excludeBrand?.[0] ?? '\u0000', ' ').replace(/[.;\s]+/g, '');
  const requiredBrand = onlyBrand ? canonicalBrand(onlyBrand[1]!) : null;
  const excludedBrand = excludeBrand ? canonicalBrand(excludeBrand[1]!) : null;
  const brandValid = (name: string | null) => name === null ||
    (/^[\p{L}\p{N}_][\p{L}\p{N}_\s&+\-]{0,99}$/u.test(name)
      && !/\b(?:и|или|кроме|но|если)\b/u.test(name));
  const markupValue = markup ? decimal(markup[1]!) : 0;
  const budgetValue = budget ? decimal(budget[1]!) : null;
  const valid = !!(markup || budget || onlyBrand || excludeBrand) && !remainder
    && brandValid(requiredBrand) && brandValid(excludedBrand)
    && !(requiredBrand !== null && requiredBrand === excludedBrand)
    && Number.isFinite(markupValue)
    && markupValue >= 0 && markupValue <= 100 && Number.isInteger(markupValue * 100)
    && (budgetValue === null || (Number.isFinite(budgetValue) && budgetValue > 0
      && Number.isInteger(budgetValue * 100)));
  const rules: QuoteRules = {
    schema_version: 1, markup_percent: String(markupValue),
    budget_max_rub: budgetValue === null ? null : String(budgetValue),
    required_brand: requiredBrand, excluded_brands: excludedBrand ? [excludedBrand] : [],
  };
  return {
    raw_text: text, rules, can_confirm: valid,
    unresolved_requirements: valid ? [] : [remaining || 'Условия не указаны'],
  };
}
function amountKopecks(raw: string): number {
  const [rubles, cents] = raw.split('.');
  return Number(rubles) * 100 + Number(cents);
}
function priceKopecks(line: ExtractedLine, product: CatalogProduct): number | null {
  if (line.category !== product.category || !line.quantity
    || !Number.isSafeInteger(line.quantity) || line.quantity < 1
    || line.unit !== product.sale_unit || product.pieces_per_sale_unit !== 1
    || (line.brand !== null && line.brand.toLowerCase() !== product.brand?.toLowerCase())) return null;
  const actualName = line.description.trim().toLowerCase().replace(/м²/g, 'м2');
  if (actualName !== product.name.trim().toLowerCase().replace(/м²/g, 'м2')) return null;
  if (line.category === 'paper') {
    const format = /а(\d)/i.exec(product.name)?.[0];
    const grammage = /(\d+)\s*г\/м2/.exec(product.name)?.[1];
    if (line.paper_format?.toLowerCase().replace(/^а(?=\d)/, 'a')
      !== format?.toLowerCase().replace(/^а(?=\d)/, 'a')
      || String(line.paper_gsm) !== grammage) return null;
  }
  if (line.category === 'pen' && (line.pen_ink_type !== 'шариковая'
    || (line.pen_ink_color === 'синий' && !product.name.includes('синяя'))
    || (line.pen_ink_color === 'черный' && !product.name.includes('чёрная'))
    || !['синий','черный'].includes(line.pen_ink_color ?? ''))) return null;
  return amountKopecks(product.price_rub) * line.quantity;
}
const money = (cents: number): string => (cents / 100).toFixed(2);

export async function activateSampleCatalog(): Promise<Catalog> {
  await sample();
  active = true;
  return catalogResponse();
}

export function resetDemoCatalog(): void {
  active = false;
}

export const demoApi = {
  capabilities: async (): Promise<Capabilities> => ({
    llm_available: false, model_trained: false, bitrix24_connected: false,
    one_c_connected: false, amocrm_connected: false, google_workspace_connected: false,
  }),
  catalog: async (): Promise<Catalog> => active ? catalogResponse() : noCatalog(),
  items: async (): Promise<{items: CatalogProduct[]}> =>
    ({ items: active ? (await sample()).products : [] }),
  importCatalog: async (file: File, _key: string): Promise<Catalog> => {
    if (file.size > 1024 * 1024 || normalizeCsv(await readCsvFile(file)) !== (await sample()).raw) {
      throw new RequestError('demo_only', 'Выберите учебный CSV со страницы загрузки. Для своего прайса откройте личный кабинет.', 422);
    }
    active = true;
    return catalogResponse();
  },
  extract: async (text: string, _model: string): Promise<ExtractedRequest> => {
    if (text.trim() !== SOURCE_TEXT) {
      throw new RequestError('demo_example_only', 'Готовый разбор доступен для исходного примера. Добавьте изменённые позиции вручную или разберите свою заявку с ИИ в личном кабинете.', 422);
    }
    return structuredClone(DEMO_REQUEST);
  },
  suggestCategory: async (_description: string, _original: string | null): Promise<never> => {
    throw new RequestError('demo_ml_disabled', 'В демо категорию товара нужно выбрать вручную.', 422);
  },
  previewInstructions: async (text: string): Promise<InstructionProposal> => parseDemoRules(text),
  draft: async (catalogId: string, request: ExtractedRequest,
    instructions?: ConfirmedInstructions): Promise<AutoDraft> => {
    if (!active || catalogId !== VERSION) noCatalog();
    if (request.lines.length < 1 || request.lines.length > 20)
      throw new RequestError('invalid_demo_request', 'Добавьте от 1 до 20 позиций для расчёта.', 422);
    const rules = instructions ? parseDemoRules(instructions.raw_text) : null;
    if (instructions && (!rules?.can_confirm
      || JSON.stringify(rules.rules) !== JSON.stringify(instructions.rules))) {
      throw new RequestError('demo_instruction_mismatch', 'Условия изменились. Проверьте и подтвердите их заново.', 422);
    }
    const products = (await sample()).products;
    let base = 0;
    let total = 0;
    let resolved = true;
    const lines: AutoDraftLine[] = request.lines.map((line, index) => {
      const permitted = products.filter(product => {
        const brand = product.brand ? canonicalBrand(product.brand) || null : null;
        const policy = rules?.rules;
        return (!policy?.required_brand || brand === policy.required_brand)
          && (!policy?.excluded_brands?.length || (
            brand !== null && !policy.excluded_brands.includes(brand)
          ));
      });
      const matches = permitted.map(product => ({
        product, cents: priceKopecks(line, product),
      })).filter((match): match is {product: CatalogProduct; cents:number} =>
        match.cents !== null);
      if (matches.length !== 1) {
        resolved = false;
        return {
          line_number: index + 1, status: matches.length ? 'needs_review' : 'no_match',
          sku: null, line_total_rub: null,
          reason_codes: [matches.length ? 'multiple_compatible_skus' :
            !permitted.some(product => product.category === line.category)
              && products.some(product => product.category === line.category)
              ? 'brand_policy_excluded' : 'no_compatible_sku'],
          selection_method: null,
        };
      }
      const chosen = matches[0]!;
      const percent = Number(rules?.rules.markup_percent ?? '0');
      const selling = Math.round(chosen.cents * (100 + percent) / 100);
      base += chosen.cents;
      total += selling;
      return { line_number: index + 1, status: 'auto_selected', sku: chosen.product.sku,
        line_total_rub: money(selling), reason_codes: [],
        selection_method: 'exact_demo_fixture_match' };
    });
    const exceeded = resolved && rules?.rules.budget_max_rub != null
      && total > Number(rules.rules.budget_max_rub) * 100;
    return {
      catalog_version_id: VERSION, approval_status: 'not_approved',
      ready_for_draft: resolved && !exceeded, total_rub: resolved ? money(total) : null,
      base_total_rub: resolved ? money(base) : null, markup_percent: rules?.rules.markup_percent ?? '0',
      budget_max_rub: rules?.rules.budget_max_rub ?? null, budget_exceeded: !!exceeded,
      lines,
    };
  },
};
