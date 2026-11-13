import { afterEach, describe, expect, it, vi } from 'vitest';
import { sessionApi } from './tenant-session';
import type { SupplierCanonicalDecision, SupplierCanonicalReview } from './tenant-session';
import {
  appendSupplierReviewPage, restoreSupplierReviewPages,
} from './supplier-paged-checkpoint';

const SHA = 'a'.repeat(64);
const HEAD = '11111111-1111-4111-8111-111111111111';
const NEXT = '22222222-2222-4222-8222-222222222222';
const REVIEWER = '33333333-3333-4333-8333-333333333333';
const review: SupplierCanonicalReview = {
  source_sha256: SHA, mapping: { header_row: 1, name: 1, price: 2 },
  price_basis: 'standard', formula_policy: 'reject',
  explicitly_confirmed_price_basis: true, decisions: [],
};
const source = new File(['original'], 'supplier.csv', { type: 'text/csv' });
const scope = {
  organizationId: 'org-a', supplierId: 'supplier-a89',
  source, csrf: 'csrf-a89', review,
};
function decision(line: number): SupplierCanonicalDecision {
  return { line, source_row_sha256: 'b'.repeat(64), confirmed: true,
    action: 'exclude', reason: 'Supplier section separator, not a product.' };
}
const first = Array.from({ length: 50 }, (_, index) => decision(index + 2));
const second = Array.from({ length: 50 }, (_, index) => decision(index + 52));
const third = Array.from({ length: 21 }, (_, index) => decision(index + 102));
function page(offset: number, decisions: SupplierCanonicalDecision[],
              next_offset: number | null, head = HEAD) {
  return {
    checkpoint_id: head, reviewer_user_id: REVIEWER,
    source_sha256: SHA, mapping_sha256: 'c'.repeat(64), taxonomy_version: 'v1',
    price_basis: 'standard' as const, total_count: 121, offset, limit: 50,
    next_offset, decisions, published: false as const,
  };
}

afterEach(() => vi.restoreAllMocks());

describe('A89 paged checkpoint integrity and recovery', () => {
  it('restores 121 decisions across pages pinned to one checkpoint head', async () => {
    const restore = vi.spyOn(sessionApi, 'restoreReviewedSupplierCheckpointPage')
      .mockResolvedValueOnce(page(0, first, 50))
      .mockResolvedValueOnce(page(50, second, 100))
      .mockResolvedValueOnce(page(100, third, null));
    const recovered = await restoreSupplierReviewPages(scope);
    expect(recovered?.totalCount).toBe(121);
    expect(Object.keys(recovered!.decisions)).toHaveLength(121);
    expect(recovered?.checkpointId).toBe(HEAD);
    expect(restore).toHaveBeenCalledTimes(3);
    expect(restore.mock.calls.map(args => [args[5], args[6], args[7]])).toEqual([
      [0, null, 50], [50, HEAD, 50], [100, HEAD, 50],
    ]);
  });

  it('accepts a blank server result but never produces fake saved decisions', async () => {
    vi.spyOn(sessionApi, 'restoreReviewedSupplierCheckpointPage')
      .mockResolvedValueOnce(null);
    expect(await restoreSupplierReviewPages(scope)).toBe(null);
  });

  it('rejects a concurrently changed head without returning any partial state', async () => {
    const restore = vi.spyOn(sessionApi, 'restoreReviewedSupplierCheckpointPage')
      .mockResolvedValueOnce(page(0, first, 50))
      .mockResolvedValueOnce(page(50, second, 100, NEXT));
    await expect(restoreSupplierReviewPages(scope)).rejects.toMatchObject({
      code: 'paged_checkpoint_changed',
    });
    expect(restore).toHaveBeenCalledTimes(2);
  });

  it('rejects duplicate lines, tampered hashes, wrong next offset and truncated pages', async () => {
    const malformed = [
      page(0, [...first.slice(0, 49), first[0]!], 50),
      page(0, [{ ...first[0]!, source_row_sha256: '0' }], 1),
      page(0, first, 49),
      page(0, first, null),
      { ...page(0, first, 50), total_count: 5001 },
    ];
    const restore = vi.spyOn(sessionApi, 'restoreReviewedSupplierCheckpointPage');
    for (const result of malformed) {
      restore.mockResolvedValueOnce(result);
      await expect(restoreSupplierReviewPages(scope)).rejects.toBeDefined();
    }
  });

  it('requires explicit complete, bounded and unique decisions for append', async () => {
    const append = vi.spyOn(sessionApi, 'appendReviewedSupplierCheckpoint');
    for (const decisions of [
      [], [...first, decision(150)], [first[0]!, first[0]!],
      [{ ...first[0]!, confirmed: false as true }],
    ]) {
      await expect(appendSupplierReviewPage(scope, decisions, null, 'a89-chunk-0001'))
        .rejects.toMatchObject({ code: 'invalid_paged_review_batch' });
    }
    expect(append).not.toHaveBeenCalled();
  });

  it('appends a page with optimistic head and validates the server receipt', async () => {
    const append = vi.spyOn(sessionApi, 'appendReviewedSupplierCheckpoint')
      .mockResolvedValueOnce({
        checkpoint_id: NEXT, source_sha256: SHA,
        mapping_sha256: 'c'.repeat(64), taxonomy_version: 'v1',
        recorded: true, published: false,
      });
    const receipt = await appendSupplierReviewPage(scope, first, HEAD, 'a89-chunk-0002');
    expect(receipt).toEqual({ checkpointId: NEXT, mappingSha256: 'c'.repeat(64) });
    expect(append).toHaveBeenCalledWith(
      'org-a', 'supplier-a89', source, 'csrf-a89',
      { ...review, decisions: first }, 'a89-chunk-0002', HEAD,
    );
  });

  it('propagates optimistic concurrency conflict instead of replacing reviewed rows', async () => {
    vi.spyOn(sessionApi, 'appendReviewedSupplierCheckpoint').mockRejectedValueOnce(
      Object.assign(new Error('Conflict'), { code: 'checkpoint_conflict', status: 409 }),
    );
    await expect(appendSupplierReviewPage(scope, first, HEAD, 'a89-chunk-0002'))
      .rejects.toMatchObject({ code: 'checkpoint_conflict', status: 409 });
  });

  it('refuses a receipt for different source bytes', async () => {
    vi.spyOn(sessionApi, 'appendReviewedSupplierCheckpoint').mockResolvedValueOnce({
      checkpoint_id: HEAD, source_sha256: 'e'.repeat(64),
      mapping_sha256: 'c'.repeat(64), taxonomy_version: 'v1',
      recorded: true, published: false,
    });
    await expect(appendSupplierReviewPage(scope, first, null, 'a89-chunk-0003'))
      .rejects.toMatchObject({ code: 'invalid_paged_checkpoint_receipt' });
  });
});
