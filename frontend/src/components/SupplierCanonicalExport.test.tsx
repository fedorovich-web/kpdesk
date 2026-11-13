import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { SupplierCanonicalExport } from './SupplierCanonicalExport';
import { sessionApi } from '../tenant-session';
import type { SupplierStagePreview } from '../tenant-session';

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

const sha = 'a'.repeat(64);
const stage: SupplierStagePreview = {
  source_sha256: sha,
  total_rows: 2, priced_rub_rows: 1, unresolved_rows: 1,
  offset: 0, next_offset: null, pack_size_issue_counts: {},
  requires_confirmation: true, canonical_importable: false,
  model_training_authorized: false,
  sampled_rows: [
    {
      line: 2, source_row_sha256: 'b'.repeat(64), supplier_sku: 'K-001',
      name: 'Keyboard', price_rub: '1500.90', source_category: null,
      source_sale_unit: 'pcs', source_pack_size: 1, issues: [],
      pack_size_review_reason: null,
    },
    {
      line: 3, source_row_sha256: 'c'.repeat(64), supplier_sku: 'Section',
      name: '', price_rub: null, source_category: null,
      source_sale_unit: null, source_pack_size: null,
      issues: ['missing_mapped_columns', 'missing_name', 'invalid_or_missing_price'],
      pack_size_review_reason: null,
    },
  ],
};

describe('A68 reviewed supplier conversion', () => {
  it('requires decisions for every source row and separate price-basis confirmation', async () => {
    const uploaded = new File(['source'], 'supplier.csv', { type: 'text/csv' });
    const canonical = new File(['catalog'], 'reviewed-supplier.csv', { type: 'text/csv' });
    const submit = vi.spyOn(sessionApi, 'exportReviewedSupplier').mockResolvedValue(canonical);
    const ready = vi.fn();
    render(<SupplierCanonicalExport
      organizationId="org-a" supplierId="supplier-1" csrf="csrf"
      source={uploaded} mapping={{ header_row: 1, sku: 0, name: 1, price: 2 }}
      stage={stage} priceBasis="standard" quarantine={false}
      busy={false} onCanonicalReady={ready}
    />);
    expect(screen.getByRole('group', { name: 'Преобразование проверенных строк' })).toBeVisible();
    expect(screen.getByRole('group', { name: 'Проверка группы товаров' })).toBeVisible();
    const prepare = screen.getByRole('button', {
      name: 'Подготовить CSV для загрузки',
    });
    expect(prepare).toBeDisabled();
    expect(submit).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: /Строка 3:/ }));
    fireEvent.click(screen.getByLabelText('Исключить эту строку из каталога'));
    fireEvent.change(screen.getByLabelText('Причина исключения'), {
      target: { value: 'Supplier section title, not a product.' },
    });
    fireEvent.click(screen.getByLabelText(
      'Я лично проверил эту строку оригинала и подтверждаю решение',
    ));
    fireEvent.click(screen.getByRole('button', { name: 'Сохранить решение по строке' }));
    expect(prepare).toBeDisabled();

    fireEvent.click(screen.getByRole('button', { name: /Строка 2:/ }));
    fireEvent.change(screen.getByLabelText('Категория товара'), {
      target: { value: 'keyboard' },
    });
    fireEvent.change(screen.getByLabelText('Единица продажи'), {
      target: { value: 'штука' },
    });
    fireEvent.change(screen.getByLabelText('Подключение'), {
      target: { value: 'wired' },
    });
    fireEvent.change(screen.getByLabelText('Клавиш'), {
      target: { value: '104' },
    });
    fireEvent.click(screen.getByLabelText(
      'Я лично проверил эту строку оригинала и подтверждаю решение',
    ));
    fireEvent.click(screen.getByRole('button', { name: 'Сохранить решение по строке' }));
    expect(prepare).toBeDisabled();
    fireEvent.click(screen.getByLabelText(/Я проверил тип цены/));
    expect(prepare).toBeEnabled();
    fireEvent.click(prepare);
    await waitFor(() => expect(ready).toHaveBeenCalledWith(canonical));
    expect(submit).toHaveBeenCalledWith('org-a', 'supplier-1', uploaded, 'csrf',
      expect.objectContaining({
        source_sha256: sha,
        decisions: [
          expect.objectContaining({
            action: 'include', confirmed: true, line: 2,
            category: 'keyboard', attributes: { connection: 'wired', keys: 104 },
          }),
          expect.objectContaining({
            action: 'exclude', confirmed: true, line: 3,
            reason: 'Supplier section title, not a product.',
          }),
        ],
      }),
    );
  });
});

