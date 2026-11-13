import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { sessionApi } from '../tenant-session';
import type { SupplierCatalogDiff } from '../tenant-session';
import { SupplierCatalogVersionDiff } from './SupplierCatalogVersionDiff';

afterEach(() => vi.restoreAllMocks());

const before = {
  supplier_id: 'vendor-a', profile_fingerprint: 'a'.repeat(64),
  catalog_version_id: 'version-before',
};
const after = {
  supplier_id: 'vendor-a', profile_fingerprint: 'b'.repeat(64),
  catalog_version_id: 'version-after',
};
const options = [
  { ref: before, label: 'Проверенный исходный прайс A' },
  { ref: after, label: 'Подготовленный прайс B' },
];
const props = { organizationId: 'org', supplierId: 'vendor-a', csrf: 'csrf', options };
const diff: SupplierCatalogDiff = {
  supplier_id: 'vendor-a', before_catalog_version_id: 'version-before',
  after_catalog_version_id: 'version-after',
  before_source_sha256: '1'.repeat(64), after_source_sha256: '2'.repeat(64),
  counts: {
    added: 1, removed: 0, price_changed: 1,
    product_changed: 0, product_and_price_changed: 1,
  },
  total_changes: 3, offset: 0, next_offset: 2,
  requires_manager_review: true,
  changes: [
    { sku: 'A', kind: 'price_changed', changed_fields: [],
      before_price_rub: '10.00', after_price_rub: '12.00',
      comparable_price_delta_rub: '2.00' },
    { sku: 'B', kind: 'product_and_price_changed',
      changed_fields: ['pieces_per_sale_unit'],
      before_price_rub: '5.00', after_price_rub: '50.00',
      comparable_price_delta_rub: null },
  ],
};

describe('A101 source-bound supplier catalog changes', () => {
  it('never picks versions automatically; loads bounded verified changes on demand', async () => {
    const load = vi.spyOn(sessionApi, 'compareSupplierVersions').mockResolvedValue(diff);
    render(<SupplierCatalogVersionDiff {...props}/>);
    expect(load).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText('Изменения товаров и цен · vendor-a'));
    const show = screen.getByRole('button', { name: 'Сравнить версии' });
    expect(show).toBeDisabled();
    fireEvent.change(screen.getByLabelText('Базовая версия · vendor-a'), {
      target: { value: 'a'.repeat(64) + '|version-before' },
    });
    fireEvent.change(screen.getByLabelText('Сравниваемая версия · vendor-a'), {
      target: { value: 'b'.repeat(64) + '|version-after' },
    });
    expect(show).toBeEnabled();
    fireEvent.click(show);
    await waitFor(() => expect(load).toHaveBeenCalledWith(
      'org', before, after, 'csrf', 0,
    ));
    expect(await screen.findByRole('group', { name: 'Изменения прайса vendor-a' }))
      .toHaveTextContent('Изменённых товаров: 3');
    expect(screen.getByRole('row', { name: /B.*Изменились цена и характеристики/ }))
      .toHaveTextContent('Нельзя сравнить');
    expect(screen.getByText(/Новую цену нельзя использовать/, { exact: false }))
      .toBeInTheDocument();
  });

  it('loads the next page without losing earlier changes or inventing price deltas', async () => {
    const next: SupplierCatalogDiff = {
      ...diff, offset: 2, next_offset: null,
      changes: [{
        sku: 'C', kind: 'added', changed_fields: [],
        before_price_rub: null, after_price_rub: '22.00',
        comparable_price_delta_rub: null,
      }],
    };
    const load = vi.spyOn(sessionApi, 'compareSupplierVersions')
      .mockResolvedValueOnce(diff).mockResolvedValueOnce(next);
    render(<SupplierCatalogVersionDiff {...props}/>);
    fireEvent.click(screen.getByText('Изменения товаров и цен · vendor-a'));
    fireEvent.change(screen.getByLabelText('Базовая версия · vendor-a'), {
      target: { value: 'a'.repeat(64) + '|version-before' },
    });
    fireEvent.change(screen.getByLabelText('Сравниваемая версия · vendor-a'), {
      target: { value: 'b'.repeat(64) + '|version-after' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Сравнить версии' }));
    await screen.findByRole('button', { name: 'Показать следующие изменения' });
    fireEvent.click(screen.getByRole('button', { name: 'Показать следующие изменения' }));
    await waitFor(() => expect(load).toHaveBeenLastCalledWith(
      'org', before, after, 'csrf', 2,
    ));
    expect(screen.getByRole('row', { name: /A.*Изменилась цена/ }))
      .toBeInTheDocument();
    expect(screen.getByRole('row', { name: /C.*Товар добавлен/ }))
      .toHaveTextContent('Нельзя сравнить');
    expect(screen.queryByRole('button', { name: 'Показать следующие изменения' }))
      .not.toBeInTheDocument();
  });

  it('rejects mismatched server provenance instead of showing a fabricated report', async () => {
    const load = vi.spyOn(sessionApi, 'compareSupplierVersions').mockResolvedValue({
      ...diff, after_catalog_version_id: 'wrong-version',
    });
    render(<SupplierCatalogVersionDiff {...props}/>);
    fireEvent.click(screen.getByText('Изменения товаров и цен · vendor-a'));
    fireEvent.change(screen.getByLabelText('Базовая версия · vendor-a'), {
      target: { value: 'a'.repeat(64) + '|version-before' },
    });
    fireEvent.change(screen.getByLabelText('Сравниваемая версия · vendor-a'), {
      target: { value: 'b'.repeat(64) + '|version-after' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Сравнить версии' }));
    await waitFor(() => expect(load).toHaveBeenCalledTimes(1));
    expect(await screen.findByRole('alert')).toHaveTextContent('Не удалось подтвердить результат сравнения');
    expect(screen.queryByLabelText('Изменения прайса vendor-a')).not.toBeInTheDocument();
  });
});
