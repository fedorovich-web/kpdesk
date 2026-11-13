import type { AutoDraft, ExtractedRequest } from '../domain';

const purposes: Record<string, string> = {
  'PAPER-A4-80': 'Бумага А4 80 г/м² — для офисных печатных документов.',
  'PAPER-A4-65': 'Бумага А4 65 г/м² — для печатных документов.',
  'PAPER-A3-80': 'Бумага А3 80 г/м² — для документов формата А3.',
  'PEN-BLUE-BALL': 'Синие шариковые ручки — для рукописных записей.',
  'PEN-BLACK-BALL': 'Чёрные шариковые ручки — для рукописных записей.',
};
const unitForms: Record<string, [string, string, string]> = {
  'пачка': ['пачка', 'пачки', 'пачек'],
  'штука': ['штука', 'штуки', 'штук'],
};
const plural = new Intl.PluralRules('ru');
function quantityText(quantity: number, unit: string): string {
  const form = plural.select(quantity);
  const forms = unitForms[unit];
  return `${quantity} ${forms ? forms[form === 'one' ? 0 : form === 'few' ? 1 : 2] : unit}`;
}

/** Authored browser example: no model call, no company claims or inferred suitability. */
export function demoProposalExample(draft: AutoDraft, request: ExtractedRequest): {
  composition: string; purpose: string; nextStep: string;
} | null {
  if (draft.catalog_version_id !== 'demo-synthetic-catalog-v1' || !draft.ready_for_draft
    || !draft.lines.length || draft.lines.some(line => line.status !== 'auto_selected'
      || !line.sku || !purposes[line.sku]
      || line.selection_method !== 'exact_demo_fixture_match')) return null;
  const selected = draft.lines.map(line => ({ line, source: request.lines[line.line_number - 1] }));
  if (selected.some(({ source }) => !source?.description || !source.quantity || !source.unit)) return null;
  return {
    composition: 'В предложение включены: ' + selected.map(({ source }) =>
      `${source!.description} — ${quantityText(source!.quantity!, source!.unit!)}`).join('; ') + '.',
    purpose: [...new Set(selected.map(({ line }) => purposes[line.sku!]!))].join(' ')
      + ' Проверьте, подходит ли выбранный состав для задачи заказчика.',
    nextStep: 'Согласуйте с заказчиком товары и количество. Наличие, оплату и поставку уточните отдельно: в учебном прайсе этих условий нет.',
  };
}