describe('A68 stale export isolation', () => {
  it('does not attach a completed export after source review is unmounted', async () => {
    const uploaded = new File(['old'], 'old.csv');
    let resolve!: (file: File) => void;
    vi.spyOn(sessionApi, 'exportReviewedSupplier')
      .mockImplementation(() => new Promise<File>(done => { resolve = done; }));
    const ready = vi.fn();
    const oneRow: SupplierStagePreview = {
      ...stage, total_rows: 1, sampled_rows: [stage.sampled_rows[0]!],
    };
    const view = render(<SupplierCanonicalExport
      organizationId="org-a" supplierId="supplier-1" csrf="csrf"
      source={uploaded} mapping={{ header_row: 1, sku: 0, name: 1, price: 2 }}
      stage={oneRow} priceBasis="standard" quarantine={false}
      busy={false} onCanonicalReady={ready}
    />);
    fireEvent.click(screen.getByRole('button', { name: /Строка 2:/ }));
    fireEvent.change(screen.getByLabelText('Категория товара'), {
      target: { value: 'keyboard' },
    });
    fireEvent.change(screen.getByLabelText('Единица продажи'), {
      target: { value: 'штука' },
    });
    fireEvent.change(screen.getByLabelText('Подключение'), {
      target: { value: 'wired' },
    });
    fireEvent.change(screen.getByLabelText('Клавиш'), {
      target: { value: '104' },
    });
    fireEvent.click(screen.getByLabelText(
      'Я лично проверил эту строку оригинала и подтверждаю решение',
    ));
    fireEvent.click(screen.getByRole('button', { name: 'Сохранить решение по строке' }));
    fireEvent.click(screen.getByLabelText(/Я проверил тип цены/));
    fireEvent.click(screen.getByRole('button', {
      name: 'Подготовить CSV для загрузки',
    }));
    view.unmount();
    resolve(new File(['new'], 'reviewed-supplier.csv'));
    await Promise.resolve();
    expect(ready).not.toHaveBeenCalled();
  });
});

