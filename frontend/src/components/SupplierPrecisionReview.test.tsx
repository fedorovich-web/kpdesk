import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { sessionApi } from '../tenant-session';
import type { SupplierMappingHistoryItem } from '../tenant-session';
import { SupplierPrecisionReview } from './SupplierPrecisionReview';

const original = new File(
  ['sku;name;price;currency;unit;pack\nA-1;Paper;10.125;RUB;pcs;1\n'],
  'supplier.csv', { type: 'text/csv' },
);
const sha = 'a'.repeat(64);
const rowHash = 'b'.repeat(64);
const preview = {
  source_sha256: sha, source_filename: 'supplier.csv', total_rows: 1,
  precision_cases: 1, offset: 0, next_offset: null,
  published: false as const, model_training_authorized: false as const,
  rows: [{
    line: 2, source_row_sha256: rowHash, supplier_sku: 'A-1', name: 'Paper',
    source_price_raw: '10.125', source_sale_unit: 'pcs', source_pack_size: 1,
    pack_size_review_reason: null,
    currency_confirmed_rub: true, formula_provenance_unverified: false,
    issues: ['price_precision_requires_review'],
  }],
};

afterEach(() => vi.restoreAllMocks());

const basicInspection = {
  source_sha256: sha, source_format: 'csv', row_count: 1,
  available_sheets: [], selected_sheet: null, encoding: 'utf-8-sig',
  delimiter: ';', items_key: null, header_row: 1,
  observed_columns: ['SKU', 'Name', 'Price', 'Currency', 'Unit', 'Pack'],
  sample_rows: [['A-1', 'Paper', '10.125', 'RUB', 'pcs', '1']],
  proposed_fields: { sku: 0, name: 1, price: 2, currency: 3 },
  options: { name: [1], price: [2] }, stage_mapping: null,
  issues: [], requires_confirmation: true as const,
  canonical_importable: false as const, model_training_authorized: false as const,
};

beforeEach(() => {
  vi.spyOn(sessionApi, 'inspectSupplierStructure').mockResolvedValue(basicInspection);
});

async function inspectAndConfirm() {
  fireEvent.click(screen.getByRole('button', { name: 'Определить столбцы' }));
  fireEvent.click(await screen.findByLabelText('Я проверил соответствие всех столбцов оригиналу'));
}


