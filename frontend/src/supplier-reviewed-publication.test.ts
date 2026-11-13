import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { MockInstance } from 'vitest';
import { SessionApiError, sessionApi } from './tenant-session';
import type { SupplierCanonicalReview } from './tenant-session';
import {
  createReviewedPublicationAttempt, publishConfirmedSupplierReview,
} from './supplier-reviewed-publication';

const sha = 'a'.repeat(64);
const canonicalSha = 'b'.repeat(64);
const checkpointId = '11111111-1111-4111-8111-111111111111';
const publishedId = '22222222-2222-4222-8222-222222222222';
const source = new File(['source'], 'source.csv', { type: 'text/csv' });
const review: SupplierCanonicalReview = {
  source_sha256: sha, mapping: { header_row: 1, name: 1, price: 2, sku: 0 },
  formula_policy: 'reject', price_basis: 'standard',
  explicitly_confirmed_price_basis: true,
  decisions: [{
    action: 'include', line: 2, confirmed: true, source_row_sha256: 'c'.repeat(64),
    category: 'keyboard', sale_unit: 'штука', pieces_per_sale_unit: 1,
    attributes: { connection: 'wired', keys: 104 },
    brand: null, paper_format: null, paper_gsm: null, pen_ink_color: null,
    pen_ink_type: null, price_unit_review_evidence: null, precision_review: null,
  }],
};
const attempt = {
  checkpointKey: 'review-save-0001',
  publicationKey: 'review-publish-0001',
};

const request = () => ({
  organizationId: 'org-a', supplierId: 'supplier-a88', source, csrf: 'csrf-a',
  review, totalRows: 1, attempt,
});

let save: MockInstance<typeof sessionApi.saveReviewedSupplierCheckpoint>;
let exportCsv: MockInstance<typeof sessionApi.exportReviewedSupplier>;
let publish: MockInstance<typeof sessionApi.publishReviewedSupplierCheckpoint>;

beforeEach(() => {
  vi.stubGlobal('crypto', {
    randomUUID: vi.fn().mockReturnValueOnce(checkpointId).mockReturnValueOnce(publishedId),
    subtle: { digest: vi.fn().mockResolvedValue(new Uint8Array(32).fill(0xbb).buffer) },
  });
  save = vi.spyOn(sessionApi, 'saveReviewedSupplierCheckpoint').mockResolvedValue({
    checkpoint_id: checkpointId, source_sha256: sha,
    mapping_sha256: 'd'.repeat(64), taxonomy_version: 'kpdesk-v1',
    recorded: true, published: false,
  });
  const canonical = new File(['catalog'], 'reviewed.csv', { type: 'text/csv' });
  Object.defineProperty(canonical, 'arrayBuffer', {
    value: () => Promise.resolve(new Uint8Array([1, 2, 3, 4]).buffer),
  });
  exportCsv = vi.spyOn(sessionApi, 'exportReviewedSupplier').mockResolvedValue(canonical);
  publish = vi.spyOn(sessionApi, 'publishReviewedSupplierCheckpoint').mockResolvedValue({
    version_id: publishedId, source_sha256: canonicalSha, rows_count: 1,
  });
});
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe('A88 clone-only reviewed supplier publication workflow', () => {
  it('saves original decisions, verifies exported CSV and publishes the same source', async () => {
    const result = await publishConfirmedSupplierReview(request());
    expect(result.version_id).toBe(publishedId);
    expect(save).toHaveBeenCalledWith(
      'org-a', 'supplier-a88', source, 'csrf-a', review, attempt.checkpointKey,
    );
    expect(exportCsv).toHaveBeenCalledWith(
      'org-a', 'supplier-a88', source, 'csrf-a', review,
    );
    expect(publish).toHaveBeenCalledWith(
      'org-a', 'supplier-a88', checkpointId, source, 'csrf-a',
      review, canonicalSha, attempt.publicationKey,
    );
    expect(save.mock.invocationCallOrder[0]).toBeLessThan(
      exportCsv.mock.invocationCallOrder[0]!,
    );
    expect(exportCsv.mock.invocationCallOrder[0]).toBeLessThan(
      publish.mock.invocationCallOrder[0]!,
    );
  });

  it('retains caller-provided idempotency keys across repeat requests', async () => {
    await publishConfirmedSupplierReview(request());
    await publishConfirmedSupplierReview(request());
    expect(save).toHaveBeenCalledTimes(2);
    expect(publish).toHaveBeenCalledTimes(2);
    expect(publish.mock.calls[0]?.[7]).toBe(attempt.publicationKey);
    expect(publish.mock.calls[1]?.[7]).toBe(attempt.publicationKey);
  });

  it('refuses partial or duplicate manual reviews before any HTTP call', async () => {
    for (const invalid of [
      { ...request(), totalRows: 2 },
      { ...request(), review: { ...review, decisions: [review.decisions[0]!, review.decisions[0]!] }, totalRows: 2 },
      { ...request(), totalRows: 51 },
      { ...request(), review: { ...review, explicitly_confirmed_price_basis: false as true } },
    ]) {
      await expect(publishConfirmedSupplierReview(invalid)).rejects.toMatchObject({
        code: 'invalid_review_for_publication',
      });
    }
    expect(save).not.toHaveBeenCalled();
    expect(exportCsv).not.toHaveBeenCalled();
    expect(publish).not.toHaveBeenCalled();
  });

  it('rejects untrusted checkpoint acknowledgement before export', async () => {
    save.mockResolvedValueOnce({
      checkpoint_id: checkpointId, source_sha256: 'e'.repeat(64),
      mapping_sha256: 'd'.repeat(64), taxonomy_version: 'v1',
      recorded: true, published: false,
    });
    await expect(publishConfirmedSupplierReview(request())).rejects.toMatchObject({
      code: 'invalid_checkpoint_response',
    });
    expect(exportCsv).not.toHaveBeenCalled();
    expect(publish).not.toHaveBeenCalled();
  });

  it('never publishes when export SHA verification fails', async () => {
    exportCsv.mockRejectedValueOnce(new SessionApiError(
      'invalid_catalog_export', 'SHA mismatch', 0,
    ));
    await expect(publishConfirmedSupplierReview(request())).rejects.toMatchObject({
      code: 'invalid_catalog_export',
    });
    expect(publish).not.toHaveBeenCalled();
  });

  it('rejects a server-published digest that differs from verified CSV bytes', async () => {
    publish.mockResolvedValueOnce({
      version_id: publishedId, source_sha256: 'f'.repeat(64), rows_count: 1,
    });
    await expect(publishConfirmedSupplierReview(request())).rejects.toMatchObject({
      code: 'invalid_published_catalog',
    });
  });

  it('does not fallback to direct import when checkpoint publish is unavailable', async () => {
    publish.mockRejectedValueOnce(new SessionApiError('checkpoint_unavailable', 'Disabled', 503));
    const direct = vi.spyOn(sessionApi, 'importCatalog');
    await expect(publishConfirmedSupplierReview(request())).rejects.toMatchObject({
      code: 'checkpoint_unavailable',
    });
    expect(direct).not.toHaveBeenCalled();
  });

  it('uses cryptographic UUIDs instead of predictable idempotency keys', () => {
    const generated = createReviewedPublicationAttempt();
    expect(generated.checkpointKey).toBe('review-save-' + checkpointId);
    expect(generated.publicationKey).toBe('review-publish-' + publishedId);
    expect(Object.isFrozen(generated)).toBe(true);
  });
});