describe('A70 bounded batch UI', () => {
  const first = {
    ...stage.sampled_rows[0]!, source_category: 'Keyboards',
  };
  const second = {
    ...first, line: 4, supplier_sku: 'K-002',
    name: 'Keyboard 2', price_rub: '1100.00',
    source_row_sha256: 'd'.repeat(64),
  };
  const reviewedStage: SupplierStagePreview = {
    ...stage, total_rows: 3, priced_rub_rows: 2,
    sampled_rows: [first, second, stage.sampled_rows[1]!],
  };
  const props = {
    organizationId: 'org-a', supplierId: 'supplier-1', csrf: 'csrf',
    source: new File(['raw'], 'vendor.csv'),
    mapping: { header_row: 1, sku: 0, name: 1, price: 2 },
    stage: reviewedStage, priceBasis: 'standard' as const,
    quarantine: false, busy: false,
  };

  it('requires individual row selection and explicit shared attributes; unresolved rows remain separate', async () => {
    const exportCsv = vi.spyOn(sessionApi, 'exportReviewedSupplier').mockResolvedValue(
      new File(['ok'], 'reviewed-supplier.csv'),
    );
    const ready = vi.fn();
    render(<SupplierCanonicalExport {...props} onCanonicalReady={ready}/>);
    const publish = screen.getByRole('button', {
      name: 'Подготовить CSV для загрузки',
    });
    expect(publish).toBeDisabled();
    expect(screen.queryByLabelText('Проверил строку 3 для группового подтверждения')).toBeNull();
    fireEvent.click(screen.getByLabelText('Проверил строку 2 для группового подтверждения'));
    fireEvent.click(screen.getByLabelText('Проверил строку 4 для группового подтверждения'));
    const apply = screen.getByRole('button', {
      name: 'Применить проверенные параметры к 2 строкам',
    });
    expect(apply).toBeDisabled();
    fireEvent.change(screen.getByLabelText('Подтверждённая категория всех выбранных товаров'), {
      target: { value: 'keyboard' },
    });
    fireEvent.change(screen.getByLabelText('Общая характеристика: Подключение'), {
      target: { value: 'wired' },
    });
    fireEvent.change(screen.getByLabelText('Общая характеристика: Клавиш'), {
      target: { value: '104' },
    });
    fireEvent.click(screen.getByLabelText(/Я сверил каждую отмеченную строку оригинала/));
    fireEvent.click(apply);
    expect(screen.getByText(/Проверено 2 из 3/)).toBeInTheDocument();
    expect(publish).toBeDisabled();
    expect(screen.queryByLabelText('Проверил строку 2 для группового подтверждения')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: /Строка 3:/ }));
    fireEvent.click(screen.getByLabelText('Исключить эту строку из каталога'));
    fireEvent.change(screen.getByLabelText('Причина исключения'), {
      target: { value: 'Supplier grouping header without a sellable product.' },
    });
    fireEvent.click(screen.getByLabelText(
      'Я лично проверил эту строку оригинала и подтверждаю решение',
    ));
    fireEvent.click(screen.getByRole('button', { name: 'Сохранить решение по строке' }));
    expect(publish).toBeDisabled();
    fireEvent.click(screen.getByLabelText(/Я проверил тип цены/));
    fireEvent.click(publish);
    await waitFor(() => expect(ready).toHaveBeenCalledTimes(1));
    expect(exportCsv).toHaveBeenCalledWith(
      'org-a', 'supplier-1', props.source, 'csrf',
      expect.objectContaining({
        decisions: [
          expect.objectContaining({ line: 2, action: 'include', confirmed: true,
            category: 'keyboard', attributes: { connection: 'wired', keys: 104 } }),
          expect.objectContaining({ line: 3, action: 'exclude', confirmed: true }),
          expect.objectContaining({ line: 4, action: 'include', confirmed: true,
            category: 'keyboard', attributes: { connection: 'wired', keys: 104 } }),
        ],
      }),
    );
  });

  it('blocks mixing different source categories and clears batch selection on page changes', () => {
    const different = {
      ...second, source_category: 'Other category',
    };
    const page = {
      ...reviewedStage, sampled_rows: [first, different],
    };
    const ready = vi.fn();
    const view = render(<SupplierCanonicalExport {...props} stage={page}
      onCanonicalReady={ready}/>);
    fireEvent.click(screen.getByLabelText('Проверил строку 2 для группового подтверждения'));
    fireEvent.click(screen.getByLabelText('Проверил строку 4 для группового подтверждения'));
    expect(screen.getByText(/разные исходные категории/)).toBeInTheDocument();
    expect(screen.queryByLabelText('Подтверждённая категория всех выбранных товаров')).toBeNull();
    view.rerender(<SupplierCanonicalExport {...props}
      stage={{ ...page, offset: 20, sampled_rows: [different] }}
      onCanonicalReady={ready}/>);
    expect(screen.getByText(/Проверенные строки текущей страницы: 0/)).toBeInTheDocument();
    expect(ready).not.toHaveBeenCalled();
  });
});

