import { categories } from './categories';

export type QuoteStatus = 'auto_selected' | 'needs_review' | 'no_match';
export type SaleUnit = 'штука' | 'пачка' | 'коробка';
export type AttributeValue = string | number | string[] | null;

export interface ExtractedLine {
  source_quote: string;
  description: string;
  category: string | null;
  quantity: number | null;
  unit: SaleUnit | null;
  brand: string | null;
  paper_format: string | null;
  paper_gsm: number | null;
  pen_ink_color: string | null;
  pen_ink_type: string | null;
  attributes: Record<string, AttributeValue>;
}

export interface ExtractedRequest { lines: ExtractedLine[] }
export interface CategorySuggestion {
  original_category: string | null;
  suggested_category: string | null;
  best_category: string;
  confidence: number;
  margin: number;
  abstained: boolean;
  requires_explicit_acceptance: boolean;
}
export interface Catalog { id: string; source_sha256: string; rows_count: number }

export interface CatalogProduct {
  sku: string;
  name: string;
  category: string;
  price_rub: string;
  sale_unit: SaleUnit;
  pieces_per_sale_unit: number;
  brand: string | null;
  attributes: Record<string, AttributeValue>;
}

export interface Capabilities {
  llm_available: boolean;
  model_trained: boolean;
  bitrix24_connected: boolean;
  one_c_connected: boolean;
  amocrm_connected: boolean;
  google_workspace_connected: boolean;
}

export interface AutoDraftLine {
  line_number: number;
  status: QuoteStatus;
  sku: string | null;
  line_total_rub: string | null;
  reason_codes: string[];
  selection_method: string | null;
}
export interface QuoteRules {
  schema_version: 1;
  markup_percent: string;
  budget_max_rub: string | null;
  required_brand?: string | null;
  excluded_brands?: string[];
}
export interface InstructionProposal {
  raw_text: string;
  rules: QuoteRules;
  unresolved_requirements: string[];
  can_confirm: boolean;
}
export interface ConfirmedInstructions {
  raw_text: string;
  rules: QuoteRules;
}
export interface AutoDraft {
  catalog_version_id: string;
  ready_for_draft: boolean;
  total_rub: string | null;
  base_total_rub?: string | null;
  markup_percent?: string;
  budget_max_rub?: string | null;
  budget_exceeded?: boolean;
  approval_status: 'not_approved';
  lines: AutoDraftLine[];
}

export const MODELS = [
  { value: 'nvidia/nemotron-3-ultra-550b-a55b:free', label: 'Nemotron 3 Ultra (OpenRouter Free)' },
] as const;

export const REASONS: Record<string, string> = {
  category_needs_review: 'Категория товара не определена',
  no_compatible_sku: 'Не удалось подобрать товар для этой позиции. Проверьте требования и данные прайса.',
  category_description_mismatch: 'Описание товара противоречит выбранной категории. Проверьте категорию заявки.',
  multiple_compatible_skus: 'Есть несколько возможных вариантов',
  insufficient_lexical_evidence: 'Похожего названия недостаточно — проверьте характеристики товара',
  unverified_product_identity: 'Нужно подтвердить точное соответствие товара: похожего названия недостаточно',
  explicit_identifier_mismatch: 'Артикул, модель или обозначение упаковки не совпадают — проверьте товар',
  missing_quantity_or_unit: 'Уточните количество или единицу измерения',
  attributes_not_verified: 'Не подтверждены обязательные характеристики',
  fractional_package: 'Требуется подтвердить количество упаковок',
  unknown_package_conversion: 'Не удалось сопоставить единицы заявки и прайса. Проверьте фасовку и количество в упаковке.',
  inconsistent_piece_package: 'В прайсе не совпадают единица продажи и количество штук. Проверьте упаковку товара',
  invalid_markup_percent: 'Проверьте наценку: одно число от 0 до 100, например 18,5',
  invalid_budget_max_rub: 'Проверьте бюджет: положительное число, группы тысяч по три цифры, например 1 234,50',
  generic_category_requires_review: 'Требуется уточнить вид товара',
  brand_policy_excluded: 'Не удалось подобрать товар с учётом ограничений по брендам. Проверьте прайс и условия подбора.',
  unresolved_attributes: 'Есть неподтверждённые характеристики',
  conflicting_markup_percent: 'Укажите одну наценку для этого КП',
  conflicting_budget_max_rub: 'Укажите один предельный бюджет для этого КП',
  conflicting_required_brand: 'Укажите один обязательный бренд',
  invalid_required_brand: 'Проверьте название обязательного бренда',
  invalid_excluded_brand: 'Проверьте название исключённого бренда',
  too_many_excluded_brands: 'Можно исключить не более 10 брендов',
  duplicate_excluded_brand: 'Один бренд указан в списке исключений несколько раз',
  conflicting_brand_policy: 'Один и тот же бренд нельзя одновременно требовать и исключать',
  explicit_package_overage_confirmed: 'Количество целых упаковок подтверждено менеджером',
  supplier_selection_required: 'Выберите поставщика и проверьте выбранные товары',
  supplier_explicitly_selected: 'Поставщик выбран менеджером',
  category_mismatch: 'Категория товара не соответствует заявке',
  printer_model_incompatible: 'По данным прайса картридж не подходит к указанной модели принтера',
  cartridge_identifier_missing: 'Укажите модель принтера или обозначение картриджа',
};

const attributeLabels: Record<string, string> = Object.fromEntries([
  ...categories.flatMap(category => category.fields.map(field => [field.key, field.label])),
  ['brand', 'Бренд'], ['paper_format', 'Формат бумаги'], ['paper_gsm', 'Плотность бумаги'],
  ['pen_ink_color', 'Цвет чернил'], ['pen_ink_type', 'Тип чернил'],
]);

export function readableReason(code: string): string {
  if (Object.hasOwn(REASONS, code)) return REASONS[code]!;
  for (const suffix of ['_request_unknown', '_unknown', '_mismatch']) {
    if (!code.endsWith(suffix)) continue;
    const key = code.slice(0, -suffix.length);
    if (!Object.hasOwn(attributeLabels, key)) break;
    const label = attributeLabels[key];
    if (suffix === '_request_unknown') return `В заявке не уточнена характеристика «${label}».`;
    if (suffix === '_unknown') return `Не подтверждена характеристика «${label}» у товара.`;
    return `Характеристика «${label}» не соответствует требованиям заявки.`;
  }
  return 'Требуется проверка соответствия';
}

export function money(value: string | null | undefined): string {
  if (value == null) return '—';
  const amount = Number(value);
  return Number.isFinite(amount)
    ? new Intl.NumberFormat('ru-RU', { style: 'currency', currency: 'RUB' }).format(amount)
    : '—';
}
