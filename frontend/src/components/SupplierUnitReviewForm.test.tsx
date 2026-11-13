import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { sessionApi } from '../tenant-session';
import { SupplierUnitReviewForm } from './SupplierUnitReviewForm';

const sourceSha = 'a'.repeat(64);
const rowSha = 'b'.repeat(64);
const file = new File(['source'], 'supplier.csv', { type: 'text/csv' });
const mapping = {
  name: 1, price: 2, sku: 0, currency: 3, sale_unit: 4,
  pieces_per_sale_unit: 5, header_row: 1, source_sha256: sourceSha,
};
const row = {
  line: 2, source_row_sha256: rowSha, supplier_sku: 'K-1',
  name: 'Vendor item', price_rub: '250.00', source_category: null,
  source_sale_unit: 'pcs', source_pack_size: null,
  issues: ['missing_pack_size'],
  pack_size_review_reason: 'Столбец упаковки сопоставлен, но значение отсутствует.',
};
const evidence = 'Лично проверены оригинальные условия поставщика о продаже за одну штуку.';

const props = {
  organizationId: 'org-a', supplierId: 'vendor-a', csrf: 'csrf-a',
  file, sourceSha, mapping, quarantine: false, row,
};

afterEach(() => vi.restoreAllMocks());

describe('source-bound unit review form', () => {
  it('never attests on row selection and requires explicit reason and human confirmation', async () => {
    const attest = vi.spyOn(sessionApi, 'attestSupplierUnit').mockResolvedValue({
      recorded: true, source_sha256: sourceSha, source_row_sha256: rowSha,
      line: 2, published: false, model_training_authorized: false,
    });
    render(<SupplierUnitReviewForm {...props}/>);
    expect(attest).not.toHaveBeenCalled();
    const save = screen.getByRole('button', { name: 'Сохранить проверку единицы цены' });
    expect(save).toBeDisabled();
    fireEvent.change(screen.getByLabelText('Заключение по фасовке'), {
      target: { value: 'confirm_price_unit' },
    });
    fireEvent.change(screen.getByLabelText('Штук в единице продажи'), {
      target: { value: '0' },
    });
    fireEvent.change(screen.getByLabelText('Что подтверждено по документу поставщика'), {
      target: { value: evidence },
    });
    fireEvent.click(screen.getByLabelText(/Я лично сверил оригинал/));
    expect(save).toBeDisabled();
    fireEvent.change(screen.getByLabelText('Штук в единице продажи'), {
      target: { value: '1' },
    });
    expect(save).toBeDisabled();
    fireEvent.click(screen.getByLabelText(/Я лично сверил оригинал/));
    fireEvent.click(save);
    await waitFor(() => expect(attest).toHaveBeenCalledWith(
      'org-a', 'vendor-a', file, mapping, sourceSha,
      expect.objectContaining({
        line: 2, source_row_sha256: rowSha, finding: 'confirm_price_unit',
        approved_sale_unit: 'штука', approved_pack_size: 1, rationale: evidence,
      }),
      'csrf-a', false,
    ));
    expect(await screen.findByText(/Заключение сохранено/)).toBeInTheDocument();
  });

  it('rejects commercial confirmation for unverified currency and source formulas', () => {
    const attest = vi.spyOn(sessionApi, 'attestSupplierUnit');
    render(<SupplierUnitReviewForm {...props} row={{
      ...row, price_rub: null, issues: ['xlsx_formula_cell_quarantined'],
    }}/>);
    expect(screen.getByRole('option', {
      name: 'Подтвердить единицу цены после проверки',
    })).toBeDisabled();
    expect(attest).not.toHaveBeenCalled();
  });

  it('history is read separately, not confused with current source approval', async () => {
    const get = vi.spyOn(sessionApi, 'supplierUnitHistory').mockResolvedValue({
      records: [], next_offset: null,
      published: false, model_training_authorized: false,
    });
    render(<SupplierUnitReviewForm {...props}/>);
    fireEvent.click(screen.getByRole('button', { name: 'История подтверждений этого файла' }));
    await waitFor(() => expect(get).toHaveBeenCalledWith(
      'org-a', 'vendor-a', sourceSha, 0,
    ));
    expect(screen.getByText(/все строки выбранного файла/)).toBeInTheDocument();
  });
});
