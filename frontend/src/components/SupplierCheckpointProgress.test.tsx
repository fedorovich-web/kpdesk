import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { SupplierCanonicalDecision, SupplierCanonicalReview, SupplierStagePreview } from '../tenant-session';
import { sessionApi } from '../tenant-session';
import { SupplierCheckpointProgress } from './SupplierCheckpointProgress';

const SOURCE_SHA = 'a'.repeat(64);
const HEAD1 = '11111111-1111-4111-8111-111111111111';
const HEAD2 = '22222222-2222-4222-8222-222222222222';
const REVIEWER = '33333333-3333-4333-8333-333333333333';
const source = new File(['original'], 'supplier.csv', { type: 'text/csv' });
const review: SupplierCanonicalReview = {
  source_sha256: SOURCE_SHA,
  mapping: { header_row: 1, name: 1, price: 2 },
  formula_policy: 'reject', price_basis: 'standard',
  explicitly_confirmed_price_basis: true, decisions: [],
};
const decision = (line: number): SupplierCanonicalDecision => ({
  action: 'exclude', line, source_row_sha256: 'b'.repeat(64),
  confirmed: true, reason: 'This supplier line is a non-product heading.',
});
function stage(offset: number, lines: number[]): SupplierStagePreview {
  return {
    source_sha256: SOURCE_SHA, total_rows: 121, priced_rub_rows: 120,
    unresolved_rows: 1, offset, next_offset: offset === 100 ? null : offset + 50,
    pack_size_issue_counts: {}, requires_confirmation: true,
    canonical_importable: false, model_training_authorized: false,
    sampled_rows: lines.map(line => ({
      line, source_row_sha256: 'b'.repeat(64), supplier_sku: 'X-' + line,
      name: 'Supplier item ' + line, price_rub: '10.00',
      source_category: null, source_sale_unit: null,
      source_pack_size: null, issues: [], pack_size_review_reason: null,
    })),
  };
}
function props(rows: Record<number, SupplierCanonicalDecision>,
               current: SupplierStagePreview) {
  return {
    organizationId: 'org-a', supplierId: 'supplier-a89', source,
    csrf: 'csrf-a89', review, reviews: rows, stage: current,
    onRestored: vi.fn(), busy: false, priceBasisConfirmed: true,
  };
}
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe('A89 guarded React progress for supplier review over 50 rows', () => {
  it('saves current-page decisions and carries the optimistic head to the next page', async () => {
    vi.stubGlobal('crypto', { randomUUID: vi.fn()
      .mockReturnValueOnce(HEAD1).mockReturnValueOnce(HEAD2)
      .mockReturnValueOnce(HEAD1).mockReturnValueOnce(HEAD2) });
    const append = vi.spyOn(sessionApi, 'appendReviewedSupplierCheckpoint')
      .mockResolvedValueOnce({
        checkpoint_id: HEAD1, source_sha256: SOURCE_SHA,
        mapping_sha256: 'c'.repeat(64), taxonomy_version: 'v1',
        recorded: true, published: false,
      }).mockResolvedValueOnce({
        checkpoint_id: HEAD2, source_sha256: SOURCE_SHA,
        mapping_sha256: 'c'.repeat(64), taxonomy_version: 'v1',
        recorded: true, published: false,
      });
    const initial = props({ 2: decision(2) }, stage(0, [2, 3]));
    const view = render(<SupplierCheckpointProgress {...initial} />);
    const save = screen.getByRole('button', { name: /Сохранить строки текущей страницы/ });
    expect(save).toBeEnabled();
    fireEvent.click(save);
    await waitFor(() => expect(screen.getByText(/Сохранено 1 из 121/))
      .toBeInTheDocument());
    expect(append.mock.calls[0]?.[6]).toBeNull();

    const advanced = props({ 2: decision(2), 52: decision(52) }, stage(50, [52]));
    view.rerender(<SupplierCheckpointProgress {...advanced} />);
    fireEvent.click(save);
    await waitFor(() => expect(screen.getByText(/Сохранено 2 из 121/))
      .toBeInTheDocument());
    expect(append.mock.calls[1]?.[6]).toBe(HEAD1);
    expect(append.mock.calls[1]).toHaveLength(7); // default is replace_existing=false
    expect(append.mock.calls[1]?.[4].decisions).toEqual([decision(52)]);
  });

  it('restores server progress only after all pages validate', async () => {
    const restore = vi.spyOn(sessionApi, 'restoreReviewedSupplierCheckpointPage')
      .mockResolvedValueOnce({
        checkpoint_id: HEAD1, reviewer_user_id: REVIEWER,
        source_sha256: SOURCE_SHA, mapping_sha256: 'c'.repeat(64),
        taxonomy_version: 'v1', price_basis: 'standard',
        total_count: 2, offset: 0, limit: 50, next_offset: null,
        decisions: [decision(2), decision(52)], published: false,
      });
    const p = props({}, stage(0, [2, 3]));
    const received = vi.fn();
    render(<SupplierCheckpointProgress {...p} onRestored={received} />);
    fireEvent.click(screen.getByRole('button', {
      name: 'Восстановить сохранённые решения',
    }));
    await waitFor(() => expect(received).toHaveBeenCalledWith({
      2: decision(2), 52: decision(52),
    }));
    expect(restore).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('status')).toHaveTextContent('2');
  });

  it('refuses to discard unsaved work and never restores on a stale checkpoint', async () => {
    const restore = vi.spyOn(sessionApi, 'restoreReviewedSupplierCheckpointPage');
    render(<SupplierCheckpointProgress {...props({ 2: decision(2) }, stage(0, [2]))} />);
    const restoreButton = screen.getByRole('button', {
      name: 'Восстановить сохранённые решения',
    });
    expect(restoreButton).toBeDisabled();
    expect(restore).not.toHaveBeenCalled();
    fireEvent.click(screen.getByLabelText(
      'Я понимаю, что восстановление заменит несохранённые локальные решения',
    ));
    expect(restoreButton).toBeEnabled();
  });
});

