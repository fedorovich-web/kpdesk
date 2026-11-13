import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { sessionApi } from '../tenant-session';
import { SupplierCatalogChanges } from './SupplierCatalogChanges';

const approved = [
  { supplier_id: 'vendor', profile_fingerprint: 'a'.repeat(64),
    catalog_version_id: 'version-old', source_sha256: 'b'.repeat(64),
    rows_count: 2, evidence: 'original' as const },
  { supplier_id: 'vendor', profile_fingerprint: 'c'.repeat(64),
    catalog_version_id: 'version-new', source_sha256: 'd'.repeat(64),
    rows_count: 3, evidence: 'reviewed_canonical' as const },
];

afterEach(() => vi.restoreAllMocks());

function fixture() {
  return vi.spyOn(sessionApi, 'supplierVersions').mockResolvedValue({
    supplier_id: 'vendor', options: approved, truncated: false,
    canonical_discovery_available: true,
  });
}

describe('A101 independent supplier catalog history in tenant workspace', () => {
  it('requires explicit source lookup and selection without a supplier registry', async () => {
    const sources = fixture();
    const compare = vi.spyOn(sessionApi, 'compareSupplierVersions');
    render(<SupplierCatalogChanges organizationId="org" csrf="csrf"/>);
    const selector = screen.getByLabelText('ID поставщика для сравнения');
    expect(sources).not.toHaveBeenCalled();
    fireEvent.change(selector, { target: { value: 'vendor' } });
    fireEvent.click(screen.getByRole('button', { name: 'Найти подтверждённые версии' }));
    await waitFor(() => expect(sources).toHaveBeenCalledWith('org', 'vendor', 'csrf'));
    expect(await screen.findByText('Изменения товаров и цен · vendor')).toBeInTheDocument();
    expect(compare).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText('Изменения товаров и цен · vendor'));
    expect(screen.getByRole('button', { name: 'Сравнить версии' }))
      .toBeDisabled();
  });

  it('rejects cross-supplier source history rather than showing unreliable versions', async () => {
    fixture().mockResolvedValue({
      supplier_id: 'spoofed', options: approved, truncated: false,
      canonical_discovery_available: true,
    });
    const compare = vi.spyOn(sessionApi, 'compareSupplierVersions');
    render(<SupplierCatalogChanges organizationId="org" csrf="csrf"/>);
    const selector = screen.getByLabelText('ID поставщика для сравнения');
    fireEvent.change(selector, { target: { value: 'vendor' } });
    fireEvent.click(screen.getByRole('button', { name: 'Найти подтверждённые версии' }));
    expect(await screen.findByText('Ответ содержит версии другого поставщика.'))
      .toBeInTheDocument();
    expect(screen.queryByText('Изменения товаров и цен · vendor')).not.toBeInTheDocument();
    expect(compare).not.toHaveBeenCalled();
  });

  it('clears old supplier versions and diff when the supplier identity changes', async () => {
    fixture();
    render(<SupplierCatalogChanges organizationId="org" csrf="csrf"/>);
    const selector = screen.getByLabelText('ID поставщика для сравнения');
    fireEvent.change(selector, { target: { value: 'vendor' } });
    fireEvent.click(screen.getByRole('button', { name: 'Найти подтверждённые версии' }));
    await screen.findByText('Изменения товаров и цен · vendor');
    fireEvent.change(screen.getByLabelText('ID поставщика для сравнения'), {
      target: { value: 'vendor-new' },
    });
    expect(screen.queryByText('Изменения товаров и цен · vendor')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Найти подтверждённые версии' }))
      .toBeEnabled();
  });
});