describe('protected supplier precision review UI', () => {
  it('explains ambiguous columns and unknown warnings without automatically confirming the mapping', async () => {
    vi.mocked(sessionApi.inspectSupplierStructure).mockResolvedValue({ ...basicInspection,
      issues: ['ambiguous_name_columns', 'manual_name_selection_required', 'future_source_issue'],
    });
    const stage = vi.spyOn(sessionApi, 'previewSupplierStage');
    render(<SupplierPrecisionReview organizationId="org-a" csrf="csrf-a"/>);
    fireEvent.change(screen.getByLabelText('Идентификатор поставщика'), { target: { value: 'vendor-a' } });
    fireEvent.change(screen.getByLabelText('Оригинал прайс-листа'), { target: { files: [original] } });
    fireEvent.click(screen.getByRole('button', { name: 'Определить столбцы' }));
    expect(await screen.findByText('Найдено несколько столбцов названия. Выберите нужный вручную.')).toBeVisible();
    expect(screen.getByRole('group', { name: 'Предложенное соответствие столбцов' })).toBeVisible();
    expect(screen.getByText('Нужно дополнительно проверить данные строки или настройки прайса.')).toBeVisible();
    expect(screen.getByText(/ambiguous_name_columns, manual_name_selection_required, future_source_issue/)).not.toBeVisible();
    expect(screen.getByLabelText('Я проверил соответствие всех столбцов оригиналу')).not.toBeChecked();
    expect(screen.getByRole('button', { name: 'Просмотреть все строки прайса' })).toBeDisabled();
    expect(stage).not.toHaveBeenCalled();
  });

  it('never records, rounds or publishes a price on file selection', async () => {
    const pre = vi.spyOn(sessionApi, 'previewSupplierPrecision').mockResolvedValue(preview);
    const attest = vi.spyOn(sessionApi, 'attestSupplierPrecision');
    vi.spyOn(sessionApi, 'supplierPrecisionHistory').mockResolvedValue({
      records: [], published: false, current_price_verified: false,
    });
    render(<SupplierPrecisionReview organizationId="org-a" csrf="csrf-a"/>);
    fireEvent.change(screen.getByLabelText('Идентификатор поставщика'), {
      target: { value: 'vendor-a' },
    });
    fireEvent.change(screen.getByLabelText('Оригинал прайс-листа'), {
      target: { files: [original] },
    });
    expect(attest).not.toHaveBeenCalled();
    await inspectAndConfirm();
    fireEvent.click(screen.getByRole('button', { name: 'Показать спорные цены' }));
    await waitFor(() => expect(pre).toHaveBeenCalledWith(
      'org-a', 'vendor-a', original,
      expect.objectContaining({
        name: 1, price: 2, sku: 0, currency: 3, header_row: 1,
      }),
      'csrf-a', 0, 50, false,
    ));
    expect((await screen.findAllByText('10.125')).length).toBeGreaterThanOrEqual(2);
    expect(attest).not.toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: 'Сохранить заключение' }))
      .not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /Строка 2/ }));
    expect(screen.getByRole('button', { name: 'Сохранить заключение' })).toBeDisabled();
  });

  it('requires manual rule, cents, rationale and attestation before saving', async () => {
    vi.spyOn(sessionApi, 'previewSupplierPrecision').mockResolvedValue(preview);
    vi.spyOn(sessionApi, 'supplierPrecisionHistory').mockResolvedValue({
      records: [], published: false, current_price_verified: false,
    });
    const attest = vi.spyOn(sessionApi, 'attestSupplierPrecision').mockResolvedValue({
      source_sha256: sha, source_row_sha256: rowHash, line: 2,
      recorded: true, published: false, current_price_verified: false,
    });
    render(<SupplierPrecisionReview organizationId="org-a" csrf="csrf-a"/>);
    fireEvent.change(screen.getByLabelText('Идентификатор поставщика'), {
      target: { value: 'vendor-a' },
    });
    fireEvent.change(screen.getByLabelText('Оригинал прайс-листа'), {
      target: { files: [original] },
    });
    await inspectAndConfirm();
    fireEvent.click(screen.getByRole('button', { name: 'Показать спорные цены' }));
    fireEvent.click(await screen.findByRole('button', { name: /Строка 2/ }));
    fireEvent.change(screen.getByLabelText('Решение по цене'), {
      target: { value: 'approve_rounding' },
    });
    fireEvent.change(screen.getByLabelText('Правило округления'), {
      target: { value: 'half_up' },
    });
    fireEvent.change(screen.getByLabelText('Подтверждённая цена, ₽'), {
      target: { value: '10.13' },
    });
    fireEvent.change(screen.getByLabelText('Обоснование решения'), {
      target: { value: 'Проверено условие поставщика об округлении до двух копеек.' },
    });
    const submit = screen.getByRole('button', { name: 'Сохранить заключение' });
    expect(submit).toBeDisabled();
    fireEvent.click(screen.getByLabelText('Я лично проверил исходную цену и правило'));
    fireEvent.click(submit);
    await waitFor(() => expect(attest).toHaveBeenCalledWith(
      'org-a', 'vendor-a', original,
      expect.objectContaining({ price: 2, header_row: 1 }),
      preview,
      expect.objectContaining({
        line: 2, source_row_sha256: rowHash, finding: 'approve_rounding',
        rounded_price_rub: '10.13', rounding_mode: 'half_up',
      }),
      'csrf-a', false,
    ));
    expect(await screen.findByText(/Заключение сохранено/)).toBeInTheDocument();
  });

  it('does not allow approval of unverified currency or formula provenance', async () => {
    vi.spyOn(sessionApi, 'previewSupplierPrecision').mockResolvedValue({
      ...preview, rows: [{
        ...preview.rows[0]!, currency_confirmed_rub: false,
        formula_provenance_unverified: true,
      }],
    });
    vi.spyOn(sessionApi, 'supplierPrecisionHistory').mockResolvedValue({
      records: [], published: false, current_price_verified: false,
    });
    render(<SupplierPrecisionReview organizationId="org-a" csrf="csrf-a"/>);
    fireEvent.change(screen.getByLabelText('Идентификатор поставщика'), {
      target: { value: 'vendor-a' },
    });
    fireEvent.change(screen.getByLabelText('Оригинал прайс-листа'), {
      target: { files: [original] },
    });
    await inspectAndConfirm();
    fireEvent.click(screen.getByRole('button', { name: 'Показать спорные цены' }));
    fireEvent.click(await screen.findByRole('button', { name: /Строка 2/ }));
    expect(screen.getByRole('option', { name: 'Подтвердить округление' }))
      .toBeDisabled();
    expect(screen.getAllByText(/Источник требует отдельной проверки/))
      .toHaveLength(2);
  });
});