describe('A90 reviewed 121-line catalog publication in isolated React control', () => {
  it('enables publishing only for a complete restored review and reports actual version', async () => {
    const original: SupplierCanonicalDecision[] = Array.from({ length: 121 },
      (_, index): SupplierCanonicalDecision => index === 120 ? decision(index + 2)
      : {
          line: index + 2, source_row_sha256: 'b'.repeat(64),
          confirmed: true, action: 'include', category: 'keyboard',
          sale_unit: 'штука', pieces_per_sale_unit: 1,
          attributes: { connection: 'wired', keys: 104 },
          brand: null, paper_format: null, paper_gsm: null,
          pen_ink_color: null, pen_ink_type: null,
          price_unit_review_evidence: null, precision_review: null,
        });
    const paged = (offset: number, part: typeof original, next: number | null) => ({
      checkpoint_id: HEAD1, reviewer_user_id: REVIEWER,
      source_sha256: SOURCE_SHA, mapping_sha256: 'c'.repeat(64),
      taxonomy_version: 'v1', price_basis: 'standard' as const,
      total_count: 121, offset, limit: 50, next_offset: next,
      decisions: part, published: false as const,
    });
    const restore = vi.spyOn(sessionApi, 'restoreReviewedSupplierCheckpointPage');
    for (let i = 0; i < 2; i += 1) {
      restore.mockResolvedValueOnce(paged(0, original.slice(0, 50), 50))
        .mockResolvedValueOnce(paged(50, original.slice(50, 100), 100))
        .mockResolvedValueOnce(paged(100, original.slice(100), null));
    }
    vi.stubGlobal('crypto', {
      randomUUID: vi.fn().mockReturnValue(HEAD2),
      subtle: { digest: vi.fn().mockResolvedValue(new Uint8Array(32).fill(0xbb).buffer) },
    });
    const canonical = new File(['catalog'], 'catalog.csv');
    Object.defineProperty(canonical, 'arrayBuffer', {
      value: () => Promise.resolve(new Uint8Array([1, 2, 3]).buffer),
    });
    const exportCsv = vi.spyOn(sessionApi, 'exportReviewedSupplier').mockResolvedValue(canonical);
    const publish = vi.spyOn(sessionApi, 'publishReviewedSupplierCheckpoint')
      .mockResolvedValue({
        version_id: HEAD2, source_sha256: 'b'.repeat(64), rows_count: 120,
      });
    const published = vi.fn();
    render(<SupplierCheckpointProgress {...props({}, stage(0, [2]))}
      onPublished={published}/>);
    const publishButton = screen.getByRole('button', {
      name: 'Проверить и опубликовать весь прайс (тестовый стенд)',
    });
    expect(publishButton).toBeDisabled();
    fireEvent.click(screen.getByRole('button', {
      name: 'Восстановить сохранённые решения',
    }));
    await waitFor(() => expect(screen.getByText(/Сохранено 121 из 121/))
      .toBeInTheDocument());
    expect(publishButton).toBeEnabled();
    fireEvent.click(publishButton);
    await waitFor(() => expect(published).toHaveBeenCalledWith({
      version_id: HEAD2, source_sha256: 'b'.repeat(64), rows_count: 120,
    }));
    expect(restore).toHaveBeenCalledTimes(6);
    expect(exportCsv).toHaveBeenCalledTimes(1);
    expect(publish).toHaveBeenCalledWith(
      'org-a', 'supplier-a89', HEAD1, source, 'csrf-a89',
      expect.objectContaining({ decisions: original }),
      'b'.repeat(64), 'review-large-' + HEAD2,
    );
    expect(screen.getByText(/Опубликована версия каталога:/))
      .toHaveTextContent(HEAD2);
  });
});