describe('A102 historical-only same-layout mapping audit', () => {
  it('selects different immutable review checksums and never calls live quote sources', async () => {
    const live = fixture();
    const history = vi.spyOn(sessionApi, 'supplierHistoricalSources').mockResolvedValue({
      supplier_id: 'vendor', audit_only: true, truncated: false,
      options: [
        { supplier_id: 'vendor', profile_sha256: '1'.repeat(64),
          profile_fingerprint: 'f'.repeat(64), catalog_version_id: 'old-xlsx',
          source_sha256: 'a'.repeat(64), rows_count: 2, evidence: 'original' },
        { supplier_id: 'vendor', profile_sha256: '2'.repeat(64),
          profile_fingerprint: 'f'.repeat(64), catalog_version_id: 'new-xlsx',
          source_sha256: 'b'.repeat(64), rows_count: 2, evidence: 'original' },
      ],
    });
    const compare = vi.spyOn(sessionApi, 'compareHistoricalSupplierVersions').mockResolvedValue({
      supplier_id: 'vendor', before_catalog_version_id: 'old-xlsx',
      after_catalog_version_id: 'new-xlsx',
      before_source_sha256: 'a'.repeat(64), after_source_sha256: 'b'.repeat(64),
      counts: { added: 0, removed: 0, price_changed: 1,
        product_changed: 0, product_and_price_changed: 0 },
      total_changes: 1, offset: 0, next_offset: null, requires_manager_review: true,
      changes: [{ sku: 'SKU-1', kind: 'price_changed',
        changed_fields: [], before_price_rub: '1111.00',
        after_price_rub: '990.00', comparable_price_delta_rub: '-121.00' }],
    });
    const currentDiff = vi.spyOn(sessionApi, 'compareSupplierVersions');
    render(<SupplierCatalogChanges organizationId="org" csrf="csrf"/>);
    fireEvent.change(screen.getByLabelText('Режим истории поставщика'), {
      target: { value: 'history' },
    });
    const selector = screen.getByLabelText('ID поставщика для сравнения');
    fireEvent.change(selector, { target: { value: 'vendor' } });
    fireEvent.click(screen.getByRole('button', { name: 'Найти подтверждённые версии' }));
    await waitFor(() => expect(history).toHaveBeenCalledWith('org', 'vendor', 'csrf'));
    expect(live).not.toHaveBeenCalled();
    fireEvent.click(await screen.findByText('Сравнение архивных прайсов · vendor'));
    expect(screen.getByText(/не добавляются к источникам новых КП/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Базовая версия · vendor'), {
      target: { value: '1'.repeat(64) + '|old-xlsx' },
    });
    fireEvent.change(screen.getByLabelText('Сравниваемая версия · vendor'), {
      target: { value: '2'.repeat(64) + '|new-xlsx' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Сравнить версии' }));
    await waitFor(() => expect(compare).toHaveBeenCalledWith(
      'org',
      { supplier_id: 'vendor', profile_sha256: '1'.repeat(64),
        catalog_version_id: 'old-xlsx' },
      { supplier_id: 'vendor', profile_sha256: '2'.repeat(64),
        catalog_version_id: 'new-xlsx' }, 'csrf', 0,
    ));
    expect(await screen.findByLabelText('Изменения прайса vendor'))
      .toHaveTextContent('-121.00');
    expect(currentDiff).not.toHaveBeenCalled();
  });

  it('does not reclassify audit-only sources as currently quote-eligible', async () => {
    fixture();
    vi.spyOn(sessionApi, 'supplierHistoricalSources').mockResolvedValue({
      supplier_id: 'vendor', options: [], truncated: false, audit_only: true,
    });
    render(<SupplierCatalogChanges organizationId="org" csrf="csrf"/>);
    fireEvent.change(screen.getByLabelText('Режим истории поставщика'), {
      target: { value: 'history' },
    });
    const selector = screen.getByLabelText('ID поставщика для сравнения');
    fireEvent.change(selector, { target: { value: 'vendor' } });
    fireEvent.click(screen.getByRole('button', { name: 'Найти подтверждённые версии' }));
    expect(await screen.findByText(/два ранее загруженных исходных прайса/))
      .toBeInTheDocument();
    expect(screen.queryByText('Сравнение архивных прайсов · vendor')).not.toBeInTheDocument();
  });
});