describe('source switching and bounded pagination', () => {
  it('re-parses original source for next page and clears stale row state', async () => {
    const next = {
      ...preview, precision_cases: 51, offset: 50, next_offset: null,
      rows: [{ ...preview.rows[0]!, line: 52, source_row_sha256: 'c'.repeat(64) }],
    };
    const previewApi = vi.spyOn(sessionApi, 'previewSupplierPrecision')
      .mockResolvedValueOnce({ ...preview, precision_cases: 51, next_offset: 50 })
      .mockResolvedValueOnce(next);
    vi.spyOn(sessionApi, 'supplierPrecisionHistory').mockResolvedValue({
      records: [], published: false, current_price_verified: false,
    });
    render(<SupplierPrecisionReview organizationId="org-a" csrf="csrf-a"/>);
    fireEvent.change(screen.getByLabelText('Идентификатор поставщика'), {
      target: { value: 'vendor-a' },
    });
    fireEvent.change(screen.getByLabelText('Оригинал прайс-листа'), {
      target: { files: [original] },
    });
    await inspectAndConfirm();
    fireEvent.click(screen.getByRole('button', { name: 'Показать спорные цены' }));
    fireEvent.click(await screen.findByRole('button', { name: /Строка 2/ }));
    expect(screen.getByRole('button', { name: 'Сохранить заключение' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Следующие 50' }));
    await waitFor(() => expect(previewApi).toHaveBeenLastCalledWith(
      'org-a', 'vendor-a', original, expect.any(Object),
      'csrf-a', 50, 50, false,
    ));
    expect(await screen.findByRole('button', { name: /Строка 52/ })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Сохранить заключение' }))
      .not.toBeInTheDocument();
    const another = new File(['different supplier'], 'different.csv');
    fireEvent.change(screen.getByLabelText('Оригинал прайс-листа'), {
      target: { files: [another] },
    });
    expect(screen.queryByText('Спорных цен: 51 из 1 строк')).not.toBeInTheDocument();
  });

  it('never requests protected APIs without a CSRF token', () => {
    const api = vi.spyOn(sessionApi, 'previewSupplierPrecision');
    render(<SupplierPrecisionReview organizationId="org-a" csrf={null}/>);
    fireEvent.change(screen.getByLabelText('Идентификатор поставщика'), {
      target: { value: 'vendor-a' },
    });
    fireEvent.change(screen.getByLabelText('Оригинал прайс-листа'), {
      target: { files: [original] },
    });
    expect(screen.getByRole('button', { name: 'Показать спорные цены' })).toBeDisabled();
    expect(api).not.toHaveBeenCalled();
  });
});


describe('source-grounded field inference with explicit operator approval', () => {
  it('offers a source-derived mapping, but never applies it or previews without review', async () => {
    const inspect = vi.spyOn(sessionApi, 'inspectSupplierStructure').mockResolvedValue({
      source_sha256: sha, source_format: 'csv', row_count: 1,
      available_sheets: [], selected_sheet: null, encoding: 'utf-8-sig', delimiter: ';',
      items_key: null, header_row: 1,
      observed_columns: ['SKU', 'Name', 'Price', 'Currency', 'Unit', 'Pack'],
      sample_rows: [['A-1', 'Paper', '10.125', 'RUB', 'pcs', '1']],
      proposed_fields: { sku: 0, name: 1, price: 2, currency: 3 },
      options: { name: [1], price: [2] },
      stage_mapping: {
        sku: 0, name: 1, price: 2, currency: 3, header_row: 1,
        sale_unit: null, pieces_per_sale_unit: null, category: null,
        confirmed_currency: null, encoding: 'utf-8-sig', delimiter: ';', sheet: null,
      },
      issues: [], requires_confirmation: true, canonical_importable: false,
      model_training_authorized: false,
    });
    const pre = vi.spyOn(sessionApi, 'previewSupplierPrecision').mockResolvedValue(preview);
    render(<SupplierPrecisionReview organizationId="org-a" csrf="csrf-a"/>);
    fireEvent.change(screen.getByLabelText('Идентификатор поставщика'), {
      target: { value: 'vendor-a' },
    });
    fireEvent.change(screen.getByLabelText('Оригинал прайс-листа'), {
      target: { files: [original] },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Определить столбцы' }));
    await waitFor(() => expect(inspect).toHaveBeenCalledWith(
      'org-a', 'vendor-a', original, 'csrf-a', undefined, false, undefined,
    ));
    expect(await screen.findByText('SKU')).toBeInTheDocument();
    expect(pre).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Показать спорные цены' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Применить предложенные настройки столбцов' }));
    expect(screen.getByRole('button', { name: 'Показать спорные цены' })).toBeDisabled();
    fireEvent.click(screen.getByLabelText('Я проверил соответствие всех столбцов оригиналу'));
    fireEvent.click(screen.getByRole('button', { name: 'Показать спорные цены' }));
    await waitFor(() => expect(pre).toHaveBeenCalledWith(
      'org-a', 'vendor-a', original,
      expect.objectContaining({ name: 1, price: 2, sku: 0, currency: 3 }),
      'csrf-a', 0, 50, false,
    ));
  });

  it('does not invent a mapping for ambiguous price headers', async () => {
    vi.spyOn(sessionApi, 'inspectSupplierStructure').mockResolvedValue({
      source_sha256: sha, source_format: 'csv', row_count: 1,
      available_sheets: [], selected_sheet: null, encoding: 'utf-8-sig', delimiter: ';',
      items_key: null, header_row: 1,
      observed_columns: ['Name', 'Цена опт', 'Цена розница'],
      sample_rows: [['Paper', '10.125', '20.456']],
      proposed_fields: { name: 0 },
      options: { name: [0], price: [1, 2] },
      stage_mapping: null, issues: ['ambiguous_price_columns', 'manual_price_selection_required'],
      requires_confirmation: true, canonical_importable: false,
      model_training_authorized: false,
    });
    render(<SupplierPrecisionReview organizationId="org-a" csrf="csrf-a"/>);
    fireEvent.change(screen.getByLabelText('Идентификатор поставщика'), {
      target: { value: 'vendor-a' },
    });
    fireEvent.change(screen.getByLabelText('Оригинал прайс-листа'), {
      target: { files: [original] },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Определить столбцы' }));
    expect(await screen.findByText('Найдено несколько столбцов цены. Выберите нужный вручную.')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Применить предложенные настройки столбцов' }))
      .not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Показать спорные цены' })).toBeDisabled();
  });
});


describe('multi-sheet source selection', () => {
  it('requires a named Excel sheet and re-inspects it before any mapping', async () => {
    const inspector = vi.spyOn(sessionApi, 'inspectSupplierStructure')
      .mockResolvedValueOnce({
        ...basicInspection, source_format: 'xlsx', observed_columns: [],
        sample_rows: [], stage_mapping: null, selected_sheet: null,
        available_sheets: ['Office', 'Warehouse'],
        issues: ['sheet_selection_required'],
      })
      .mockResolvedValueOnce({
        ...basicInspection, source_format: 'xlsx',
        selected_sheet: 'Warehouse', available_sheets: ['Office', 'Warehouse'],
        stage_mapping: {
          sku: 0, name: 1, price: 2, currency: 3, header_row: 1,
          sale_unit: null, pieces_per_sale_unit: null, category: null,
          confirmed_currency: null, sheet: 'Warehouse', encoding: 'utf-8-sig',
          delimiter: ';',
        },
      });
    render(<SupplierPrecisionReview organizationId="org-a" csrf="csrf-a"/>);
    const book = new File(['fake is fine: network is mocked'], 'supplier.xlsx');
    fireEvent.change(screen.getByLabelText('Идентификатор поставщика'), {
      target: { value: 'vendor-a' },
    });
    fireEvent.change(screen.getByLabelText('Оригинал прайс-листа'), {
      target: { files: [book] },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Определить столбцы' }));
    expect(await screen.findByLabelText('Выберите лист из файла')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Показать спорные цены' })).toBeDisabled();
    fireEvent.change(screen.getByLabelText('Выберите лист из файла'), {
      target: { value: 'Warehouse' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Определить столбцы' }));
    await waitFor(() => expect(inspector).toHaveBeenLastCalledWith(
      'org-a', 'vendor-a', book, 'csrf-a', 'Warehouse', false, undefined,
    ));
    expect(await screen.findByRole('button', { name: 'Применить предложенные настройки столбцов' }))
      .toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Показать спорные цены' })).toBeDisabled();
  });
});


describe('verified supplier mapping profile persistence', () => {
  it('finds previously reviewed headers but never applies a saved map silently', async () => {
    const match = vi.spyOn(sessionApi, 'matchSupplierMapping').mockResolvedValue({
      found: true, source_sha256: sha, profile_sha256: 'c'.repeat(64),
      fingerprint: 'd'.repeat(64), approved_by: 'reviewer-id',
      price_basis: 'standard', columns: {
        sku: 0, name: 1, price: 2, currency: 3,
      }, header_row: 1, sheet: null, reviewed: true,
      canonical_importable: false, model_training_authorized: false,
    });
    const previewSpy = vi.spyOn(sessionApi, 'previewSupplierPrecision');
    render(<SupplierPrecisionReview organizationId="org-a" csrf="csrf-a"/>);
    fireEvent.change(screen.getByLabelText('Идентификатор поставщика'), {
      target: { value: 'vendor-a' },
    });
    fireEvent.change(screen.getByLabelText('Оригинал прайс-листа'), {
      target: { files: [original] },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Определить столбцы' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Найти настройки столбцов' }));
    await waitFor(() => expect(match).toHaveBeenCalledWith(
      'org-a', 'vendor-a', original, 'csrf-a', undefined,
    ));
    expect(await screen.findByRole('button', {
      name: 'Применить сохранённые настройки столбцов',
    })).toBeInTheDocument();
    expect(previewSpy).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Показать спорные цены' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Применить сохранённые настройки столбцов' }));
    expect(screen.getByRole('button', { name: 'Показать спорные цены' })).toBeDisabled();
    fireEvent.click(screen.getByLabelText('Я проверил соответствие всех столбцов оригиналу'));
    expect(screen.getByRole('button', { name: 'Показать спорные цены' })).toBeEnabled();
  });

  it('requires personal field confirmation and evidence before PostgreSQL save', async () => {
    const save = vi.spyOn(sessionApi, 'saveSupplierMapping').mockResolvedValue({
      found: true, source_sha256: sha, profile_sha256: 'e'.repeat(64),
      fingerprint: 'f'.repeat(64), approved_by: 'reviewer-id',
      price_basis: 'standard', columns: { name: 1, price: 2 },
      header_row: 1, sheet: null, reviewed: true,
      canonical_importable: false, model_training_authorized: false,
    });
    render(<SupplierPrecisionReview organizationId="org-a" csrf="csrf-a"/>);
    fireEvent.change(screen.getByLabelText('Идентификатор поставщика'), {
      target: { value: 'vendor-a' },
    });
    fireEvent.change(screen.getByLabelText('Оригинал прайс-листа'), {
      target: { files: [original] },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Определить столбцы' }));
    expect(save).not.toHaveBeenCalled();
    fireEvent.click(await screen.findByLabelText(
      'Я проверил соответствие всех столбцов оригиналу',
    ));
    const button = screen.getByRole('button', {
      name: 'Сохранить соответствие столбцов',
    });
    expect(button).toBeDisabled();
    fireEvent.change(screen.getByLabelText('Что проверено при сопоставлении столбцов'), {
      target: { value: 'Сверены заголовки и выбран правильный ценовой столбец.' },
    });
    fireEvent.click(button);
    await waitFor(() => expect(save).toHaveBeenCalledWith(
      'org-a', 'vendor-a', original, 'csrf-a',
      expect.objectContaining({ source_sha256: sha }),
      expect.objectContaining({ price: 2, name: 1 }),
      'standard', 'Сверены заголовки и выбран правильный ценовой столбец.',
    ));
    expect(await screen.findByText(/Соответствие столбцов сохранено/)).toBeInTheDocument();
  });
});


describe('A26 immutable supplier mapping history', () => {
  it('compares source-grounded versions without applying maps or creating ML labels', async () => {
    const template = {
      fingerprint: 'f'.repeat(64), source_format: 'csv',
      header_row: 1, sheet: null, delimiter: ';', encoding: 'utf-8-sig',
      items_key: null, confirmed_currency: null, price_basis: 'standard' as const,
      approved_by: '20000000-0000-4000-8000-000000000001',
      approved_source_sha256: sha, reviewed_at: '2026-10-22T09:00:00Z',
    };
    const records: SupplierMappingHistoryItem[] = [
      { ...template, profile_sha256: 'c'.repeat(64),
        columns: { name: 1, price: 2, sku: 0 },
        review_evidence: 'Исходные заголовки и артикул сверены вручную' },
      { ...template, profile_sha256: 'd'.repeat(64),
        columns: { name: 1, price: 2 },
        review_evidence: 'Отдельная проверка заголовков без артикула' },
    ];
    const history = vi.spyOn(sessionApi, 'supplierMappingHistory').mockResolvedValue({
      items: records, next_offset: null,
      canonical_importable: false, model_training_authorized: false,
    });
    const previewApi = vi.spyOn(sessionApi, 'previewSupplierPrecision');
    const saveApi = vi.spyOn(sessionApi, 'saveSupplierMapping');
    render(<SupplierPrecisionReview organizationId="org-a" csrf="csrf-a"/>);
    fireEvent.change(screen.getByLabelText('Идентификатор поставщика'), {
      target: { value: 'vendor-a' },
    });
    fireEvent.change(screen.getByLabelText('Оригинал прайс-листа'), {
      target: { files: [original] },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Определить столбцы' }));
    fireEvent.click(await screen.findByRole('button', { name: 'История настроек столбцов' }));
    await waitFor(() => expect(history).toHaveBeenCalledWith('org-a', 'vendor-a', 0));
    expect(await screen.findByText(/Исходные заголовки и артикул сверены вручную/))
      .toBeInTheDocument();
    expect(screen.getByRole('group', { name: 'История настроек столбцов' })).toBeVisible();
    expect(screen.getByRole('heading', { name: 'Сравнение двух версий' }))
      .toBeInTheDocument();
    expect(screen.getByRole('row', { name: /columns/ })).toBeInTheDocument();
    expect(screen.getByText(/не цены, остатки/)).toBeInTheDocument();
    expect(previewApi).not.toHaveBeenCalled();
    expect(saveApi).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Показать спорные цены' }))
      .toBeDisabled();
    fireEvent.change(screen.getByLabelText('Оригинал прайс-листа'), {
      target: { files: [new File(['different source'], 'other.csv')] },
    });
    expect(screen.queryByRole('heading', { name: 'Сравнение двух версий' }))
      .not.toBeInTheDocument();
  });
});


describe('supplier package review diagnosis', () => {
  it('shows source-grounded package issue separately from price rounding review', async () => {
    vi.spyOn(sessionApi, 'previewSupplierPrecision').mockResolvedValue({
      ...preview,
      rows: [{
        ...preview.rows[0]!,
        source_pack_size: null,
        pack_size_review_reason: 'Столбец размера упаковки выбран, но значение в строке пустое.',
        issues: ['price_precision_requires_review', 'missing_pack_size'],
      }],
    });
    vi.spyOn(sessionApi, 'supplierPrecisionHistory').mockResolvedValue({
      records: [], published: false, current_price_verified: false,
    });
    const attest = vi.spyOn(sessionApi, 'attestSupplierPrecision');
    render(<SupplierPrecisionReview organizationId="org-a" csrf="csrf-a"/>);
    fireEvent.change(screen.getByLabelText('Идентификатор поставщика'), {
      target: { value: 'vendor-a' },
    });
    fireEvent.change(screen.getByLabelText('Оригинал прайс-листа'), {
      target: { files: [original] },
    });
    await inspectAndConfirm();
    fireEvent.click(screen.getByRole('button', { name: 'Показать спорные цены' }));
    expect(await screen.findByText(/Столбец размера упаковки выбран, но значение в строке пустое/))
      .toBeInTheDocument();
    expect(screen.getByText(/Проверка округления не подтверждает фасовку/))
      .toBeInTheDocument();
    expect(attest).not.toHaveBeenCalled();
  });
});


describe('all supplier rows stay source-bound and read-only', () => {
  const staging = {
    source_sha256: sha, total_rows: 51, priced_rub_rows: 51, unresolved_rows: 2,
    pack_size_issue_counts: {
      pack_size_requires_review: 0, missing_pack_size: 1, invalid_pack_size: 1,
    },
    offset: 0, next_offset: 50,
    sampled_rows: [{
      line: 2, source_row_sha256: rowHash,
      supplier_sku: 'A-1', name: 'Paper', price_rub: '10.12',
      source_category: null, source_sale_unit: 'pcs', source_pack_size: null,
      issues: ['missing_pack_size'],
      pack_size_review_reason: 'Столбец упаковки выбран, но ячейка пустая.',
    }],
    requires_confirmation: true as const,
    canonical_importable: false as const,
    model_training_authorized: false as const,
  };

  it('previews mapped supplier lines and diagnosis without publishing or recording', async () => {
    const stage = vi.spyOn(sessionApi, 'previewSupplierStage').mockResolvedValue(staging);
    const attest = vi.spyOn(sessionApi, 'attestSupplierPrecision');
    const save = vi.spyOn(sessionApi, 'saveSupplierMapping');
    render(<SupplierPrecisionReview organizationId="org-a" csrf="csrf-a"/>);
    fireEvent.change(screen.getByLabelText('Идентификатор поставщика'), {
      target: { value: 'vendor-a' },
    });
    fireEvent.change(screen.getByLabelText('Оригинал прайс-листа'), {
      target: { files: [original] },
    });
    expect(stage).not.toHaveBeenCalled();
    await inspectAndConfirm();
    fireEvent.click(screen.getByRole('button', { name: 'Просмотреть все строки прайса' }));
    await waitFor(() => expect(stage).toHaveBeenCalledWith(
      'org-a', 'vendor-a', original,
      expect.objectContaining({ source_sha256: sha }),
      'csrf-a', 0, 50, false,
    ));
    expect(await screen.findByText('Всего строк: 51')).toBeInTheDocument();
    expect(screen.getByText('Столбец упаковки выбран, но ячейка пустая.'))
      .toBeInTheDocument();
    expect(screen.getByText(/ячейка пустая — 1/)).not.toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'Проверить единицу цены' }));
    expect(screen.getByRole('button', { name: 'Сохранить проверку единицы цены' }))
      .toBeDisabled();
    expect(screen.getByText(/Подтверждение означает личную проверку единицы/))
      .toBeInTheDocument();
    expect(attest).not.toHaveBeenCalled();
    expect(save).not.toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: 'Сохранить заключение' }))
      .not.toBeInTheDocument();
  });

  it('reads the next page from the same file and clears stale rows on file change', async () => {
    const stage = vi.spyOn(sessionApi, 'previewSupplierStage')
      .mockResolvedValueOnce(staging)
      .mockResolvedValueOnce({
        ...staging, offset: 50, next_offset: null,
        sampled_rows: [{
          ...staging.sampled_rows[0]!, line: 52, source_row_sha256: 'c'.repeat(64),
          supplier_sku: 'B-52', issues: ['invalid_pack_size'],
          pack_size_review_reason: 'Некорректный размер упаковки.',
        }],
      });
    render(<SupplierPrecisionReview organizationId="org-a" csrf="csrf-a"/>);
    fireEvent.change(screen.getByLabelText('Идентификатор поставщика'), {
      target: { value: 'vendor-a' },
    });
    fireEvent.change(screen.getByLabelText('Оригинал прайс-листа'), {
      target: { files: [original] },
    });
    await inspectAndConfirm();
    fireEvent.click(screen.getByRole('button', { name: 'Просмотреть все строки прайса' }));
    expect(await screen.findByText('Всего строк: 51')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Следующие строки' }));
    await waitFor(() => expect(stage).toHaveBeenLastCalledWith(
      'org-a', 'vendor-a', original, expect.objectContaining({ source_sha256: sha }),
      'csrf-a', 50, 50, false,
    ));
    expect(await screen.findByText('Некорректный размер упаковки.'))
      .toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Следующие строки' })).toBeDisabled();
    fireEvent.change(screen.getByLabelText('Оригинал прайс-листа'), {
      target: { files: [new File(['changed'], 'changed.csv')] },
    });
    expect(screen.queryByText('Всего строк: 51')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Просмотреть все строки прайса' }))
      .toBeDisabled();
  });

  it('cannot request all supplier rows without CSRF', () => {
    const stage = vi.spyOn(sessionApi, 'previewSupplierStage');
    render(<SupplierPrecisionReview organizationId="org-a" csrf={null}/>);
    fireEvent.change(screen.getByLabelText('Идентификатор поставщика'), {
      target: { value: 'vendor-a' },
    });
    fireEvent.change(screen.getByLabelText('Оригинал прайс-листа'), {
      target: { files: [original] },
    });
    expect(screen.getByRole('button', { name: 'Просмотреть все строки прайса' }))
      .toBeDisabled();
    expect(stage).not.toHaveBeenCalled();
  });
});


