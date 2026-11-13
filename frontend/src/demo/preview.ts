import type { AutoDraft, ExtractedRequest, ExtractedLine } from '../domain';

/** Visual reference fixture transcribed from approved PNG #15.
 * These electrical goods are NOT from KPDesk's working catalog and must never be posted to its API.
 * Demo totals exclude the mockup's unsupported VAT calculation.
 */
const descriptions = [
  { name: 'Кабель ВВГнг-LS 5×6', sku: '123456', quantity: 100, total: '21500.00' },
  { name: 'Автомат ABB S203 C16', sku: '654321', quantity: 20, total: '25000.00' },
  { name: 'Розетка Schneider Electric Blanca', sku: '789012', quantity: 50, total: '16000.00' },
  { name: 'Светильник LED 36W', sku: '345678', quantity: 20, total: '28400.00' },
  { name: 'Лоток металлический 100×50', sku: '901234', quantity: 30, total: '17400.00' },
] as const;

const lines: ExtractedLine[] = descriptions.map(item => ({
  source_quote: item.name,
  description: item.name,
  category: null,
  quantity: item.quantity,
  unit: null,
  brand: null,
  paper_format: null,
  paper_gsm: null,
  pen_ink_color: null,
  pen_ink_type: null,
  attributes: {},
}));

export const previewDemo: { draft: AutoDraft; extracted: ExtractedRequest } = {
  extracted: { lines },
  draft: {
    catalog_version_id: 'Демопример из макета №15 — без серверного каталога',
    approval_status: 'not_approved',
    ready_for_draft: true,
    total_rub: '108300.00',
    lines: descriptions.map((item, i) => ({
      line_number: i + 1,
      sku: item.sku,
      status: 'auto_selected',
      line_total_rub: item.total,
      reason_codes: [],
      selection_method: null,
    })),
  },
};
