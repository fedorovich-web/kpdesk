import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { sessionApi } from './tenant-session';
import type { SupplierCanonicalDecision, SupplierCanonicalReview } from './tenant-session';
import type { SupplierSavedProgress } from './supplier-paged-checkpoint';
import { publishSavedSupplierPages } from './supplier-large-publication';

const original = 'a'.repeat(64);
const digest = 'b'.repeat(64);
const checkpoint = '11111111-1111-4111-8111-111111111111';
const version = '22222222-2222-4222-8222-222222222222';
const reviewer = '33333333-3333-4333-8333-333333333333';
const source = new File(['original'], 'source.csv');
const review: SupplierCanonicalReview = {
  source_sha256: original,
  mapping: { name: 1, price: 2, sku: 0, header_row: 1 },
  formula_policy: 'reject', price_basis: 'standard',
  explicitly_confirmed_price_basis: true, decisions: [],
};
const rows: SupplierCanonicalDecision[] = Array.from({ length: 121 }, (_, i) =>
  i === 120 ? {
    line: i + 2, action: 'exclude', confirmed: true,
    source_row_sha256: 'c'.repeat(64),
    reason: 'Section header, not supplier product.',
  } : {
    line: i + 2, action: 'include', confirmed: true,
    source_row_sha256: 'c'.repeat(64),
    category: 'keyboard', sale_unit: 'штука', pieces_per_sale_unit: 1,
    attributes: { connection: 'wired', keys: 104 },
    brand: null, paper_format: null, paper_gsm: null,
    pen_ink_color: null, pen_ink_type: null,
    price_unit_review_evidence: null, precision_review: null,
  },
);
const saved: SupplierSavedProgress = {
  checkpointId: checkpoint, mappingSha256: 'd'.repeat(64),
  totalCount: 121, decisions: Object.fromEntries(rows.map(r => [r.line, r])),
};
const scope = { organizationId: 'org', supplierId: 'supplier',
  source, csrf: 'csrf', review };
const key = 'review-large-12345678';

function response(offset: number, entries: SupplierCanonicalDecision[], next: number | null,
                  head = checkpoint) {
  return {
    checkpoint_id: head, reviewer_user_id: reviewer,
    mapping_sha256: saved.mappingSha256, source_sha256: original,
    taxonomy_version: 'v1', price_basis: 'standard' as const,
    total_count: 121, offset, limit: 50, next_offset: next,
    decisions: entries, published: false as const,
  };
}
beforeEach(() => {
  vi.stubGlobal('crypto', { subtle: {
    digest: vi.fn().mockResolvedValue(new Uint8Array(32).fill(0xbb).buffer),
  } });
  vi.spyOn(sessionApi, 'restoreReviewedSupplierCheckpointPage')
    .mockResolvedValueOnce(response(0, rows.slice(0, 50), 50))
    .mockResolvedValueOnce(response(50, rows.slice(50, 100), 100))
    .mockResolvedValueOnce(response(100, rows.slice(100), null));
  const csv = new File(['catalog'], 'catalog.csv');
  Object.defineProperty(csv, 'arrayBuffer', {
    value: () => Promise.resolve(new Uint8Array([1, 2, 3]).buffer),
  });
  vi.spyOn(sessionApi, 'exportReviewedSupplier').mockResolvedValue(csv);
  vi.spyOn(sessionApi, 'publishReviewedSupplierCheckpoint')
    .mockResolvedValue({ version_id: version, source_sha256: digest, rows_count: 120 });
});
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe('A90 paginated reviewed source publication', () => {
  it('reconstructs all 121 saved reviews then verifies CSV before atomic publish', async () => {
    const current = await publishSavedSupplierPages(scope, saved, 121, key);
    expect(current).toEqual({
      version_id: version, source_sha256: digest, rows_count: 120,
    });
    expect(sessionApi.restoreReviewedSupplierCheckpointPage).toHaveBeenCalledTimes(3);
    expect(sessionApi.exportReviewedSupplier).toHaveBeenCalledWith(
      'org', 'supplier', source, 'csrf',
      { ...review, decisions: rows },
    );
    expect(sessionApi.publishReviewedSupplierCheckpoint).toHaveBeenCalledWith(
      'org', 'supplier', checkpoint, source, 'csrf',
      { ...review, decisions: rows }, digest, key,
    );
  });

  it('rejects incomplete server review before any HTTP call', async () => {
    await expect(publishSavedSupplierPages(scope,
      { ...saved, totalCount: 120 }, 121, key)).rejects.toMatchObject({
      code: 'incomplete_paged_review',
    });
    expect(sessionApi.restoreReviewedSupplierCheckpointPage).not.toHaveBeenCalled();
  });

  it('rejects stale checkpoint head and never publishes', async () => {
    vi.mocked(sessionApi.restoreReviewedSupplierCheckpointPage)
      .mockReset()
      .mockResolvedValueOnce(response(0, rows.slice(0, 50), 50))
      .mockResolvedValueOnce(response(50, rows.slice(50, 100), 100, version));
    await expect(publishSavedSupplierPages(scope, saved, 121, key)).rejects.toMatchObject({
      code: 'paged_checkpoint_changed',
    });
    expect(sessionApi.exportReviewedSupplier).not.toHaveBeenCalled();
    expect(sessionApi.publishReviewedSupplierCheckpoint).not.toHaveBeenCalled();
  });

  it('never publishes when canonical verification fails', async () => {
    vi.mocked(sessionApi.exportReviewedSupplier).mockRejectedValueOnce(
      Object.assign(new Error('SHA mismatch'), { code: 'invalid_catalog_export' }),
    );
    await expect(publishSavedSupplierPages(scope, saved, 121, key)).rejects.toMatchObject({
      code: 'invalid_catalog_export',
    });
    expect(sessionApi.publishReviewedSupplierCheckpoint).not.toHaveBeenCalled();
  });

  it('rejects an invalid catalog version or incorrect number of products', async () => {
    vi.mocked(sessionApi.publishReviewedSupplierCheckpoint).mockResolvedValueOnce({
      version_id: version, source_sha256: digest, rows_count: 119,
    });
    await expect(publishSavedSupplierPages(scope, saved, 121, key)).rejects.toMatchObject({
      code: 'invalid_published_catalog',
    });
  });
});
