import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { RequestAnalysisPanel, RequirementMatrix } from './RequestAnalysis';

describe('request completeness and evidence', () => {
  it('shows source-bound questions and selectable text without sending a message', () => {
    const refresh = vi.fn();
    render(<RequestAnalysisPanel onRefresh={refresh} busy={false} canAnalyze analysis={{
      ready_for_matching: false, questions_text: 'Позиция 1 — Бумага\nКакая плотность бумаги нужна (г/м²)?',
      scope_notice: 'Проверены поля поддерживаемых категорий.',
      lines: [{ line_number: 1, source_quote: '20 коробок бумаги', description: 'Бумага',
        missing_fields: ['paper_gsm'], questions: ['Какая плотность бумаги нужна (г/м²)?'] }],
    }}/>);
    expect(screen.getByText('20 коробок бумаги')).toBeInTheDocument();
    expect(screen.getByLabelText('Вопросы для клиента')).toHaveValue('Позиция 1 — Бумага\nКакая плотность бумаги нужна (г/м²)?');
    fireEvent.click(screen.getByRole('button', { name: 'Проверить полноту заявки' }));
    expect(refresh).toHaveBeenCalledOnce();
    expect(screen.queryByRole('button', { name: /Отправить/ })).not.toBeInTheDocument();
  });

  it('shows actual values and distinguishes missing evidence from mismatch', () => {
    render(<RequirementMatrix checks={[
      { field: 'paper_gsm', label: 'Плотность', requested: '120', actual: '80', status: 'mismatch' },
      { field: 'brand', label: 'Бренд', requested: 'Acme', actual: null, status: 'unknown_product' },
    ]}/>);
    fireEvent.click(screen.getByText('Проверка характеристик'));
    const region = screen.getByRole('region', { name: 'Таблица соответствия требованиям' });
    expect(region).toHaveAttribute('tabindex', '0');
    region.focus();
    expect(region).toHaveFocus();
    expect(screen.getByText('120')).toBeInTheDocument();
    expect(screen.getByText('80')).toBeInTheDocument();
    expect(screen.getByText('Не соответствует')).toBeInTheDocument();
    expect(screen.getByText('Нет данных в прайсе')).toBeInTheDocument();
  });

  it('shows category conflicts even when all required fields are filled', () => {
    const notice = 'Уточните категорию: описание указывает на другой тип товара.';
    render(<>
      <RequestAnalysisPanel onRefresh={() => {}} busy={false} canAnalyze analysis={{
        ready_for_matching: false, questions_text: 'Позиция 1 — этикетки\nУточните наименование и назначение товара.',
        scope_notice: 'Проверены поля поддерживаемых категорий.',
        lines: [{ line_number: 1, source_quote: 'Этикетки — 2 пачки',
          description: 'Самоклеящаяся бумага для этикеток A4 80', missing_fields: [],
          questions: [notice] }],
      }}/>
      <RequirementMatrix checks={[{
        field: 'description', label: 'Наименование',
        requested: 'Самоклеящаяся бумага для этикеток A4 80', actual: 'Бумага офисная A4 80',
        status: 'mismatch', note: notice,
      }]}/>
    </>);
    expect(screen.getByText('Для подбора нужны уточнения.')).toBeInTheDocument();
    expect(screen.getByRole('listitem')).toHaveTextContent(notice);
    expect(screen.getByLabelText('Вопросы для клиента')).toHaveValue('Позиция 1 — этикетки\nУточните наименование и назначение товара.');
    expect(screen.getByText('Не соответствует')).toBeInTheDocument();
    expect(screen.getByText('Бумага офисная A4 80')).toBeInTheDocument();
  });
});
