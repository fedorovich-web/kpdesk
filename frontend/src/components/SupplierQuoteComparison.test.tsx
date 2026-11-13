import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { sessionApi } from '../tenant-session';
import type { QuoteLineInput, SupplierQuotePreview } from '../tenant-session';
import { SupplierQuoteComparison } from './SupplierQuoteComparison';

const lines: QuoteLineInput[] = [{
  source_quote: 'Карандаш графитовый HB', description: 'Карандаш графитовый HB',
  category: 'pencil', quantity: 2, unit: 'штука', brand: null,
  paper_format: null, paper_gsm: null, pen_ink_color: null, pen_ink_type: null,
  attributes: { pencil_type: 'graphite', lead_grade: 'HB' },
}];
const offer = (supplier: string, version: string, price: string) => ({
  supplier_id: supplier, catalog_version_id: version,
  source_sha256: supplier === 'supplier_one' ? 'a'.repeat(64) : 'b'.repeat(64),
  sku: 'SKU-IDENTICAL', product_name: 'Карандаш графитовый HB',
  product_brand: 'PaperCo', sale_quantity: 2, sale_unit: 'штука',
  unit_price_rub: price, base_total_rub: String(Number(price) * 2),
  selling_total_rub: String(Number(price) * 2),
});

afterEach(() => { vi.restoreAllMocks(); });

function fixture() {
  vi.spyOn(sessionApi, 'catalogVersions').mockRejectedValue(new Error('Legacy discovery called'));
  vi.spyOn(sessionApi, 'discoverSupplierSources').mockResolvedValue({
    suppliers: [
      { supplier_id: 'supplier_one', truncated: false,
        canonical_discovery_available: true, options: [{
          supplier_id: 'supplier_one', profile_fingerprint: 'c'.repeat(64),
          catalog_version_id: 'ver-one', source_sha256: 'a'.repeat(64),
          rows_count: 5, evidence: 'original',
        }] },
      { supplier_id: 'supplier_two', truncated: false,
        canonical_discovery_available: true, options: [{
          supplier_id: 'supplier_two', profile_fingerprint: 'd'.repeat(64),
          catalog_version_id: 'ver-two', source_sha256: 'b'.repeat(64),
          rows_count: 10, evidence: 'reviewed_canonical',
        }] },
    ],
  });
  const preview: SupplierQuotePreview = {
    requires_manager_confirmation: true, catalog_source_count: 2,
    lines: [{
      line_number: 1, description: lines[0]!.description, status: 'select_supplier',
      offers: [offer('supplier_two', 'ver-two', '12.00'),
        offer('supplier_one', 'ver-one', '20.00')],
    }],
  };
  const previewCall = vi.spyOn(sessionApi, 'previewSupplierQuote').mockResolvedValue(preview);
  return previewCall;
}

const props = () => ({
  organizationId: 'org-test', activeVersionId: 'active-catalog',
  csrf: 'csrf-test', requestLines: lines, onSaved: vi.fn(),
});

