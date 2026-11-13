import { describe, expect, it } from 'vitest';
import { commonVerifiedBatchGroup, MAX_BATCH_REVIEW, verifiedBatchGroup } from './supplier-batch-review';
import type { SupplierStageRow } from './tenant-session';

const base: SupplierStageRow = {
  line: 2, source_row_sha256: 'a'.repeat(64),
  supplier_sku: 'P-001', name: 'Office paper A4 80 gsm',
  price_rub: '390.00', source_high_precision_price: null,
  source_category: 'Paper/A4', source_sale_unit: 'пачка',
  source_pack_size: 500, issues: [], pack_size_review_reason: null,
};

describe('A70 bounded batch review eligibility', () => {
  it('accepts only complete rows with verified currency, denominator, identity and category', () => {
    expect(verifiedBatchGroup(base)).toEqual({
      sourceCategory: 'Paper/A4', saleUnit: 'пачка', piecesPerSaleUnit: 500,
    });
    const rows: SupplierStageRow[] = [
      base, { ...base, line: 3, supplier_sku: 'P-002', source_row_sha256: 'b'.repeat(64) },
    ];
    expect(commonVerifiedBatchGroup(rows)).toEqual(verifiedBatchGroup(base));
    expect(commonVerifiedBatchGroup([...rows, { ...base, line: 4, supplier_sku: 'P-003',
      source_category: 'Other', source_row_sha256: 'c'.repeat(64) }])).toBeNull();
    expect(commonVerifiedBatchGroup([...rows, { ...base, line: 4, supplier_sku: 'P-003',
      source_pack_size: 100, source_row_sha256: 'c'.repeat(64) }])).toBeNull();
    expect(commonVerifiedBatchGroup([...rows, rows[0]!])).toBeNull();
    expect(commonVerifiedBatchGroup([...rows, { ...base, line: 4,
      source_row_sha256: 'c'.repeat(64) }])).toBeNull();
  });

  it('never batches missing/uncertain commercial facts or oversized selections', () => {
    const bad: Partial<SupplierStageRow>[] = [
      { issues: ['price_precision_requires_review'] },
      { source_high_precision_price: '390.125' },
      { price_rub: null }, { price_rub: '-1.00' }, { price_rub: '0.00' },
      { source_sale_unit: null }, { source_sale_unit: 'mystery' },
      { source_pack_size: null }, { source_pack_size: 0 },
      { supplier_sku: null }, { name: '' }, { source_category: null },
      { source_row_sha256: 'x' },
      { issues: ['xlsx_formula_cell_quarantined'] },
      { source_sale_unit: 'штука', source_pack_size: 12 },
    ];
    for (const mutation of bad) {
      expect(verifiedBatchGroup({ ...base, ...mutation })).toBeNull();
    }
    expect(commonVerifiedBatchGroup([])).toBeNull();
    expect(commonVerifiedBatchGroup(Array.from({ length: MAX_BATCH_REVIEW + 1 },
      (_, index) => ({ ...base, line: index + 2, supplier_sku: 'P-' + index }),
    ))).toBeNull();
  });
});
