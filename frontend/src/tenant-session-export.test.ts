import { afterEach, describe, expect, it, vi } from 'vitest';
import { sessionApi, SessionApiError } from './tenant-session';
import type { SupplierCanonicalReview } from './tenant-session';

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

const sha = 'b'.repeat(64);
const review: SupplierCanonicalReview = {
  source_sha256: sha, mapping: { header_row: 1, name: 1, price: 2 },
  formula_policy: 'reject', price_basis: 'standard',
  explicitly_confirmed_price_basis: true,
  decisions: [{
    action: 'exclude', source_row_sha256: 'c'.repeat(64), line: 2,
    confirmed: true, reason: 'This row is a heading',
  }],
};

describe('A68 canonical export transfer', () => {
  it('stays HTTPS-only, validates the returned digest and keeps catalog unpublished', async () => {
    const file = new File(['original'], 'supplier.csv', { type: 'text/csv' });
    const spy = vi.fn();
    vi.stubGlobal('fetch', spy);
    await expect(sessionApi.exportReviewedSupplier('org-a', 'supplier', file, 'csrf', review))
      .rejects.toBeInstanceOf(SessionApiError);
    expect(spy).not.toHaveBeenCalled();

    const securedWindow = Object.create(window) as Window;
    Object.defineProperty(securedWindow, 'location', { value: { protocol: 'https:' } });
    vi.stubGlobal('window', securedWindow);
    const bytes = new Uint8Array([115, 107, 117, 10]).buffer;
    const digest = new Uint8Array(32).fill(0xaa).buffer;
    vi.stubGlobal('crypto', { subtle: { digest: vi.fn().mockResolvedValue(digest) } });
    spy.mockResolvedValue({
      ok: true, status: 200,
      headers: new Headers({
        'Content-Type': 'text/csv; charset=utf-8',
        'X-Catalog-SHA256': 'aa'.repeat(32),
        'X-Source-SHA256': sha,
      }),
      arrayBuffer: async () => bytes,
    });
    const csv = await sessionApi.exportReviewedSupplier(
      'org-a', 'supplier', file, 'csrf', review,
    );
    expect(csv.name).toBe('reviewed-supplier.csv');
    expect(csv.type).toBe('text/csv');
    expect(spy).toHaveBeenCalledWith(
      '/api/session/organizations/org-a/suppliers/supplier/canonical/export',
      expect.objectContaining({
        method: 'POST', credentials: 'include', cache: 'no-store',
        headers: { 'X-CSRF-Token': 'csrf' },
      }),
    );
    const sent = spy.mock.calls[0]![1].body as FormData;
    expect(sent.get('file')).toBe(file);
    expect(JSON.parse(String(sent.get('review_json')))).toEqual(review);

    spy.mockResolvedValue({
      ok: true, status: 200,
      headers: new Headers({
        'Content-Type': 'text/csv; charset=utf-8',
        'X-Catalog-SHA256': 'cc'.repeat(32),
        'X-Source-SHA256': sha,
      }),
      arrayBuffer: async () => bytes,
    });
    await expect(sessionApi.exportReviewedSupplier('org-a', 'supplier', file, 'csrf', review))
      .rejects.toMatchObject({ code: 'invalid_catalog_export' });
  });
});

