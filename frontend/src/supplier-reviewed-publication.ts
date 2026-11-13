/** A88 explicitly opt-in bridge for the clone-only checkpoint/publish API.
 * Never called from the production import screen. The existing CSV export
 * remains the only normal user path until a production migration is approved.
 */
import { SessionApiError, sessionApi } from './tenant-session';
import type { ActiveTenantCatalog, SupplierCanonicalReview } from './tenant-session';

const SHA256 = /^[a-f0-9]{64}$/;
const UUID = /^[a-f0-9]{8}-[a-f0-9]{4}-[1-8][a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/i;
const KEY = /^[A-Za-z0-9_.-]{8,128}$/;

export type ReviewedPublicationAttempt = Readonly<{
  checkpointKey: string;
  publicationKey: string;
}>;

export function createReviewedPublicationAttempt(): ReviewedPublicationAttempt {
  if (!globalThis.crypto?.randomUUID) {
    throw new SessionApiError('secure_random_unavailable',
      'Не удалось безопасно начать сохранение прайса. Проверьте, что сайт открыт по HTTPS.', 0);
  }
  return Object.freeze({
    checkpointKey: 'review-save-' + crypto.randomUUID(),
    publicationKey: 'review-publish-' + crypto.randomUUID(),
  });
}

export async function publishConfirmedSupplierReview(args: {
  organizationId: string;
  supplierId: string;
  source: File;
  csrf: string;
  review: SupplierCanonicalReview;
  totalRows: number;
  attempt: ReviewedPublicationAttempt;
}): Promise<ActiveTenantCatalog> {
  const { organizationId, supplierId, source, csrf, review, totalRows, attempt } = args;
  const decisions = review.decisions;
  if (!organizationId || !supplierId || !csrf
    || !SHA256.test(review.source_sha256)
    || !Number.isInteger(totalRows) || totalRows < 1 || totalRows > 50
    || decisions.length !== totalRows || !decisions.some(item => item.action === 'include')
    || !decisions.every(item => item.confirmed === true
      && Number.isInteger(item.line) && item.line > 0
      && SHA256.test(item.source_row_sha256))
    || new Set(decisions.map(item => item.line)).size !== totalRows
    || (review.mapping.source_sha256 != null
      && review.mapping.source_sha256 !== review.source_sha256)
    || !review.explicitly_confirmed_price_basis
    || !KEY.test(attempt.checkpointKey) || !KEY.test(attempt.publicationKey)
    || attempt.checkpointKey === attempt.publicationKey) {
    throw new SessionApiError('invalid_review_for_publication',
      'Перед добавлением товаров в каталог подтвердите каждую строку и тип цены.', 0);
  }

  // The server validates source bytes and each immutable decision on every step.
  const checkpoint = await sessionApi.saveReviewedSupplierCheckpoint(
    organizationId, supplierId, source, csrf, review, attempt.checkpointKey,
  );
  if (!checkpoint.recorded || checkpoint.published !== false
    || !UUID.test(checkpoint.checkpoint_id)
    || checkpoint.source_sha256 !== review.source_sha256
    || !SHA256.test(checkpoint.mapping_sha256)
    || !checkpoint.taxonomy_version) {
    throw new SessionApiError('invalid_checkpoint_response',
      'Не удалось подтвердить, что результаты проверки относятся к этому прайсу.', 0);
  }
  // Export endpoint additionally verifies the response header SHA and original
  // source SHA. Never trust a caller-supplied digest for the publish endpoint.
  const canonical = await sessionApi.exportReviewedSupplier(
    organizationId, supplierId, source, csrf, review,
  );
  if (canonical.size < 1 || canonical.size > 10 * 1024 * 1024 || !crypto.subtle) {
    throw new SessionApiError('invalid_canonical_export',
      'Не удалось получить подготовленный CSV-файл каталога.', 0);
  }
  const digest = await crypto.subtle.digest('SHA-256', await canonical.arrayBuffer());
  const canonicalSha256 = Array.from(new Uint8Array(digest),
    value => value.toString(16).padStart(2, '0')).join('');
  if (!SHA256.test(canonicalSha256)) {
    throw new SessionApiError('invalid_canonical_export', 'Проверка целостности файла не пройдена. Скачайте каталог заново.', 0);
  }
  const published = await sessionApi.publishReviewedSupplierCheckpoint(
    organizationId, supplierId, checkpoint.checkpoint_id,
    source, csrf, review, canonicalSha256, attempt.publicationKey,
  );
  if (!UUID.test(published.version_id)
    || published.source_sha256 !== canonicalSha256
    || !Number.isSafeInteger(published.rows_count)
    || published.rows_count < 1) {
    throw new SessionApiError('invalid_published_catalog',
      'Не удалось подтвердить новую версию прайса. Проверьте каталог перед повторной попыткой.', 0);
  }
  return published;
}
