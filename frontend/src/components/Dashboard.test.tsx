import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { Dashboard } from './Dashboard';

const empty = {
  catalog: null,
  draft: null,
  connectionError: null,
};

describe('portfolio dashboard uses verified session state', () => {
  it('describes browser-only fixture behavior when embedded in the public demo', () => {
    render(<Dashboard isDemo {...empty} onNavigate={vi.fn()}/>);
    expect(screen.getByText('Учебный прайс · 5 вымышленных товаров')).toBeInTheDocument();
    expect(screen.getByText('Учебный пример без обращения к ИИ')).toBeInTheDocument();
    expect(screen.queryByText(/Товары, цены и характеристики из Excel или CSV/)).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /Шаблон Excel/ })).not.toBeInTheDocument();
  });
  it('never displays synthetic business metrics and provides a real import route', () => {
    const onNavigate = vi.fn();
    render(<Dashboard {...empty} onNavigate={onNavigate}/>);
    expect(screen.getByText('Начните с прайс-листа')).toBeInTheDocument();
    expect(screen.getByText('Не загружен')).toBeInTheDocument();
    expect(screen.queryByText('За 30 дней · демоданные')).not.toBeInTheDocument();
    expect(screen.queryByText('КП для ООО АльфаТех')).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: /Личный кабинет/ }))
      .toHaveAttribute('href', '/app');
    fireEvent.click(screen.getByRole('button', { name: /1. Загрузить прайс/ }));
    expect(onNavigate).toHaveBeenCalledWith('import');
    expect(screen.queryByRole('button', { name: /4. Посмотреть КП/ })).not.toBeInTheDocument();
  });
  it('renders only server-supplied catalog counts and actual draft state', () => {
    const onNavigate = vi.fn();
    render(<Dashboard catalog={{ id: 'v1', source_sha256: 'sha', rows_count: 35 }}
      draft={{ catalog_version_id: 'v1', ready_for_draft: false, total_rub: null,
        approval_status: 'not_approved', lines: [
          { line_number: 1, status: 'needs_review', sku: null,
            line_total_rub: null, reason_codes: [], selection_method: null },
          { line_number: 2, status: 'auto_selected', sku: 'SKU-2',
            line_total_rub: '25.00', reason_codes: [], selection_method: 'rapidfuzz' },
        ] }} connectionError={null} onNavigate={onNavigate}/>);
    expect(screen.getByText('35')).toBeInTheDocument();
    expect(screen.getByText('Позиции, требующие проверки: 1. Уточните их перед подготовкой документа.'))
      .toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /4. Посмотреть КП/ }));
    expect(onNavigate).toHaveBeenCalledWith('preview');
  });
});
