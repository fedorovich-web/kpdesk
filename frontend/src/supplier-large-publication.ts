/** A90 opt-in publication of an already saved, complete paged checkpoint.
 * This never registers a production route or accepts raw client decisions for
 * the final SQL publish. All saved decisions are replayed from the server.
 */
import { SessionApiError, sessionApi } from './tenant-session';
import type { ActiveTenantCatalog, SupplierCanonicalReview } from './tenant-session';
import { restoreSupplierReviewPages } from './supplier-paged-checkpoint';
import type { PagedReviewScope, SupplierSavedProgress } from './supplier-paged-checkpoint';

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const SHA = /^[0-9a-f]{64}$/;
const KEY = /^[A-Za-z0-9_.-]{8,128}$/;

function reject(code: string): never {
  throw new SessionApiError(code, 'Не удалось подтвердить добавление товаров в каталог. Проверьте текущий прайс перед повторной попыткой.', 409);
}

export async function publishSavedSupplierPages(
  scope: PagedReviewScope,
  saved: SupplierSavedProgress,
  totalSourceRows: number,
  publicationKey: string,
): Promise<ActiveTenantCatalog> {
  if (!UUID.test(saved.checkpointId) || !SHA.test(saved.mappingSha256)
    || !Number.isSafeInteger(totalSourceRows)
    || totalSourceRows <= 50 || totalSourceRows > 5000
    || saved.totalCount !== totalSourceRows
    || Object.keys(saved.decisions).length !== totalSourceRows
    || !Object.values(saved.decisions).some(value => value.action === 'include')
    || !KEY.test(publicationKey) || !scope.review.explicitly_confirmed_price_basis) {
    reject('incomplete_paged_review');
  }

  // Reconstruct a clean current snapshot (not the browser's potentially stale
  // draft) across HTTPS. Every page is pinned to one DB checkpoint head.
  const latest = await restoreSupplierReviewPages(scope);
  if (!latest || latest.checkpointId !== saved.checkpointId
    || latest.mappingSha256 !== saved.mappingSha256
    || latest.totalCount !== totalSourceRows
    || Object.keys(latest.decisions).length !== totalSourceRows) {
    reject('paged_checkpoint_changed');
  }
  const decisions = Object.values(latest.decisions).sort((a, b) => a.line - b.line);
  const review: SupplierCanonicalReview = {
    ...scope.review, decisions,
  };
  // The export endpoint independently parses the SAME original upload,
  // verifies each human decision and the response SHA-256 header.
  const canonical = await sessionApi.exportReviewedSupplier(
    scope.organizationId, scope.supplierId, scope.source, scope.csrf, review,
  );
  if (canonical.size < 1 || canonical.size > 10 * 1024 * 1024 || !crypto.subtle) {
    reject('invalid_canonical_export');
  }
  const digest = await crypto.subtle.digest('SHA-256', await canonical.arrayBuffer());
  const canonicalSha256 = Array.from(new Uint8Array(digest), value =>
    value.toString(16).padStart(2, '0')).join('');
  if (!SHA.test(canonicalSha256)) reject('invalid_canonical_export');

  // Publication is a single SQLAlchemy transaction. The server again looks
  // up the immutable, locked saved checkpoint, NOT these export decisions.
  const result = await sessionApi.publishReviewedSupplierCheckpoint(
    scope.organizationId, scope.supplierId, saved.checkpointId,
    scope.source, scope.csrf, review, canonicalSha256, publicationKey,
  );
  const expectedProducts = decisions.filter(item => item.action === 'include').length;
  if (!UUID.test(result.version_id) || result.source_sha256 !== canonicalSha256
    || result.rows_count !== expectedProducts) {
    reject('invalid_published_catalog');
  }
  return result;
}
