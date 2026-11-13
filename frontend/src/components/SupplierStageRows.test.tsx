import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { SupplierStageRows } from './SupplierStageRows';
import type { SupplierStagePreview } from '../tenant-session';

const preview: SupplierStagePreview = {
  source_sha256: 'a'.repeat(64), total_rows: 1, priced_rub_rows: 0, unresolved_rows: 1,
  pack_size_issue_counts: {}, offset: 0, next_offset: null,
  requires_confirmation: true, canonical_importable: false, model_training_authorized: false,
  sampled_rows: [{ line: 2, source_row_sha256: 'b'.repeat(64), supplier_sku: 'SYNTHETIC',
    name: 'Учебный товар', price_rub: null, source_category: null,
    source_sale_unit: null, source_pack_size: null, pack_size_review_reason: null,
    issues: ['missing_name', 'price_precision_requires_review', 'future_source_issue'],
  }],
};

describe('supplier row diagnosis', () => {
  it('explains issues, keeps raw codes hidden until requested and preserves row selection', () => {
    const select = vi.fn();
    render(<SupplierStageRows preview={preview} busy={false} canPage={false}
      onPage={vi.fn()} onSelect={select}/>);
    expect(screen.getByRole('group', { name: 'Строки прайс-листа' })).toBeVisible();
    expect(screen.getByText('В строке нет названия товара. Проверьте исходный прайс.')).toBeVisible();
    expect(screen.getByText(/В исходной цене больше двух знаков после запятой/)).toBeVisible();
    expect(screen.getByText('Нужно дополнительно проверить данные строки или настройки прайса.')).toBeVisible();
    const details = screen.getByText(/missing_name, price_precision_requires_review, future_source_issue/);
    expect(details).not.toBeVisible();
    fireEvent.click(screen.getByText('Технические сведения о замечаниях'));
    expect(details).toBeVisible();
    expect(select).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Проверить единицу цены' }));
    expect(select).toHaveBeenCalledExactlyOnceWith(preview.sampled_rows[0]);
  });
});