describe('A88 source-locked checkpoint and publication HTTP transfer', () => {
  it('requires HTTPS, sends original bytes under CSRF and pins the canonical digest', async () => {
    const uploaded = new File(['original'], 'supplier.csv', { type: 'text/csv' });
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const checkpointKey = 'review-save-12345678';
    const publishKey = 'review-publish-87654321';
    await expect(sessionApi.saveReviewedSupplierCheckpoint(
      'org-a', 'vendor x', uploaded, 'csrf', review, checkpointKey,
    )).rejects.toMatchObject({ code: 'https_required' });
    expect(fetchMock).not.toHaveBeenCalled();

    const secureWindow = Object.create(window) as Window;
    Object.defineProperty(secureWindow, 'location', { value: { protocol: 'https:' } });
    vi.stubGlobal('window', secureWindow);
    const checkpointId = '11111111-1111-4111-8111-111111111111';
    fetchMock.mockResolvedValueOnce({
      ok: true, status: 201,
      json: async () => ({
        checkpoint_id: checkpointId, source_sha256: sha,
        mapping_sha256: 'd'.repeat(64), taxonomy_version: 'v1',
        recorded: true, published: false,
      }),
    }).mockResolvedValueOnce({
      ok: true, status: 201,
      json: async () => ({
        version_id: '22222222-2222-4222-8222-222222222222',
        source_sha256: 'e'.repeat(64), rows_count: 1,
      }),
    });

    const saved = await sessionApi.saveReviewedSupplierCheckpoint(
      'org-a', 'vendor x', uploaded, 'csrf', review, checkpointKey,
    );
    expect(saved.checkpoint_id).toBe(checkpointId);
    expect(fetchMock).toHaveBeenNthCalledWith(1,
      '/api/session/organizations/org-a/suppliers/vendor%20x/canonical/checkpoints/save',
      expect.objectContaining({
        method: 'POST', credentials: 'include', cache: 'no-store',
        headers: { 'X-CSRF-Token': 'csrf' },
      }),
    );
    const saveBody = (fetchMock.mock.calls[0]![1].body as FormData);
    expect(saveBody.get('file')).toBe(uploaded);
    expect(JSON.parse(String(saveBody.get('review_json')))).toEqual({
      ...review, idempotency_key: checkpointKey, expected_checkpoint_id: null,
    });

    const result = await sessionApi.publishReviewedSupplierCheckpoint(
      'org-a', 'vendor x', checkpointId, uploaded,
      'csrf', review, 'e'.repeat(64), publishKey,
    );
    expect(result.rows_count).toBe(1);
    expect(fetchMock).toHaveBeenNthCalledWith(2,
      '/api/session/organizations/org-a/suppliers/vendor%20x/canonical/checkpoints/'
      + checkpointId + '/publish',
      expect.objectContaining({
        method: 'POST', credentials: 'include', cache: 'no-store',
        headers: {
          'X-CSRF-Token': 'csrf',
          'X-Catalog-SHA256': 'e'.repeat(64),
          'Idempotency-Key': publishKey,
        },
      }),
    );
    const publishBody = fetchMock.mock.calls[1]![1].body as FormData;
    expect(publishBody.get('file')).toBe(uploaded);
    expect(JSON.parse(String(publishBody.get('review_json')))).toEqual({
      source_sha256: sha, mapping: review.mapping,
      formula_policy: 'reject', price_basis: 'standard',
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('does not silently reinterpret a 503 clone-only outage as successful publication', async () => {
    const secureWindow = Object.create(window) as Window;
    Object.defineProperty(secureWindow, 'location', { value: { protocol: 'https:' } });
    vi.stubGlobal('window', secureWindow);
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false, status: 503, json: async () => ({
        code: 'review_signer_unavailable', message: 'Review signer unavailable',
      }),
    }));
    await expect(sessionApi.publishReviewedSupplierCheckpoint(
      'org-a', 'vendor', '11111111-1111-4111-8111-111111111111',
      new File(['x'], 'source.csv'), 'csrf', review,
      'e'.repeat(64), 'review-publish-12345678',
    )).rejects.toMatchObject({
      code: 'review_signer_unavailable', status: 503,
    });
  });
});

describe('A89 multipart paged-review transport', () => {
  it('sends page decisions with a single optimistic checkpoint head and no replacement', async () => {
    const secureWindow = Object.create(window) as Window;
    Object.defineProperty(secureWindow, 'location', { value: { protocol: 'https:' } });
    vi.stubGlobal('window', secureWindow);
    const uploaded = new File(['original'], 'source.csv');
    const fetcher = vi.fn().mockResolvedValueOnce({
      ok: true, status: 201,
      json: async () => ({
        checkpoint_id: '22222222-2222-4222-8222-222222222222',
        source_sha256: sha, mapping_sha256: 'd'.repeat(64),
        taxonomy_version: 'v1', recorded: true, published: false,
      }),
    }).mockResolvedValueOnce({
      ok: true, status: 200,
      json: async () => ({
        checkpoint_id: '22222222-2222-4222-8222-222222222222',
        source_sha256: sha, mapping_sha256: 'd'.repeat(64),
        taxonomy_version: 'v1', price_basis: 'standard',
        reviewer_user_id: '33333333-3333-4333-8333-333333333333',
        total_count: 61, offset: 50, limit: 50, next_offset: null,
        decisions: [review.decisions[0]!], published: false,
      }),
    });
    vi.stubGlobal('fetch', fetcher);
    const expected = '11111111-1111-4111-8111-111111111111';
    await sessionApi.appendReviewedSupplierCheckpoint(
      'org-a', 'supplier-a', uploaded, 'csrf-a', review,
      'review-page-12345678', expected,
    );
    expect(fetcher.mock.calls[0]?.[0]).toBe(
      '/api/session/organizations/org-a/suppliers/supplier-a/canonical/checkpoints/append',
    );
    const firstInit = fetcher.mock.calls[0]?.[1] as RequestInit;
    expect(firstInit.credentials).toBe('include');
    expect(firstInit.headers).toEqual({ 'X-CSRF-Token': 'csrf-a' });
    const firstBody = firstInit.body as FormData;
    expect(firstBody.get('file')).toBe(uploaded);
    expect(JSON.parse(String(firstBody.get('review_json')))).toEqual({
      ...review, expected_checkpoint_id: expected,
      idempotency_key: 'review-page-12345678', replace_existing: false,
    });

    await sessionApi.restoreReviewedSupplierCheckpointPage(
      'org-a', 'supplier-a', uploaded, 'csrf-a', review, 50, expected,
    );
    const secondInit = fetcher.mock.calls[1]?.[1] as RequestInit;
    expect(secondInit.credentials).toBe('include');
    expect(secondInit.headers).toEqual({ 'X-CSRF-Token': 'csrf-a' });
    expect(JSON.parse(String((secondInit.body as FormData).get('review_json')))).toEqual({
      source_sha256: sha, mapping: review.mapping,
      formula_policy: 'reject', price_basis: 'standard',
      offset: 50, limit: 50, expected_checkpoint_id: expected,
    });
  });

  it('preserves the 409 concurrency error from the real HTTP boundary', async () => {
    const secureWindow = Object.create(window) as Window;
    Object.defineProperty(secureWindow, 'location', { value: { protocol: 'https:' } });
    vi.stubGlobal('window', secureWindow);
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false, status: 409, json: async () => ({
        code: 'checkpoint_changed', message: 'Review changed',
      }),
    }));
    await expect(sessionApi.restoreReviewedSupplierCheckpointPage(
      'org-a', 'supplier-a', new File(['x'], 'source.csv'),
      'csrf-a', review, 50, '11111111-1111-4111-8111-111111111111',
    )).rejects.toMatchObject({ code: 'checkpoint_changed', status: 409 });
  });
});
