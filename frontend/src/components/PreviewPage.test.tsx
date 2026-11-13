import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { PreviewPage } from './PreviewPage';
import type { AutoDraft, ExtractedRequest } from '../domain';

const request: ExtractedRequest = { lines: [
  {
    source_quote: '10 пачек бумаги А4', description: 'Бумага А4 80 г/м²',
    category: 'paper', quantity: 10, unit: 'пачка', brand: null,
    paper_format: 'A4', paper_gsm: 80,
    pen_ink_color: null, pen_ink_type: null, attributes: {},
  },
  {
    source_quote: 'нужны ручки', description: 'Шариковая ручка',
    category: 'pen', quantity: null, unit: null, brand: null,
    paper_format: null, paper_gsm: null, pen_ink_color: null,
    pen_ink_type: null, attributes: {},
  },
] };
const draft: AutoDraft = {
  catalog_version_id: 'version-1',
  approval_status: 'not_approved',
  ready_for_draft: false,
  total_rub: null,
  lines: [
    { line_number: 1, status: 'auto_selected', sku: 'REAL-1', line_total_rub: '1200.00',
      reason_codes: [], selection_method: 'deterministic' },
    { line_number: 2, status: 'needs_review', sku: null, line_total_rub: null,
      reason_codes: ['missing_quantity_or_unit'], selection_method: null },
  ],
};

describe('preview screen #15', () => {
  it('opens only an explicit, clearly marked demonstration when no live draft exists', () => {
    render(<PreviewPage draft={null} extracted={null} onBack={vi.fn()} onNewRequest={vi.fn()}/>);
    expect(screen.queryByText('Демонстрационные данные')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /Посмотреть пример оформления/ }));
    expect(screen.getByText('ДЕМО — НЕ ЯВЛЯЕТСЯ КП')).toBeInTheDocument();
    expect(screen.getByText('Демонстрационные данные')).toBeInTheDocument();
    expect(screen.getByText('Кабель ВВГнг-LS 5×6')).toBeInTheDocument();
    expect(screen.getByText('Розетка Schneider Electric Blanca')).toBeInTheDocument();
    expect(screen.getByText(/Пример оформления с вымышленными товарами/)).toBeInTheDocument();
    expect(screen.getByText(/Пример таблицы товаров с вымышленными данными/)).toBeInTheDocument();
  });

  it('shows no fake client or supplier information without a calculated draft', () => {
    const start = vi.fn();
    render(<PreviewPage draft={null} extracted={null} onBack={vi.fn()} onNewRequest={start}/>);
    expect(screen.getByRole('heading', { name: 'Нет черновика для предпросмотра' })).toBeInTheDocument();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Создать черновик' }));
    expect(start).toHaveBeenCalledOnce();
  });

  it('shows only server-calculated rows and blocks printing unresolved items', () => {
    const print = vi.fn();
    vi.stubGlobal('print', print);
    render(<PreviewPage draft={draft} extracted={request} onBack={vi.fn()} onNewRequest={vi.fn()}/>);
    const sheet = screen.getByRole('article', { name: 'Неутверждённое коммерческое предложение' });
    expect(sheet).toHaveTextContent('Бумага А4 80 г/м²');
    expect(sheet).toHaveTextContent('REAL-1');
    expect(sheet).toHaveTextContent('Требуется проверка');
    expect(sheet).toHaveTextContent('Уточните количество или единицу измерения');
    expect(sheet.querySelector('.print-summary')).toHaveTextContent('—');
    expect(sheet).not.toHaveTextContent('Итого к оплате');
    expect(screen.queryByRole('button', { name: /отправить клиенту|утвердить КП/i })).not.toBeInTheDocument();
    for (const button of screen.getAllByRole('button', { name: /Печать \/ сохранить PDF/ })) {
      expect(button).toBeDisabled();
    }
    fireEvent.click(screen.getAllByRole('button', { name: /Печать \/ сохранить PDF/ })[0]!);
    expect(print).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });

  it('changes real presentation options without mutating draft data', () => {
    const complete: AutoDraft = {
      ...draft, ready_for_draft: true, total_rub: '1200.00', lines: [draft.lines[0]!],
    };
    render(<PreviewPage draft={complete} extracted={request} onBack={vi.fn()} onNewRequest={vi.fn()}/>);
    expect(screen.getAllByText(/1\s?200,00/)).toHaveLength(2);
    fireEvent.click(screen.getByRole('button', { name: 'Современный' }));
    expect(screen.getByRole('button', { name: 'Современный' })).toHaveAttribute('aria-pressed', 'true');
    expect(document.querySelector('.print-sheet.template-modern')).not.toBeNull();
    fireEvent.click(screen.getByRole('checkbox', { name: 'Показывать логотип' }));
    expect(document.querySelector('.print-header img')).toBeNull();
    expect(complete.total_rub).toBe('1200.00');
  });

  it('keeps synthetic provenance on the printed sheet after an interactive demo calculation', () => {
    const complete: AutoDraft = {
      ...draft, ready_for_draft: true, total_rub: '1200.00', lines: [draft.lines[0]!],
    };
    render(<PreviewPage isDemo draft={complete} extracted={request} onBack={vi.fn()} onNewRequest={vi.fn()}/>);
    const sheet = screen.getByRole('article');
    expect(sheet).toHaveTextContent('ДЕМО — НЕ ЯВЛЯЕТСЯ КП');
    expect(sheet).toHaveTextContent('Учебный документ с вымышленными товарами, ценами и примером расчёта.');
    expect(sheet).toHaveTextContent('Не является счётом, офертой или действующим коммерческим предложением');
    expect(sheet).not.toHaveTextContent('по опубликованному каталогу');
    expect(screen.getAllByRole('button', { name: /Печать \/ сохранить PDF/ })[0]).toBeEnabled();
  });

  it('still blocks printing unresolved and overbudget browser demos', () => {
    const { rerender } = render(<PreviewPage isDemo draft={draft} extracted={request} onBack={vi.fn()} onNewRequest={vi.fn()}/>);
    expect(screen.getAllByRole('button', { name: /Печать \/ сохранить PDF/ })[0]).toBeDisabled();
    rerender(<PreviewPage isDemo draft={{ ...draft, lines: [draft.lines[0]!],
      budget_exceeded: true, total_rub: '1200.00' }} extracted={request}
      onBack={vi.fn()} onNewRequest={vi.fn()}/>);
    expect(screen.getAllByRole('button', { name: /Печать \/ сохранить PDF/ })[0]).toBeDisabled();
  });
});
