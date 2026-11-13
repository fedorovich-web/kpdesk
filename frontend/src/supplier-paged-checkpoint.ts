/** A89 clone-only paged review storage: no production API exposure.
 * All pages are bound to the same original uploaded source. Restore is
 * fail-closed: callers never receive a partially reconstructed snapshot.
 */
import { SessionApiError, sessionApi } from './tenant-session';
import type { SupplierCanonicalDecision, SupplierCanonicalReview } from './tenant-session';

const SHA = /^[0-9a-f]{64}$/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const KEY = /^[A-Za-z0-9_.-]{8,128}$/;
const MAX_ROWS = 5000;
const PAGE = 50;

function reject(code: string): never {
  throw new SessionApiError(code, 'Не удалось подтвердить сохранённые результаты проверки прайса. Откройте исходный файл и проверьте строки заново.', 0);
}

function validDecision(value: SupplierCanonicalDecision): boolean {
  if (!value || value.confirmed !== true || !Number.isSafeInteger(value.line)
    || value.line < 1 || !SHA.test(value.source_row_sha256)) return false;
  if (value.action === 'exclude') {
    return typeof value.reason === 'string'
      && value.reason.trim().length >= 8 && value.reason.length <= 500;
  }
  return value.action === 'include' && typeof value.category === 'string'
    && value.category.length > 0
    && ['штука', 'пачка', 'коробка'].includes(value.sale_unit)
    && Number.isSafeInteger(value.pieces_per_sale_unit)
    && value.pieces_per_sale_unit >= 1 && value.pieces_per_sale_unit <= 100000;
}

export type SupplierSavedProgress = Readonly<{
  checkpointId: string;
  mappingSha256: string;
  decisions: Readonly<Record<number, SupplierCanonicalDecision>>;
  totalCount: number;
}>;

export type PagedReviewScope = Readonly<{
  organizationId: string;
  supplierId: string;
  source: File;
  csrf: string;
  review: SupplierCanonicalReview;
}>;

function validateScope(scope: PagedReviewScope): void {
  const { organizationId, supplierId, csrf, review } = scope;
  if (!organizationId || !supplierId || !csrf || !SHA.test(review.source_sha256)
    || !review.explicitly_confirmed_price_basis
    || review.mapping.source_sha256 != null
      && review.mapping.source_sha256 !== review.source_sha256) {
    reject('invalid_paged_review_scope');
  }
}

export async function appendSupplierReviewPage(
  scope: PagedReviewScope, decisions: SupplierCanonicalDecision[],
  expectedCheckpointId: string | null, idempotencyKey: string,
): Promise<{ checkpointId: string; mappingSha256: string }> {
  validateScope(scope);
  if (!KEY.test(idempotencyKey)
    || expectedCheckpointId !== null && !UUID.test(expectedCheckpointId)
    || decisions.length < 1 || decisions.length > PAGE
    || decisions.some(decision => !validDecision(decision))
    || new Set(decisions.map(item => item.line)).size !== decisions.length) {
    reject('invalid_paged_review_batch');
  }
  const result = await sessionApi.appendReviewedSupplierCheckpoint(
    scope.organizationId, scope.supplierId, scope.source, scope.csrf,
    { ...scope.review, decisions }, idempotencyKey, expectedCheckpointId,
  );
  if (result.recorded !== true || result.published !== false
    || !UUID.test(result.checkpoint_id)
    || !SHA.test(result.mapping_sha256)
    || result.source_sha256 !== scope.review.source_sha256
    || !result.taxonomy_version) {
    reject('invalid_paged_checkpoint_receipt');
  }
  return { checkpointId: result.checkpoint_id, mappingSha256: result.mapping_sha256 };
}

export async function restoreSupplierReviewPages(
  scope: PagedReviewScope,
): Promise<SupplierSavedProgress | null> {
  validateScope(scope);
  let offset = 0;
  let checkpointId: string | null = null;
  let mappingSha256: string | null = null;
  let reviewerId: string | null = null;
  let totalCount: number | null = null;
  const recovered: Record<number, SupplierCanonicalDecision> = {};
  for (let pageIndex = 0; pageIndex < MAX_ROWS / PAGE; pageIndex += 1) {
    const page = await sessionApi.restoreReviewedSupplierCheckpointPage(
      scope.organizationId, scope.supplierId, scope.source, scope.csrf,
      scope.review, offset, checkpointId, PAGE,
    );
    if (page === null) {
      if (offset === 0) return null;
      reject('paged_checkpoint_disappeared');
    }
    if (!UUID.test(page.checkpoint_id)
      || !UUID.test(page.reviewer_user_id)
      || !SHA.test(page.mapping_sha256)
      || page.source_sha256 !== scope.review.source_sha256
      || page.price_basis !== scope.review.price_basis
      || !page.taxonomy_version || page.published !== false
      || page.offset !== offset || page.limit !== PAGE
      || !Number.isSafeInteger(page.total_count)
      || page.total_count < 1 || page.total_count > MAX_ROWS
      || totalCount !== null && page.total_count !== totalCount
      || checkpointId !== null && page.checkpoint_id !== checkpointId
      || reviewerId !== null && page.reviewer_user_id !== reviewerId
      || mappingSha256 !== null && page.mapping_sha256 !== mappingSha256
      || !Array.isArray(page.decisions) || page.decisions.length < 1
      || page.decisions.length > PAGE
      || offset + page.decisions.length > page.total_count) {
      reject('paged_checkpoint_changed');
    }
    totalCount = page.total_count;
    checkpointId = page.checkpoint_id;
    reviewerId = page.reviewer_user_id;
    mappingSha256 = page.mapping_sha256;
    for (const decision of page.decisions) {
      if (!validDecision(decision) || recovered[decision.line] !== undefined) {
        reject('invalid_paged_checkpoint_decisions');
      }
      recovered[decision.line] = decision;
    }
    const expectedNext = offset + page.decisions.length;
    if (page.next_offset === null) {
      if (expectedNext !== totalCount) reject('paged_checkpoint_incomplete');
      return {
        checkpointId, mappingSha256, decisions: recovered, totalCount,
      };
    }
    if (page.next_offset !== expectedNext || expectedNext >= totalCount) {
      reject('paged_checkpoint_offset_mismatch');
    }
    offset = expectedNext;
  }
  return reject('paged_checkpoint_limit');
}
