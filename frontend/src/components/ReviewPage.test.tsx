import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { ReviewPage } from './ReviewPage';
import type { AutoDraft, ExtractedRequest } from '../domain';

const extracted: ExtractedRequest = { lines: [
  { source_quote: '10 карандашей', description: 'Карандаш HB', category: null, quantity: 10,
    unit: 'штука', brand: null, paper_format: null, paper_gsm: null,
    pen_ink_color: null, pen_ink_type: null, attributes: {} },
  { source_quote: 'неизвестный товар', description: 'Товар с неуточнёнными свойствами', category: null,
    quantity: 1, unit: 'штука', brand: null, paper_format: null, paper_gsm: null,
    pen_ink_color: null, pen_ink_type: null, attributes: {} },
] };

const sample: AutoDraft = {
  catalog_version_id: 'sample-version', ready_for_draft: false, approval_status: 'not_approved',
  total_rub: null, lines: [
    { line_number: 1, status: 'auto_selected', sku: 'DEMO-001',
      line_total_rub: '350.00', reason_codes: [], selection_method: 'deterministic' },
    { line_number: 2, status: 'needs_review', sku: null,
      line_total_rub: null, reason_codes: ['attributes_not_verified'], selection_method: null },
  ],
};

describe('review screen #14', () => {
  it('guides an empty demo through local matching without claiming a server calculation', () => {
    render(<ReviewPage isDemo draft={null} extracted={null} onBack={vi.fn()}/>);
    expect(screen.getByText(/Расчёт выполняется в этой вкладке/)).toBeInTheDocument();
    expect(screen.queryByText(/серверный автоматический подбор/)).not.toBeInTheDocument();
  });
  it('requires a real draft rather than rendering invented supplier offers', () => {
    const onBack = vi.fn();
    render(<ReviewPage draft={null} extracted={null} onBack={onBack}/>);
    expect(screen.getByRole('heading', { name: 'Черновик ещё не сформирован' })).toBeInTheDocument();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /Новая заявка/ }));
    expect(onBack).toHaveBeenCalledOnce();
  });
  it('shows true matching statuses and no grand total for unresolved lines', () => {
    const onBack = vi.fn();
    render(<ReviewPage draft={sample} extracted={extracted} onBack={onBack}/>);
    expect(screen.getByRole('group', { name: 'Анализ результатов подбора' })).toBeVisible();
    expect(screen.getByText('артикул: DEMO-001')).toBeInTheDocument();
    expect(screen.getByText('Не подтверждены обязательные характеристики')).toBeInTheDocument();
    expect(screen.getByText(/350,00/)).toBeInTheDocument();
    const total = document.querySelector<HTMLDivElement>('.total-amount');
    expect(total).not.toBeNull();
    expect(within(total!).getByText('—')).toBeInTheDocument();
    expect(screen.getByText('Итоговая сумма появится после проверки спорных позиций.')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /утвердить|сохранить|отправить КП/i })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Уточнить заявку' }));
    expect(onBack).toHaveBeenCalledOnce();
  });
  it('marks fully matched proposal as not approved, without enabling delivery', () => {
    const complete = { ...sample, lines: [sample.lines[0]!], ready_for_draft: true, total_rub: '350.00' };
    render(<ReviewPage draft={complete} extracted={extracted} onBack={vi.fn()}/>);
    expect(screen.getByText('Стоимость рассчитана. Предложение ещё не утверждено и не отправлено клиенту.')).toBeInTheDocument();
  });

  it('opens a genuine local preview from an existing draft without invoking approval or delivery', () => {
    const onPreview = vi.fn();
    render(<ReviewPage draft={sample} extracted={extracted} onBack={vi.fn()}
      onPreview={onPreview}/>);
    fireEvent.click(screen.getByRole('button', { name: 'Предпросмотр КП' }));
    expect(onPreview).toHaveBeenCalledOnce();
    expect(screen.queryByRole('button', { name: /отправить клиенту|утвердить/i }))
      .not.toBeInTheDocument();
  });

});

it('explains unverified item identity without showing internal backend reason codes', () => {
  const blocked: AutoDraft = {
    ...sample,
    lines: [
      { line_number: 1, status: 'needs_review', sku: null,
        line_total_rub: null, selection_method: null,
        reason_codes: ['unverified_product_identity'] },
      { line_number: 2, status: 'needs_review', sku: null,
        line_total_rub: null, selection_method: null,
        reason_codes: ['explicit_identifier_mismatch'] },
    ],
  };
  const { container } = render(
    <ReviewPage draft={blocked} extracted={extracted} onBack={vi.fn()}/>,
  );
  expect(screen.getByText(/похожего названия недостаточно/)).toBeInTheDocument();
  expect(screen.getByText(/Артикул, модель или обозначение упаковки не совпадают/))
    .toBeInTheDocument();
  const text = container.textContent ?? '';
  expect(text).not.toContain('unverified_product_identity');
  expect(text).not.toContain('explicit_identifier_mismatch');
  expect(screen.getByText('Итоговая сумма появится после проверки спорных позиций.'))
    .toBeInTheDocument();
});