describe('source-verified supplier comparison', () => {
  it('never auto-selects a price and saves only the explicitly chosen source', async () => {
    const previewCall = fixture();
    const p = props();
    const save = vi.spyOn(sessionApi, 'createSupplierQuote').mockResolvedValue({
      quote_id: 'quote-new', revision: 1, catalog_version_id: 'active-catalog',
      ready_for_draft: true, total_rub: '24.00', lines: [],
      snapshot_sha256: 'sha', approved: false,
    });
    render(<SupplierQuoteComparison {...p} />);
    expect(screen.queryByRole('button', { name: 'Создать КП с выбранными поставщиками' }))
      .not.toBeInTheDocument();
    const suppliers = screen.getAllByPlaceholderText('supplier_company_a');
    fireEvent.change(suppliers[0]!, { target: { value: 'supplier_one' } });
    fireEvent.change(suppliers[1]!, { target: { value: 'supplier_two' } });
    fireEvent.click(screen.getByRole('button', { name: 'Найти проверенные прайсы' }));
    await screen.findByText(/Версия ver-one/);
    await screen.findByText(/Подготовленный прайс · Версия ver-two/);
    expect(sessionApi.discoverSupplierSources).toHaveBeenCalledWith(
      'org-test', ['supplier_one', 'supplier_two'], 'csrf-test',
    );
    expect(sessionApi.catalogVersions).not.toHaveBeenCalled();
    const selectors = screen.getAllByLabelText(/Проверенный прайс поставщика/);
    fireEvent.change(selectors[0]!, { target: { value: 'c'.repeat(64) + '|ver-one' } });
    fireEvent.change(selectors[1]!, { target: { value: 'd'.repeat(64) + '|ver-two' } });
    fireEvent.click(screen.getByRole('button', { name: 'Сравнить предложения' }));
    await waitFor(() => expect(previewCall).toHaveBeenCalledWith(
      'org-test', [
        { supplier_id: 'supplier_one', profile_fingerprint: 'c'.repeat(64),
          catalog_version_id: 'ver-one' },
        { supplier_id: 'supplier_two', profile_fingerprint: 'd'.repeat(64),
          catalog_version_id: 'ver-two' },
      ], lines, 'csrf-test', undefined,
    ));
    expect(await screen.findByRole('group', { name: 'Проверенные предложения поставщиков' })).toBeVisible();
    const saveButton = await screen.findByRole('button', {
      name: 'Создать КП с выбранными поставщиками',
    });
    expect(saveButton).toBeDisabled();
    const offerSelector = screen.getByLabelText('Поставщик и товар для позиции 1');
    fireEvent.change(offerSelector, { target: { value: '1' } });
    expect(saveButton).toBeEnabled();
    expect(screen.getByText(/Выбранный источник:/)).toHaveTextContent('supplier_one');
    fireEvent.change(offerSelector, { target: { value: '' } });
    expect(offerSelector).toHaveValue('');
    expect(saveButton).toBeDisabled();
    expect(screen.queryByText(/Выбранный источник:/)).not.toBeInTheDocument();
    fireEvent.click(saveButton);
    expect(save).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText('Поставщик и товар для позиции 1'), {
      target: { value: '0' },
    });
    expect(saveButton).toBeEnabled();
    fireEvent.click(saveButton);
    await waitFor(() => expect(save).toHaveBeenCalledWith(
      'org-test', 'active-catalog', expect.any(Array), lines,
      [{ line_number: 1, supplier_id: 'supplier_two',
        catalog_version_id: 'ver-two', sku: 'SKU-IDENTICAL' }],
      'csrf-test', expect.any(String), undefined,
    ));
    expect(p.onSaved).toHaveBeenCalledWith(expect.objectContaining({
      quote_id: 'quote-new',
    }));
  });

  it('does not offer to compare unverified digest or VAT-included supplier prices', async () => {
    fixture();
    vi.mocked(sessionApi.discoverSupplierSources).mockResolvedValue({
      suppliers: [
        { supplier_id: 'supplier_one', options: [], truncated: false,
          canonical_discovery_available: true },
        { supplier_id: 'supplier_two', options: [], truncated: false,
          canonical_discovery_available: true },
      ],
    });
    render(<SupplierQuoteComparison {...props()} />);
    const fields = screen.getAllByPlaceholderText('supplier_company_a');
    fireEvent.change(fields[0]!, { target: { value: 'supplier_one' } });
    fireEvent.change(fields[1]!, { target: { value: 'supplier_two' } });
    fireEvent.click(screen.getByRole('button', { name: 'Найти проверенные прайсы' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('нет проверенных');
    expect(screen.getByRole('button', { name: 'Сравнить предложения' })).toBeDisabled();
    expect(sessionApi.previewSupplierQuote).not.toHaveBeenCalled();
  });
});


