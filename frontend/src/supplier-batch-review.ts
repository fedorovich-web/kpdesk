import type { SupplierStageRow } from './tenant-session';

export type VerifiedBatchGroup = {
  sourceCategory: string;
  saleUnit: 'штука' | 'пачка' | 'коробка';
  piecesPerSaleUnit: number;
};

const UNITS: Record<string, VerifiedBatchGroup['saleUnit']> = {
  'шт': 'штука', 'шт.': 'штука', 'штука': 'штука',
  pcs: 'штука', pc: 'штука', piece: 'штука',
  'уп': 'пачка', 'уп.': 'пачка', 'упаковка': 'пачка', 'пачка': 'пачка',
  'кор': 'коробка', 'кор.': 'коробка', 'коробка': 'коробка', box: 'коробка',
};
export const MAX_BATCH_REVIEW = 12;

/** Conservative UI shortlist only. The server still validates every reviewed source row. */
export function verifiedBatchGroup(row: SupplierStageRow): VerifiedBatchGroup | null {
  const unit = row.source_sale_unit
    ? UNITS[row.source_sale_unit.trim().toLocaleLowerCase('ru')] : undefined;
  const pieces = row.source_pack_size;
  const price = row.price_rub;
  if (row.issues.length !== 0
    || !row.supplier_sku?.trim() || !row.name.trim()
    || !row.source_category?.trim()
    || !/^[a-f0-9]{64}$/.test(row.source_row_sha256)
    || row.source_high_precision_price
    || !price || !/^(?:0|[1-9][0-9]{0,11})\.[0-9]{2}$/.test(price)
    || !/[1-9]/.test(price)
    || !unit || !Number.isInteger(pieces) || pieces === null
    || pieces < 1 || pieces > 100000
    || (unit === 'штука' && pieces !== 1)) return null;
  return {
    sourceCategory: row.source_category.trim(),
    saleUnit: unit, piecesPerSaleUnit: pieces,
  };
}

export function commonVerifiedBatchGroup(
  rows: SupplierStageRow[],
): VerifiedBatchGroup | null {
  if (rows.length === 0 || rows.length > MAX_BATCH_REVIEW) return null;
  const first = verifiedBatchGroup(rows[0]!);
  if (!first) return null;
  const skuSet = new Set<string>();
  const lines = new Set<number>();
  for (const row of rows) {
    const group = verifiedBatchGroup(row);
    if (!group || group.sourceCategory !== first.sourceCategory
      || group.saleUnit !== first.saleUnit
      || group.piecesPerSaleUnit !== first.piecesPerSaleUnit
      || skuSet.has(row.supplier_sku!) || lines.has(row.line)) return null;
    skuSet.add(row.supplier_sku!);
    lines.add(row.line);
  }
  return first;
}
