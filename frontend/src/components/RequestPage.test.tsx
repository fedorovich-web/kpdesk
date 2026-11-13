import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { RequestPage } from './RequestPage';
import type { ExtractedLine } from '../domain';

const line: ExtractedLine = {
  source_quote: 'Нужно десять пачек бумаги А4',
  description: 'Бумага А4',
  category: 'paper', quantity: 10, unit: 'пачка',
  brand: null, paper_format: 'A4', paper_gsm: 80,
  pen_ink_color: null, pen_ink_type: null, attributes: {},
};

function props(overrides: Partial<Parameters<typeof RequestPage>[0]> = {}) {
  return {
    catalog: null,
    catalogError: null,
    requestText: '',
    model: 'openai/gpt-oss-120b',
    extracted: null,
    rulesText: '',
    rulesProposal: null,
    rulesConfirmed: false,
    reviewingRules: false,
    onRulesTextChange: vi.fn(),
    onPreviewRules: vi.fn(),
    onConfirmRules: vi.fn(),
    busy: false,
    extracting: false,
    drafting: false,
    onTextChange: vi.fn(),
    onModelChange: vi.fn(),
    onExtract: vi.fn((event: { preventDefault: () => void }) => event.preventDefault()),
    onAddManual: vi.fn(),
    onAddLine: vi.fn(),
    onChangeLine: vi.fn(),
    onRemoveLine: vi.fn(),
    onResetLines: vi.fn(),
    onDraft: vi.fn(),
    onBack: vi.fn(),
    onImport: vi.fn(),
    ...overrides,
  };
}

describe('request page #13', () => {
  it('calls an activated demo catalog a local teaching catalog', () => {
    render(<RequestPage {...props({ isDemo: true,
      catalog: { id: 'demo', rows_count: 5, source_sha256: 'synthetic' } })}/>);
    expect(screen.getByText('Учебный · в этой вкладке')).toBeInTheDocument();
    expect(screen.queryByText('Опубликован')).not.toBeInTheDocument();
  });
  it('shows interpreted brand restrictions and distinguishes safe demo calculation', () => {
    const actions = props({
      isDemo: true,
      rulesText: 'Только бренд DemoPaper. Исключи бренд Acme.',
      rulesConfirmed: true,
      rulesProposal: {
        raw_text: 'Только бренд DemoPaper. Исключи бренд Acme.',
        can_confirm: true,
        unresolved_requirements: [],
        rules: {
          schema_version: 1, markup_percent: '0', budget_max_rub: null,
          required_brand: 'demopaper', excluded_brands: ['acme'],
        },
      },
    });
    render(<RequestPage {...actions}/>);
    expect(screen.getByText(/Только бренд: demopaper/)).toBeInTheDocument();
    expect(screen.getByText(/Исключены: acme/)).toBeInTheDocument();
    expect(screen.getByText(/Они будут применены к учебным данным в браузере/))
      .toBeInTheDocument();
    expect(screen.queryByText(/Они будут применены сервером при расчёте/))
      .not.toBeInTheDocument();
  });
  it('renders only the three implemented workflow steps, without fake supplier or address inputs', () => {
    const actions = props();
    render(<RequestPage {...actions}/>);
    expect(screen.getByRole('heading', { name: 'Заявка клиента', level: 1 })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Добавьте заявку', level: 2 })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Товары из заявки' })).toBeInTheDocument();
    expect(document.querySelectorAll('.request-intro .stepper li')).toHaveLength(3);
    expect(screen.getByRole('button', { name: /Распознать заявку/ })).toBeDisabled();
    expect(screen.queryByLabelText('Адрес доставки')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Приоритет')).not.toBeInTheDocument();
    expect(screen.getByText('Не загружен')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /Добавить товары вручную/ }));
    expect(actions.onAddManual).toHaveBeenCalledOnce();
  });

  it('passes user-entered text and a real form submission through callbacks', () => {
    const actions = props({ requestText: 'Бумага А4 10 пачек' });
    render(<RequestPage {...actions}/>);
    fireEvent.change(screen.getByRole('textbox', { name: /Текст заявки/ }), {
      target: { value: 'Бумага А4 20 пачек' },
    });
    expect(actions.onTextChange).toHaveBeenCalledWith('Бумага А4 20 пачек');
    fireEvent.click(screen.getByRole('button', { name: /Распознать заявку/ }));
    expect(actions.onExtract).toHaveBeenCalledOnce();
  });

  it('allows editing recognized lines but blocks draft generation without a published catalog', () => {
    const actions = props({ requestText: 'Бумага А4 10 пачек', extracted: { lines: [line] } });
    render(<RequestPage {...actions}/>);
    expect(screen.getByRole('button', { name: /Подобрать товары и рассчитать КП/ })).toBeDisabled();
    expect(screen.getByText('1 поз.')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /Добавить позицию/ }));
    expect(actions.onAddLine).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByRole('button', { name: /Вернуться к распознаванию/ }));
    expect(actions.onResetLines).toHaveBeenCalledOnce();
    expect(actions.onDraft).not.toHaveBeenCalled();
  });

  it('shows a classifier suggestion but changes category only after explicit confirmation', async () => {
    const propose = vi.fn().mockResolvedValue({
      original_category: 'paper', suggested_category: 'mouse_pad',
      best_category: 'mouse_pad', confidence: 0.86, margin: 0.15,
      abstained: false, requires_explicit_acceptance: true,
    });
    const actions = props({
      extracted: { lines: [{ ...line, description: 'Коврик для мыши' }] },
      onSuggestCategory: propose,
    });
    render(<RequestPage {...actions}/>);
    fireEvent.click(screen.getByRole('button', { name: 'Предложить категорию' }));
    const accept = await screen.findByRole('button', { name: 'Подтвердить смену категории' });
    expect(propose).toHaveBeenCalledWith('Коврик для мыши', 'paper');
    expect(actions.onChangeLine).not.toHaveBeenCalled();
    fireEvent.click(accept);
    expect(actions.onChangeLine).toHaveBeenCalledWith(0, expect.objectContaining({
      category: 'mouse_pad', attributes: {}, paper_format: null, paper_gsm: null,
      quantity: 10, unit: 'пачка',
    }));
  });

  it('does not offer category acceptance when the classifier abstains', async () => {
    const actions = props({
      extracted: { lines: [line] },
      onSuggestCategory: vi.fn().mockResolvedValue({
        original_category: 'paper', suggested_category: null,
        best_category: 'notebook', confidence: 0.22, margin: 0.01,
        abstained: true, requires_explicit_acceptance: true,
      }),
    });
    render(<RequestPage {...actions}/>);
    fireEvent.click(screen.getByRole('button', { name: 'Предложить категорию' }));
    expect(await screen.findByText(/Не удалось однозначно определить категорию/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Подтвердить смену категории' })).not.toBeInTheDocument();
    expect(actions.onChangeLine).not.toHaveBeenCalled();
  });
});