describe('A88 isolated checkpoint publication control', () => {
  it('is absent from normal supplier review, even after all decisions', () => {
    render(<SupplierCanonicalExport
      organizationId="org-a" supplierId="supplier-1" csrf="csrf"
      source={new File(['source'], 'supplier.csv')}
      mapping={{ header_row: 1, sku: 0, name: 1, price: 2 }}
      stage={{ ...stage, total_rows: 1, sampled_rows: [stage.sampled_rows[0]!] }}
      priceBasis="standard" quarantine={false} busy={false}
      onCanonicalReady={vi.fn()}
    />);
    expect(screen.queryByRole('button', {
      name: 'Подтвердить и опубликовать (тестовый стенд)',
    })).not.toBeInTheDocument();
  });

  it('requires manual review, retries a failed publish with the same keys', async () => {
    const ids = [
      '11111111-1111-4111-8111-111111111111',
      '22222222-2222-4222-8222-222222222222',
    ];
    vi.stubGlobal('crypto', {
      randomUUID: vi.fn().mockReturnValueOnce(ids[0]).mockReturnValueOnce(ids[1]),
      subtle: { digest: vi.fn().mockResolvedValue(new Uint8Array(32).fill(0xbb).buffer) },
    });
    const saveCheckpoint = vi.spyOn(sessionApi, 'saveReviewedSupplierCheckpoint')
      .mockResolvedValue({
        checkpoint_id: ids[0]!, source_sha256: sha,
        mapping_sha256: 'd'.repeat(64), taxonomy_version: 'v1',
        recorded: true, published: false,
      });
    const canonical = new File(['catalog'], 'reviewed.csv');
    Object.defineProperty(canonical, 'arrayBuffer', {
      value: () => Promise.resolve(new Uint8Array([1, 2, 3]).buffer),
    });
    vi.spyOn(sessionApi, 'exportReviewedSupplier').mockResolvedValue(canonical);
    const publish = vi.spyOn(sessionApi, 'publishReviewedSupplierCheckpoint')
      .mockRejectedValueOnce(new Error('Connection dropped after commit'))
      .mockResolvedValue({
        version_id: ids[1]!, source_sha256: 'b'.repeat(64), rows_count: 1,
      });
    const onPublished = vi.fn();
    render(<SupplierCanonicalExport
      organizationId="org-a" supplierId="supplier-1" csrf="csrf"
      source={new File(['source'], 'supplier.csv')}
      mapping={{ header_row: 1, sku: 0, name: 1, price: 2 }}
      stage={{ ...stage, total_rows: 1, sampled_rows: [stage.sampled_rows[0]!] }}
      priceBasis="standard" quarantine={false} busy={false}
      onCanonicalReady={vi.fn()}
      enableIsolatedCheckpointPublication onCheckpointPublished={onPublished}
    />);
    const button = screen.getByRole('button', {
      name: 'Подтвердить и опубликовать (тестовый стенд)',
    });
    expect(button).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: /Строка 2:/ }));
    fireEvent.change(screen.getByLabelText('Категория товара'), {
      target: { value: 'keyboard' },
    });
    fireEvent.change(screen.getByLabelText('Единица продажи'), {
      target: { value: 'штука' },
    });
    fireEvent.change(screen.getByLabelText('Подключение'), {
      target: { value: 'wired' },
    });
    fireEvent.change(screen.getByLabelText('Клавиш'), {
      target: { value: '104' },
    });
    fireEvent.click(screen.getByLabelText(
      'Я лично проверил эту строку оригинала и подтверждаю решение',
    ));
    fireEvent.click(screen.getByRole('button', { name: 'Сохранить решение по строке' }));
    expect(button).toBeDisabled();
    fireEvent.click(screen.getByLabelText(/Я проверил тип цены/));
    fireEvent.click(button);
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent(
      'Connection dropped after commit',
    ));
    expect(onPublished).not.toHaveBeenCalled();
    fireEvent.click(button);
    await waitFor(() => expect(onPublished).toHaveBeenCalledWith({
      version_id: ids[1], source_sha256: 'b'.repeat(64), rows_count: 1,
    }));
    expect(saveCheckpoint).toHaveBeenCalledTimes(2);
    expect(publish).toHaveBeenCalledTimes(2);
    expect(publish.mock.calls[0]?.[7]).toEqual(publish.mock.calls[1]?.[7]);
    expect(screen.getByRole('status')).toHaveTextContent(ids[1]!);
  });
});
