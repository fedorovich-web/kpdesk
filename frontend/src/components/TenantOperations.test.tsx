import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { sessionApi, SessionApiError } from '../tenant-session';
import type { QuoteLineInput } from '../tenant-session';
import { TenantOperations } from './TenantOperations';

const catalog = { version_id: 'catalog-version-1', source_sha256: 'hash', rows_count: 1 };
beforeEach(() => {
  vi.spyOn(sessionApi, 'quoteShares').mockResolvedValue({ available: false, shares: [] });
});
const makeMocks = () => {
  vi.spyOn(sessionApi, 'invitations').mockResolvedValue([]);
  vi.spyOn(sessionApi, 'listQuotes').mockResolvedValue({ items: [] });
  vi.spyOn(sessionApi, 'members').mockResolvedValue([]);
};

const fillPaperFacts = (index = 0) => {
  fireEvent.change(screen.getAllByLabelText('Категория')[index]!, { target: { value: 'paper' } });
  fireEvent.change(screen.getAllByLabelText('Количество')[index]!, { target: { value: '1' } });
  fireEvent.change(screen.getAllByLabelText('Единица')[index]!, { target: { value: 'пачка' } });
  fireEvent.change(screen.getAllByLabelText('Формат')[index]!, { target: { value: 'A4' } });
  fireEvent.change(screen.getAllByLabelText('Плотность бумаги, г/м²')[index]!, { target: { value: '80' } });
};

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('real tenant team and quote screens', () => {
  it('explains conflicting piece packaging without exposing a priced or downloadable quote', async () => {
    makeMocks();
    vi.mocked(sessionApi.listQuotes).mockResolvedValue({ items: [{ quote_id: 'bad-package',
      revision: 1, total_rub: null, ready_for_draft: false, approved: false }] });
    vi.spyOn(sessionApi, 'getQuote').mockResolvedValue({ quote_id: 'bad-package', revision: 1,
      catalog_version_id: catalog.version_id, ready_for_draft: false, total_rub: null,
      snapshot_sha256: 'a'.repeat(64), approved: false, lines: [{
        line_number: 1, description: 'Учебный товар', status: 'needs_review', sku: null,
        product_name: null, requested_quantity: 2, requested_unit: 'штука',
        sale_quantity: null, sale_unit: null, unit_price_rub: null, line_total_rub: null,
        reason_codes: ['inconsistent_piece_package'],
      }] });
    vi.spyOn(sessionApi, 'quoteReview').mockRejectedValue(new SessionApiError('not_found', '', 404));
    render(<TenantOperations organization={{ id: 'org-bad-package', name: 'Компания', role: 'editor' }}
      catalog={catalog} csrf="csrf" onOrganizationChange={vi.fn()}/>);
    fireEvent.click(await screen.findByRole('button', { name: 'Открыть' }));
    expect(await screen.findByText(/В прайсе не совпадают единица продажи и количество штук/))
      .toBeVisible();
    expect(screen.queryByText('inconsistent_piece_package')).not.toBeInTheDocument();
    expect(screen.getByText(/PDF недоступен, пока строки КП требуют проверки/)).toBeVisible();
  });

  it('shows approval for the current revision on initial and continued quote pages', async () => {
    makeMocks();
    const item = { revision: 2, total_rub: '10.00', ready_for_draft: true };
    vi.mocked(sessionApi.listQuotes).mockResolvedValueOnce({
      items: [{ ...item, quote_id: 'approved-head', approved: true },
        { ...item, quote_id: 'new-draft', approved: false }], next_cursor: 'status-page',
    }).mockResolvedValueOnce({
      items: [{ ...item, quote_id: 'older-approved', approved: true }], next_cursor: null,
    }).mockResolvedValueOnce({
      items: [{ ...item, quote_id: 'approved-head', revision: 3, approved: false }], next_cursor: null,
    });
    render(<TenantOperations organization={{ id: 'org-status', name: 'Компания', role: 'editor' }}
      catalog={catalog} csrf="csrf" onOrganizationChange={vi.fn()}/>);
    const current = await screen.findByText(/КП approved/);
    expect(current).toHaveTextContent('Утверждено');
    expect(screen.getByText(/КП new-draf/)).toHaveTextContent('Не утверждено');
    fireEvent.click(screen.getByRole('button', { name: 'Показать ещё' }));
    expect(await screen.findByText(/КП older-ap/)).toHaveTextContent('Утверждено');
    fireEvent.click(screen.getByRole('button', { name: /Обновить$/ }));
    await waitFor(() => expect(screen.getByText(/КП approved/)).toHaveTextContent('версия 3'));
    expect(screen.getByText(/КП approved/)).toHaveTextContent('Не утверждено');
  });

  it('does not invent an approval status when an older list response omits it', async () => {
    makeMocks();
    vi.mocked(sessionApi.listQuotes).mockResolvedValue({ items: [{ quote_id: 'unknown-status',
      revision: 1, total_rub: '10.00', ready_for_draft: true }] });
    render(<TenantOperations organization={{ id: 'org-unknown-status', name: 'Компания', role: 'editor' }}
      catalog={catalog} csrf="csrf" onOrganizationChange={vi.fn()}/>);
    expect(await screen.findByText(/КП unknown-/)).toHaveTextContent('Статус не загружен');
  });

  it('loads the next quote page, keeps existing rows and removes continuation at the end', async () => {
    makeMocks();
    const item = (id: string) => ({ quote_id: id, revision: 1, total_rub: '10.00', ready_for_draft: true });
    vi.mocked(sessionApi.listQuotes).mockResolvedValueOnce({
      items: Array.from({ length: 50 }, (_, i) => item(`first-${i}`)), next_cursor: 'cursor-50',
    }).mockResolvedValueOnce({ items: [item('older-51')], next_cursor: null });
    render(<TenantOperations organization={{ id: 'org-pages', name: 'Компания', role: 'editor' }}
      catalog={catalog} csrf="csrf" onOrganizationChange={vi.fn()}/>);
    fireEvent.click(await screen.findByRole('button', { name: 'Показать ещё' }));
    await screen.findByText(/КП older-51/);
    expect(screen.getAllByRole('button', { name: 'Открыть' })).toHaveLength(51);
    expect(sessionApi.listQuotes).toHaveBeenLastCalledWith('org-pages', 'cursor-50');
    expect(screen.queryByRole('button', { name: 'Показать ещё' })).not.toBeInTheDocument();
  });

  it('keeps quote rows and cursor after a failed continuation so the same page can be retried', async () => {
    makeMocks();
    const item = { quote_id: 'existing', revision: 1, total_rub: null, ready_for_draft: false };
    vi.mocked(sessionApi.listQuotes).mockResolvedValueOnce({ items: [item], next_cursor: 'retry-page' })
      .mockRejectedValueOnce(new Error('Страница временно недоступна'))
      .mockResolvedValueOnce({ items: [{ ...item, quote_id: 'retried' }], next_cursor: null });
    render(<TenantOperations organization={{ id: 'org-retry-page', name: 'Компания', role: 'editor' }}
      catalog={catalog} csrf="csrf" onOrganizationChange={vi.fn()}/>);
    fireEvent.click(await screen.findByRole('button', { name: 'Показать ещё' }));
    await screen.findByText('Страница временно недоступна');
    expect(screen.getByText(/КП existing/)).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'Показать ещё' }));
    await screen.findByText(/КП retried/);
    expect(sessionApi.listQuotes).toHaveBeenLastCalledWith('org-retry-page', 'retry-page');
  });

  it('discards a late continuation and clears old quote rows when switching organizations', async () => {
    makeMocks();
    const item = { quote_id: 'old-org', revision: 1, total_rub: '10.00', ready_for_draft: true };
    let finish!: (value: { items: typeof item[]; next_cursor: null }) => void;
    vi.mocked(sessionApi.listQuotes).mockResolvedValueOnce({ items: [item], next_cursor: 'old-cursor' })
      .mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }))
      .mockResolvedValueOnce({ items: [{ ...item, quote_id: 'new-org' }], next_cursor: null });
    const props = { catalog, csrf: 'csrf', onOrganizationChange: vi.fn() };
    const view = render(<TenantOperations {...props}
      organization={{ id: 'old-tenant', name: 'Старая', role: 'editor' }}/>);
    fireEvent.click(await screen.findByRole('button', { name: 'Показать ещё' }));
    view.rerender(<TenantOperations {...props}
      organization={{ id: 'new-tenant', name: 'Новая', role: 'editor' }}/>);
    await screen.findByText(/КП new-org/);
    await act(async () => { finish({ items: [{ ...item, quote_id: 'late-old' }], next_cursor: null }); });
    expect(screen.queryByText(/КП old-org/)).not.toBeInTheDocument();
    expect(screen.queryByText(/КП late-old/)).not.toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: 'Открыть' })).toHaveLength(1);
  });

  it('ignores a continuation completed after a refresh and prevents repeated page clicks', async () => {
    makeMocks();
    const item = { quote_id: 'initial', revision: 1, total_rub: '10.00', ready_for_draft: true };
    let finish!: (value: { items: typeof item[]; next_cursor: null }) => void;
    vi.mocked(sessionApi.listQuotes).mockResolvedValueOnce({ items: [item], next_cursor: 'cursor' })
      .mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }))
      .mockResolvedValueOnce({ items: [{ ...item, quote_id: 'refreshed' }], next_cursor: null });
    render(<TenantOperations organization={{ id: 'org-refresh', name: 'Компания', role: 'editor' }}
      catalog={catalog} csrf="csrf" onOrganizationChange={vi.fn()}/>);
    const more = await screen.findByRole('button', { name: 'Показать ещё' });
    fireEvent.click(more);
    fireEvent.click(more);
    expect(sessionApi.listQuotes).toHaveBeenCalledTimes(2);
    fireEvent.click(screen.getByRole('button', { name: /Обновить$/ }));
    await screen.findByText(/КП refreshe/);
    await act(async () => { finish({ items: [{ ...item, quote_id: 'late-page' }], next_cursor: null }); });
    expect(screen.queryByText(/КП late-pag/)).not.toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: 'Открыть' })).toHaveLength(1);
  });

  it('starts every manual line with unknown facts and a single primary lookup in the compact workspace', () => {
    makeMocks();
    render(<TenantOperations organization={{ id: 'org-blank', name: 'Компания', role: 'editor' }}
      catalog={catalog} csrf="csrf" compactNavigation onOrganizationChange={vi.fn()}/>);
    expect(screen.getByLabelText('Категория')).toHaveValue('unknown');
    expect(screen.getByLabelText('Количество')).toHaveValue(0);
    expect(screen.getByLabelText('Единица')).toHaveValue('');
    expect(screen.queryByLabelText('Формат')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Подобрать товары' })).toBeVisible();
    expect(screen.getByText('Подбор по характеристикам')).not.toBeVisible();
    expect(screen.getByText('Поиск по названию')).not.toBeVisible();
    expect(screen.getByText('Проверить категорию')).not.toBeVisible();
    expect(screen.getByText('Дополнительные инструменты подбора').closest('details')).not.toHaveAttribute('open');
    fireEvent.click(screen.getByRole('button', { name: /Добавить позицию/ }));
    expect(screen.getAllByLabelText('Категория').map(input => (input as HTMLSelectElement).value)).toEqual(['unknown', 'unknown']);
  });
  it('opens an approved saved offer read-only until an explicit edit and resets to a blank editor', async () => {
    makeMocks();
    const approved = { quote_id: 'approved', revision: 1, catalog_version_id: catalog.version_id,
      ready_for_draft: true, total_rub: '100.00', approved: true, snapshot_sha256: 'a'.repeat(64), lines: [],
      source_request: { lines: [{ source_quote: 'Бумага A4', description: 'Бумага A4', category: 'paper',
        quantity: 2, unit: 'пачка', paper_format: 'A4', paper_gsm: 80, brand: null,
        pen_ink_color: null, pen_ink_type: null, attributes: {} }] } };
    vi.mocked(sessionApi.listQuotes).mockResolvedValue({ items: [{ quote_id: approved.quote_id,
      revision: 1, total_rub: '100.00', ready_for_draft: true }] });
    vi.spyOn(sessionApi, 'getQuote').mockResolvedValue(approved);
    vi.spyOn(sessionApi, 'quoteReview').mockRejectedValue(new SessionApiError('not_found', '', 404));
    render(<TenantOperations organization={{ id: 'org-view', name: 'Компания', role: 'editor' }}
      catalog={catalog} csrf="csrf" compactNavigation onOrganizationChange={vi.fn()}/>);
    fireEvent.click(await screen.findByRole('button', { name: 'Открыть' }));
    await screen.findByRole('heading', { name: 'КП · версия 1' });
    expect(screen.queryByLabelText('Наименование позиции')).not.toBeInTheDocument();
    expect(screen.queryByText(/заново выберите товары/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Изменить состав' }));
    expect(screen.getByLabelText('Наименование позиции')).toHaveValue('Бумага A4');
    expect(screen.getByRole('button', { name: 'Сохранить состав' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Создать новое КП' }));
    expect(screen.getByLabelText('Категория')).toHaveValue('unknown');
  });
  it('describes saving composition honestly and collapses technical calculation details', async () => {
    makeMocks();
    vi.spyOn(sessionApi, 'createQuote').mockResolvedValue({ quote_id: 'draft', revision: 1,
      catalog_version_id: catalog.version_id, ready_for_draft: true, total_rub: '120.00', base_total_rub: '100.00',
      approved: false, snapshot_sha256: 'a'.repeat(64), lines: [] });
    render(<TenantOperations organization={{ id: 'org-save', name: 'Компания', role: 'editor' }}
      catalog={catalog} csrf="csrf" compactNavigation onOrganizationChange={vi.fn()}/>);
    fillPaperFacts();
    fireEvent.change(screen.getByLabelText('Наименование позиции'), { target: { value: 'Бумага A4 80' } });
    fireEvent.click(screen.getByRole('button', { name: 'Сохранить состав' }));
    await screen.findByText('Состав сохранён. Подготовьте предложение клиенту');
    expect(screen.getByText(/Сумма по прайсу/)).not.toBeVisible();
    expect(screen.getByText('Подробности расчёта').closest('details')).not.toHaveAttribute('open');
    expect(screen.getByText(/Сумма КП/)).toBeVisible();
  });
  it('uses a saved offer as a new template without copying approvals, SKU decisions or the client', async () => {
    makeMocks();
    const old = {
      quote_id: 'old-template', revision: 2, catalog_version_id: catalog.version_id,
      ready_for_draft: true, total_rub: '200.00', approved: true, snapshot_sha256: 'a'.repeat(64),
      lines: [], source_request: { lines: [{ source_quote: 'Бумага A4', description: 'Бумага A4',
        category: 'paper', quantity: 2, unit: 'пачка', brand: null, paper_format: 'A4', paper_gsm: 80,
        pen_ink_color: null, pen_ink_type: null, attributes: {} }] },
      document: { issuer_name: 'Компания', recipient_name: 'Старый клиент', manager_brief: 'Старое задание',
        commercial_terms: 'Оплата согласуется отдельно.', issued_on: '2026-10-28',
        appearance: { template: 'compact' as const, accent_color: '#123456', show_company_section: false },
        narrative: { headline: 'Старое предложение', opening: 'Старое вступление', solution: 'Старое решение', closing: 'Старое завершение' } },
    };
    vi.mocked(sessionApi.listQuotes).mockResolvedValue({ items: [{ quote_id: old.quote_id,
      revision: 2, total_rub: '200.00', ready_for_draft: true }] });
    vi.spyOn(sessionApi, 'getQuote').mockResolvedValue(old);
    vi.spyOn(sessionApi, 'quoteReview').mockRejectedValue(new SessionApiError('not_found', '', 404));
    const create = vi.spyOn(sessionApi, 'createQuote').mockResolvedValue({ ...old,
      quote_id: 'new-template', revision: 1, approved: false, document: null });
    const revise = vi.spyOn(sessionApi, 'reviseQuote');
    render(<TenantOperations organization={{ id: 'org-template', name: 'Компания', role: 'editor' }}
      catalog={catalog} csrf="csrf-template" onOrganizationChange={vi.fn()}/>);
    fireEvent.click(await screen.findByRole('button', { name: 'Открыть' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Создать КП по этому шаблону' }));
    expect(screen.getByLabelText('Наименование позиции')).toHaveValue('Бумага A4');
    expect(screen.queryByLabelText('Выбранный товар')).not.toBeInTheDocument();
    expect(screen.queryByText('Старое предложение')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Рассчитать и сохранить КП' })).toBeDisabled();
    fireEvent.click(screen.getByLabelText('Проверил позицию по исходной заявке'));
    fireEvent.click(screen.getByRole('button', { name: 'Рассчитать и сохранить КП' }));
    await screen.findByLabelText('Для кого предложение');
    expect(screen.getByLabelText('Для кого предложение')).toHaveValue('');
    expect(screen.getByLabelText(/Краткое предложение/)).toBeChecked();
    expect(screen.getByLabelText('Фирменный цвет')).toHaveValue('#123456');
    expect(screen.getByLabelText('Подтверждённые условия оплаты и поставки')).toHaveValue(old.document.commercial_terms);
    expect(create).toHaveBeenCalledOnce();
    expect(revise).not.toHaveBeenCalled();
  });
  it('keeps the compact workspace focused on requests and reveals team tools on demand', async () => {
    makeMocks();
    render(<TenantOperations organization={{ id: 'org-compact', name: 'Compact', role: 'owner' }}
      catalog={catalog} csrf="csrf-compact" compactNavigation onOrganizationChange={vi.fn()}/>);
    const team = document.getElementById('workspace-team') as HTMLDetailsElement;
    const sources = document.getElementById('workspace-sources') as HTMLDetailsElement;
    expect(team?.open).toBe(false);
    expect(sources?.open).toBe(false);
    expect(screen.getByRole('heading', { name: 'Команда организации' })).not.toBeVisible();
    fireEvent.click(screen.getByText('Команда и доступ'));
    expect(await screen.findByRole('heading', { name: 'Команда организации' })).toBeVisible();
  });
  it('shows owner-only invitation controls and binds tenant id', async () => {
    makeMocks();
    const send = vi.spyOn(sessionApi, 'invite').mockResolvedValue({
      invitation_id: 'invitation-1',
    });
    render(<TenantOperations
      organization={{ id: 'org-one', name: 'Acme', role: 'owner' }}
      catalog={catalog} csrf="session-csrf" onOrganizationChange={vi.fn()}
    />);
    expect(await screen.findByRole('heading', { name: 'Команда организации' }))
      .toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Электронная почта сотрудника'), {
      target: { value: 'PERSON@EXAMPLE.TEST' },
    });
    fireEvent.click(screen.getByRole('button', { name: /Пригласить/ }));
    await waitFor(() => {
      expect(send).toHaveBeenCalledWith(
        'org-one', 'person@example.test', 'viewer', 'session-csrf',
      );
    });
  });

  it('does not render write actions or list team for viewer', async () => {
    makeMocks();
    render(<TenantOperations
      organization={{ id: 'org-viewer', name: 'Read only', role: 'viewer' }}
      catalog={catalog} csrf="viewer-token" onOrganizationChange={vi.fn()}
    />);
    await screen.findByRole('heading', { name: 'Коммерческие предложения' });
    expect(screen.queryByRole('heading', { name: 'Команда организации' })).not.toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Проверка цен поставщиков' }))
      .not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Рассчитать и сохранить КП' }))
      .not.toBeInTheDocument();
    expect(sessionApi.members).not.toHaveBeenCalled();
  });

  it('submits typed manual quote data and idempotency key only for selected org', async () => {
    makeMocks();
    vi.stubGlobal('crypto', { randomUUID: () => 'quote-idempotency-a17' });
    const create = vi.spyOn(sessionApi, 'createQuote').mockResolvedValue({
      quote_id: 'quote-1', revision: 1, catalog_version_id: 'catalog-version-1',
      ready_for_draft: true, total_rub: '859.80', lines: [],
      snapshot_sha256: 'sha', approved: false,
    });
    render(<TenantOperations
      organization={{ id: 'org-editor', name: 'Editors', role: 'editor' }}
      catalog={catalog} csrf="csrf-editor" onOrganizationChange={vi.fn()}
    />);
    fillPaperFacts();
    fireEvent.click(screen.getByRole('button', { name: /Добавить позицию/ }));
    const descriptions = screen.getAllByLabelText('Наименование позиции');
    fillPaperFacts(1);
    expect(descriptions).toHaveLength(2);
    fireEvent.change(descriptions[0]!,  {
      target: { value: 'Бумага A4 80' },
    });
    fireEvent.change(descriptions[1]!,  {
      target: { value: 'Дополнительная пачка A4' },
    });
    fireEvent.change(screen.getAllByLabelText('Количество')[0]!,  {
      target: { value: '2' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Рассчитать и сохранить КП' }));
    await waitFor(() => {
      expect(create).toHaveBeenCalledWith(
        'org-editor', 'catalog-version-1',
        [expect.objectContaining({
          description: 'Бумага A4 80', category: 'paper',
          quantity: 2, unit: 'пачка', paper_format: 'A4', paper_gsm: 80,
        }), expect.objectContaining({
          description: 'Дополнительная пачка A4', category: 'paper',
          quantity: 1, unit: 'пачка',
        })], 'csrf-editor', 'quote-idempotency-a17',
      );
    });
    expect(await screen.findByText(/КП сохранено/)).toBeInTheDocument();
  });

  it('starts a blank independent quote after saving instead of reusing previous rows', async () => {
    makeMocks();
    vi.stubGlobal('crypto', { randomUUID: vi.fn()
      .mockReturnValueOnce('quote-first').mockReturnValueOnce('quote-second') });
    const create = vi.spyOn(sessionApi, 'createQuote')
      .mockResolvedValueOnce({
        quote_id: 'quote-first', revision: 1, catalog_version_id: 'catalog-version-1',
        ready_for_draft: false, total_rub: null, lines: [],
        snapshot_sha256: 'first', approved: false,
      })
      .mockResolvedValueOnce({
        quote_id: 'quote-second', revision: 1, catalog_version_id: 'catalog-version-1',
        ready_for_draft: false, total_rub: null, lines: [],
        snapshot_sha256: 'second', approved: false,
      });
    const revise = vi.spyOn(sessionApi, 'reviseQuote');
    render(<TenantOperations
      organization={{ id: 'org-one', name: 'Acme', role: 'editor' }}
      catalog={catalog} csrf="csrf-test" onOrganizationChange={vi.fn()}
    />);
    fillPaperFacts();
    fireEvent.change(screen.getByLabelText('Текст заявки (до 12 000 символов)'), {
      target: { value: 'Вчерашний запрос' },
    });
    fireEvent.change(screen.getByLabelText('Наименование позиции'), {
      target: { value: 'Первая партия бумаги' },
    });
    fireEvent.click(screen.getByRole('button', { name: /Добавить позицию/ }));
    fireEvent.change(screen.getAllByLabelText('Наименование позиции')[1]!, {
      target: { value: 'Вторая партия бумаги' },
    });
    fillPaperFacts(1);
    fireEvent.click(screen.getByRole('button', { name: 'Рассчитать и сохранить КП' }));
    await waitFor(() => expect(create).toHaveBeenCalledTimes(1));
    fireEvent.click(await screen.findByRole('button', { name: 'Создать новое КП' }));
    expect(screen.getAllByLabelText('Наименование позиции')).toHaveLength(1);
    expect(screen.getByLabelText('Наименование позиции')).toHaveValue('');
    expect(screen.getByLabelText('Текст заявки (до 12 000 символов)')).toHaveValue('');
    expect(screen.queryByRole('heading', { name: 'КП · версия 1' })).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Наименование позиции'), {
      target: { value: 'Новая бумага' },
    });
    fillPaperFacts();
    fireEvent.click(screen.getByRole('button', { name: 'Рассчитать и сохранить КП' }));
    await waitFor(() => expect(create).toHaveBeenCalledTimes(2));
    expect(create.mock.calls[1]?.[2]).toEqual([
      expect.objectContaining({ description: 'Новая бумага' }),
    ]);
    expect(revise).not.toHaveBeenCalled();
  });
});

describe('authenticated tenant quote instructions', () => {
  it('explains invalid numeric rules, preserves unknown clauses and blocks quote creation', async () => {
    makeMocks();
    const text = 'Наценка 1 8%. Бюджет до 12 34 рублей. сроки поставки нужно уточнить. constructor';
    vi.spyOn(sessionApi, 'previewQuoteInstructions').mockResolvedValue({ raw_text: text,
      rules: { schema_version: 1, markup_percent: '0', budget_max_rub: null },
      unresolved_requirements: ['invalid_markup_percent', 'invalid_budget_max_rub', 'conflicting_required_brand',
        'сроки поставки нужно уточнить', 'constructor'], can_confirm: false });
    const create = vi.spyOn(sessionApi, 'createQuote');
    render(<TenantOperations organization={{ id: 'org-invalid-rule-number', name: 'Компания', role: 'editor' }}
      catalog={catalog} csrf="csrf" onOrganizationChange={vi.fn()}/>);
    fillPaperFacts();
    fireEvent.change(screen.getByLabelText('Наименование позиции'), { target: { value: 'Бумага A4' } });
    fireEvent.change(screen.getByLabelText('Условия подбора и расчёта'), { target: { value: text } });
    fireEvent.click(screen.getByRole('button', { name: 'Проверить условия' }));
    expect(await screen.findByText(/Проверьте наценку: одно число от 0 до 100/)).toBeVisible();
    expect(screen.getByText(/Проверьте бюджет: положительное число, группы тысяч по три цифры/)).toBeVisible();
    expect(screen.getByText(/Не удалось применить:/)).toHaveTextContent('сроки поставки нужно уточнить');
    expect(screen.getByText(/Не удалось применить:/)).toHaveTextContent('; constructor');
    expect(screen.getByText(/Не удалось применить:/)).toHaveTextContent('Укажите один обязательный бренд');
    expect(screen.queryByText(/invalid_markup_percent|invalid_budget_max_rub|conflicting_required_brand/)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Подтвердить условия' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Рассчитать и сохранить КП' }));
    expect(create).not.toHaveBeenCalled();
    expect(screen.getByRole('alert')).toHaveTextContent('Сначала проверьте и подтвердите условия');
  });

  it('does not display candidates when the server returns the wrong matching mode', async () => {
    makeMocks();
    vi.spyOn(sessionApi, 'previewMatches').mockResolvedValue({
      catalog_version_id: catalog.version_id, mode: 'ml',
      requires_manager_confirmation: true, calibrated_no_match: false,
      candidates: [{
        sku: 'UNEXPECTED', name: 'Unknown', price_rub: '1.00',
        sale_unit: 'штука', pieces_per_sale_unit: 1, score: 0.99,
        status: 'compatible', reason_codes: [],
      }],
    });
    render(<TenantOperations
      organization={{ id: 'org-mode', name: 'Mode', role: 'editor' }}
      catalog={catalog} csrf="csrf-mode" onOrganizationChange={vi.fn()}
    />);
    fillPaperFacts();
    fireEvent.change(screen.getByLabelText('Наименование позиции'), {
      target: { value: 'Бумага' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Поиск по названию' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Получен результат другого способа поиска',
    );
    expect(screen.queryByLabelText('Выбранный товар'))
      .not.toBeInTheDocument();
  });


  it('offers explicit lexical candidate search without claiming ML confidence or approval', async () => {
    makeMocks();
    const preview = vi.spyOn(sessionApi, 'previewMatches').mockResolvedValue({
      catalog_version_id: catalog.version_id, mode: 'rapidfuzz',
      requires_manager_confirmation: true, calibrated_no_match: false,
      candidates: [{
        sku: 'PAPER-EXACT', name: 'Бумага офисная А4', price_rub: '199.00',
        sale_unit: 'пачка', pieces_per_sale_unit: 1, score: 0.84,
        status: 'compatible', reason_codes: [],
      }],
    });
    const create = vi.spyOn(sessionApi, 'createQuote');
    render(<TenantOperations
      organization={{ id: 'org-manual', name: 'Manual', role: 'editor' }}
      catalog={catalog} csrf="csrf-manual" onOrganizationChange={vi.fn()}
    />);
    fillPaperFacts();
    fireEvent.change(screen.getByLabelText('Наименование позиции'), {
      target: { value: 'Бумага офисная А4' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Поиск по названию' }));
    await waitFor(() => expect(preview).toHaveBeenCalledWith(
      'org-manual', catalog.version_id, expect.objectContaining({
        description: 'Бумага офисная А4',
      }), 'csrf-manual', undefined, 'rapidfuzz',
    ));
    expect(await screen.findByText(/сходство названия 84%/)).toBeInTheDocument();
    expect(screen.getByText(/по сходству названий/)).toBeInTheDocument();
    expect(screen.getByLabelText('Выбранный товар')).toHaveValue('');
    expect(create).not.toHaveBeenCalled();
  });

  it('binds ML candidate preview to confirmed brand rules and blocks unconfirmed edits', async () => {
    makeMocks();
    const text = 'Исключи бренд Acme.';
    const rules = {
      schema_version: 1 as const, markup_percent: '0', budget_max_rub: null,
      required_brand: null, excluded_brands: ['acme'],
    };
    vi.spyOn(sessionApi, 'previewQuoteInstructions').mockResolvedValue({
      raw_text: text, rules, unresolved_requirements: [], can_confirm: true,
    });
    const preview = vi.spyOn(sessionApi, 'previewMatches').mockResolvedValue({
      catalog_version_id: catalog.version_id, mode: 'ml',
      requires_manager_confirmation: true, calibrated_no_match: false,
      candidates: [],
    });
    render(<TenantOperations
      organization={{ id: 'org-brand', name: 'Brands', role: 'editor' }}
      catalog={catalog} csrf="csrf-brand" onOrganizationChange={vi.fn()}
    />);
    fillPaperFacts();
    fireEvent.change(screen.getByLabelText('Наименование позиции'), {
      target: { value: 'Бумага офисная A4' },
    });
    fireEvent.change(screen.getByLabelText('Условия подбора и расчёта'), {
      target: { value: text },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Подбор по характеристикам' }));
    expect(preview).not.toHaveBeenCalled();
    expect(screen.getByRole('alert')).toHaveTextContent('подтвердите условия подбора');
    fireEvent.click(screen.getByRole('button', { name: 'Проверить условия' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Подтвердить условия' }));
    fireEvent.click(screen.getByRole('button', { name: 'Подбор по характеристикам' }));
    await waitFor(() => expect(preview).toHaveBeenCalledWith(
      'org-brand', catalog.version_id, expect.objectContaining({
        description: 'Бумага офисная A4',
      }), 'csrf-brand', { raw_text: text, rules }, 'ml',
    ));
    fireEvent.change(screen.getByLabelText('Условия подбора и расчёта'), {
      target: { value: 'Исключи бренд Other.' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Подбор по характеристикам' }));
    expect(preview).toHaveBeenCalledTimes(1);
  });

  it('shows brand allow/deny restrictions returned by server before confirmation', async () => {
    makeMocks();
    const ruleText = 'Только бренд DemoPaper. Исключи бренд Acme.';
    const rules = {
      schema_version: 1 as const, markup_percent: '0', budget_max_rub: null,
      required_brand: 'demopaper', excluded_brands: ['acme'],
    };
    vi.spyOn(sessionApi, 'previewQuoteInstructions').mockResolvedValue({
      raw_text: ruleText, rules, unresolved_requirements: [], can_confirm: true,
    });
    render(<TenantOperations
      organization={{ id: 'org-brands', name: 'Brands', role: 'editor' }}
      catalog={catalog} csrf="csrf-brands" onOrganizationChange={vi.fn()}
    />);
    fireEvent.change(screen.getByLabelText('Условия подбора и расчёта'), {
      target: { value: ruleText },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Проверить условия' }));
    expect(await screen.findByText(/Только бренд: demopaper/)).toBeInTheDocument();
    expect(screen.getByText(/Исключены бренды: acme/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Подтвердить условия' }));
    expect(screen.getByText('Условия подтверждены для следующей версии КП.'))
      .toBeInTheDocument();
  });

  it('requires an explicitly confirmed server preview and persists the exact rules', async () => {
    makeMocks();
    vi.stubGlobal('crypto', { randomUUID: () => 'quote-rules-key' });
    const text = 'Наценка 18%. Бюджет до 300 рублей.';
    const rules = { schema_version: 1 as const, markup_percent: '18', budget_max_rub: '300' };
    const preview = vi.spyOn(sessionApi, 'previewQuoteInstructions')
      .mockResolvedValue({
        raw_text: text, rules, unresolved_requirements: [], can_confirm: true,
      });
    const create = vi.spyOn(sessionApi, 'createQuote').mockResolvedValue({
      quote_id: 'quote-rules', revision: 1, catalog_version_id: 'catalog-version-1',
      ready_for_draft: false, total_rub: null, base_total_rub: '296.61',
      estimated_total_rub: '350.00', budget_exceeded: true,
      instructions: { raw_text: text, rules }, lines: [],
      snapshot_sha256: 'snapshot', approved: false,
    });
    render(<TenantOperations
      organization={{ id: 'org-rules', name: 'Rules org', role: 'editor' }}
      catalog={catalog} csrf="csrf-rules" onOrganizationChange={vi.fn()}
    />);
    fillPaperFacts();
    fireEvent.change(screen.getByLabelText('Наименование позиции'), {
      target: { value: 'Бумага A4 80' },
    });
    fireEvent.change(screen.getByLabelText('Условия подбора и расчёта'), {
      target: { value: text },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Рассчитать и сохранить КП' }));
    expect(create).not.toHaveBeenCalled();
    expect(screen.getByRole('alert')).toHaveTextContent('подтвердите условия');

    fireEvent.click(screen.getByRole('button', { name: 'Проверить условия' }));
    await waitFor(() => expect(preview).toHaveBeenCalledWith('org-rules', text, 'csrf-rules'));
    fireEvent.click(screen.getByRole('button', { name: 'Подтвердить условия' }));
    fireEvent.click(screen.getByRole('button', { name: 'Рассчитать и сохранить КП' }));
    await waitFor(() => expect(create).toHaveBeenCalledTimes(1));
    expect(create.mock.calls[0]?.at(-1)).toEqual({ raw_text: text, rules });
    expect(await screen.findByText(/Превышен подтверждённый бюджет/)).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /PDF черновика/ })).not.toBeInTheDocument();
  });

  it('discards an old rules preview after switching tenant context', async () => {
    makeMocks();
    const text = 'Наценка 18%. Бюджет до 300 рублей.';
    const rules = { schema_version: 1 as const, markup_percent: '18', budget_max_rub: '300' };
    let resolvePreview!: (result: Awaited<ReturnType<typeof sessionApi.previewQuoteInstructions>>) => void;
    const delayedPreview = new Promise<Awaited<ReturnType<typeof sessionApi.previewQuoteInstructions>>>(
      resolve => { resolvePreview = resolve; },
    );
    const preview = vi.spyOn(sessionApi, 'previewQuoteInstructions')
      .mockReturnValueOnce(delayedPreview);
    const { rerender } = render(<TenantOperations
      organization={{ id: 'org-old', name: 'Old', role: 'editor' }}
      catalog={catalog} csrf="csrf-old" onOrganizationChange={vi.fn()}
    />);
    fireEvent.change(screen.getByLabelText('Условия подбора и расчёта'), {
      target: { value: text },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Проверить условия' }));
    await waitFor(() => expect(preview).toHaveBeenCalledWith('org-old', text, 'csrf-old'));

    rerender(<TenantOperations
      organization={{ id: 'org-new', name: 'New', role: 'editor' }}
      catalog={catalog} csrf="csrf-new" onOrganizationChange={vi.fn()}
    />);
    await waitFor(() => expect(screen.getByLabelText('Условия подбора и расчёта')).toHaveValue(''));
    await act(async () => {
      resolvePreview({ raw_text: text, rules, unresolved_requirements: [], can_confirm: true });
    });
    expect(screen.queryByRole('button', { name: 'Подтвердить условия' }))
      .not.toBeInTheDocument();
    expect(screen.getByLabelText('Условия подбора и расчёта')).toHaveValue('');
  });

  it('restores confirmed pricing rules when reopening an immutable quote revision', async () => {
    makeMocks();
    const rawText = 'Наценка 18%. Бюджет до 400 рублей.';
    const rules = { schema_version: 1 as const, markup_percent: '18', budget_max_rub: '400' };
    vi.spyOn(sessionApi, 'listQuotes').mockResolvedValue({
      items: [{
        quote_id: 'rules-quote', revision: 2, total_rub: '234.82',
        ready_for_draft: true,
      }],
    });
    vi.spyOn(sessionApi, 'getQuote').mockResolvedValue({
      quote_id: 'rules-quote', revision: 2, catalog_version_id: catalog.version_id,
      ready_for_draft: true, total_rub: '234.82',
      base_total_rub: '199.00', budget_exceeded: false,
      instructions: { raw_text: rawText, rules },
      lines: [], snapshot_sha256: 'sha', approved: false,
      source_request: null,
    });
    vi.spyOn(sessionApi, 'quoteReview').mockRejectedValue(
      new SessionApiError('quote_review_not_found', 'Not found', 404),
    );
    render(<TenantOperations
      organization={{ id: 'org-rules', name: 'Rules org', role: 'editor' }}
      catalog={catalog} csrf="csrf-rules" onOrganizationChange={vi.fn()}
    />);
    fireEvent.click(await screen.findByRole('button', { name: 'Открыть' }));
    await waitFor(() => {
      expect(screen.getByLabelText('Условия подбора и расчёта')).toHaveValue(rawText);
      expect(screen.getByText('Условия подтверждены для следующей версии КП.'))
        .toBeInTheDocument();
    });
    expect(screen.getByText(/Сумма по прайсу:/)).toBeInTheDocument();
  });
});

describe('A18 invitation and multiline safeguards', () => {
  it('uses a separate email-invitation workflow for an unregistered employee', async () => {
    makeMocks();
    const invitation = vi.spyOn(sessionApi, 'inviteByEmail')
      .mockResolvedValue({ status: 'accepted' });
    const original = vi.spyOn(sessionApi, 'invite');
    render(<TenantOperations
      organization={{ id: 'org-one', name: 'Acme', role: 'owner' }}
      catalog={catalog} csrf="csrf-owner" onOrganizationChange={vi.fn()}
    />);
    fireEvent.change(screen.getByLabelText('Электронная почта сотрудника'), {
      target: { value: 'NEW@EXAMPLE.TEST' },
    });
    fireEvent.click(screen.getByLabelText(
      'Новый сотрудник без аккаунта — отправить письмо',
    ));
    fireEvent.click(screen.getByRole('button', { name: /Пригласить/ }));
    await waitFor(() => expect(invitation).toHaveBeenCalledWith(
      'org-one', 'new@example.test', 'viewer', 'csrf-owner',
    ));
    expect(original).not.toHaveBeenCalled();
  });

  it('never permits more than 20 manually entered quote positions', async () => {
    makeMocks();
    render(<TenantOperations
      organization={{ id: 'org-one', name: 'Acme', role: 'editor' }}
      catalog={catalog} csrf="csrf-editor" onOrganizationChange={vi.fn()}
    />);
    for (let i = 0; i < 19; i++) {
      fireEvent.click(screen.getByRole('button', { name: /Добавить позицию/ }));
    }
    expect(screen.getAllByLabelText('Наименование позиции')).toHaveLength(20);
    expect(screen.getByRole('button', { name: /Добавить позицию/ })).toBeDisabled();
    fireEvent.click(screen.getAllByRole('button', { name: 'Удалить позицию' })[0]!);
    expect(screen.getAllByLabelText('Наименование позиции')).toHaveLength(19);
  });
});

describe('A19 authorized LLM extraction and per-line review', () => {
  it('does not call OpenRouter without explicit consent or CSRF', async () => {
    makeMocks();
    const extract = vi.spyOn(sessionApi, 'extractRequest');
    render(<TenantOperations
      organization={{ id: 'org-one', name: 'Acme', role: 'owner' }}
      catalog={catalog} csrf="csrf-19" onOrganizationChange={vi.fn()}
    />);
    fireEvent.change(screen.getByLabelText('Текст заявки (до 12 000 символов)'), {
      target: { value: 'Нужно 2 пачки бумаги А4' },
    });
    expect(screen.getByRole('button', { name: 'Распознать заявку' }))
      .toBeDisabled();
    const consent = screen.getByRole('checkbox', { name: /Разрешаю передать текст заявки/ });
    expect(consent).toHaveAccessibleName(/NVIDIA Nemotron/);
    expect(consent).toHaveAccessibleName(/OpenRouter/);
    expect(consent).not.toBeChecked();
    expect(extract).not.toHaveBeenCalled();
  });

  it.each(['openrouter', 'groq'] as const)('previews %s source-grounded rows; requires independent review before quote save', async sourceProvider => {
    makeMocks();
    const source = '2 пачки бумаги А4 80 г/м² и 1 пачка бумаги А4 80 г/м²';
    const extract = vi.spyOn(sessionApi, 'extractRequest').mockResolvedValue({
      extraction_id: '11111111-2222-4333-8444-555555555555',
      source: sourceProvider, approved: false, requires_human_review: true,
      extracted_request: {
        lines: [
          {
            source_quote: '2 пачки бумаги А4 80 г/м²',
            description: 'Бумага A4 80 г/м²',
            category: 'paper', quantity: 2, unit: 'пачка',
            brand: null, paper_format: 'A4', paper_gsm: 80,
            pen_ink_color: null, pen_ink_type: null, attributes: {},
          },
          {
            source_quote: '1 пачка бумаги А4 80 г/м²',
            description: 'Бумага A4 80 г/м²',
            category: 'paper', quantity: 1, unit: 'пачка',
            brand: null, paper_format: 'A4', paper_gsm: 80,
            pen_ink_color: null, pen_ink_type: null, attributes: {},
          },
        ],
      },
    });
    vi.stubGlobal('crypto', { randomUUID: () => 'a19-preview-quote-key' });
    const created = vi.spyOn(sessionApi, 'createQuote').mockResolvedValue({
      quote_id: 'q-19', revision: 1, catalog_version_id: 'catalog-version-1',
      ready_for_draft: true, total_rub: '1289.70', lines: [],
      snapshot_sha256: 'sha', approved: false,
    });
    const recorded = vi.spyOn(sessionApi, 'quoteReview').mockResolvedValue({
      reviewer_user_id: 'b944666b-2222-4333-8444-777777777777',
      reviewed_at: '2026-10-10T11:00:00Z',
      extraction_id: '11111111-2222-4333-8444-555555555555',
      source_sha256: 'a'.repeat(64), preview_sha256: 'b'.repeat(64),
      reviewed_request_sha256: 'c'.repeat(64),
      reviewed_line_hashes: ['d'.repeat(64), 'e'.repeat(64)],
    });
    render(<TenantOperations
      organization={{ id: 'org-one', name: 'Acme', role: 'editor' }}
      catalog={catalog} csrf="csrf-19" onOrganizationChange={vi.fn()}
    />);
    fireEvent.change(screen.getByLabelText('Текст заявки (до 12 000 символов)'), {
      target: { value: source },
    });
    fireEvent.click(screen.getByLabelText(
      /Разрешаю передать текст заявки/,
    ));
    fireEvent.click(screen.getByRole('button', { name: 'Распознать заявку' }));
    await waitFor(() => expect(extract).toHaveBeenCalledWith(
      'org-one', source, 'csrf-19', true,
    ));
    expect(await screen.findByText('1 пачка бумаги А4 80 г/м²')).toBeInTheDocument();
    const save = screen.getByRole('button', { name: 'Рассчитать и сохранить КП' });
    expect(save).toBeDisabled();
    expect(created).not.toHaveBeenCalled();
    const review = screen.getAllByLabelText('Проверил позицию по исходной заявке');
    expect(review).toHaveLength(2);
    fireEvent.click(review[0]!);
    expect(save).toBeDisabled();
    fireEvent.click(review[1]!);
    expect(save).toBeEnabled();
    fireEvent.click(save);
    await waitFor(() => expect(recorded).toHaveBeenCalledWith(
      'org-one', 'q-19', 1,
    ));
    await waitFor(() => expect(created).toHaveBeenCalledWith(
      'org-one', 'catalog-version-1', [
        expect.objectContaining({
          source_quote: '2 пачки бумаги А4 80 г/м²',
          description: 'Бумага A4 80 г/м²',
          quantity: 2, unit: 'пачка',
        }),
        expect.objectContaining({
          source_quote: '1 пачка бумаги А4 80 г/м²',
          quantity: 1, unit: 'пачка',
        }),
      ], 'csrf-19', 'a19-preview-quote-key',
      expect.objectContaining({
        extraction_id: '11111111-2222-4333-8444-555555555555',
        source_text: source,
        reviewed_lines: [true, true],
        original_extracted_request: expect.objectContaining({
          lines: expect.arrayContaining([
            expect.objectContaining({quantity: 2}),
            expect.objectContaining({quantity: 1}),
          ]),
        }),
      }),
    ));
  });
});

describe('A40 tenant ML matching and explicit SKU confirmation', () => {
  it('does not change the category before the manager confirms the ML suggestion', async () => {
    makeMocks();
    const classifier = vi.spyOn(sessionApi, 'suggestCategory').mockResolvedValue({
      original_category: 'paper', suggested_category: 'mouse_pad',
      best_category: 'mouse_pad', confidence: 0.86, margin: 0.17,
      abstained: false, requires_explicit_acceptance: true,
    });
    render(<TenantOperations
      organization={{ id: 'org-ml', name: 'ML', role: 'editor' }}
      catalog={catalog} csrf="csrf-ml" onOrganizationChange={vi.fn()}
    />);
    fireEvent.change(screen.getByLabelText('Наименование позиции'), {
      target: { value: 'Коврик для мыши' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Проверить категорию' }));
    await waitFor(() => expect(classifier).toHaveBeenCalledWith(
      'org-ml', 'Коврик для мыши', null, 'csrf-ml',
    ));
    expect((screen.getByLabelText('Категория') as HTMLSelectElement).value).toBe('unknown');
    fireEvent.click(await screen.findByRole('button', { name: 'Подтвердить категорию' }));
    expect((screen.getByLabelText('Категория') as HTMLSelectElement).value).toBe('mouse_pad');
    expect(screen.getByLabelText('Материал поверхности')).toBeInTheDocument();
  });

  it('sends only a manager-confirmed compatible SKU into the persisted quote', async () => {
    makeMocks();
    vi.stubGlobal('crypto', { randomUUID: () => 'ml-quote-key' });
    const matcher = vi.spyOn(sessionApi, 'previewMatches').mockResolvedValue({
      catalog_version_id: 'catalog-version-1', mode: 'ml',
      requires_manager_confirmation: true, calibrated_no_match: false,
      candidates: [
        {
          sku: 'SKU-B', name: 'Бумага А4 B', price_rub: '300.00',
          sale_unit: 'пачка', pieces_per_sale_unit: 500,
          score: 0.91, status: 'compatible', reason_codes: [],
        },
        {
          sku: 'SKU-C', name: 'Бумага без проверки', price_rub: '320.00',
          sale_unit: 'пачка', pieces_per_sale_unit: 500,
          score: 0.88, status: 'needs_review', reason_codes: ['paper_gsm_unknown'],
        },
      ],
    });
    const create = vi.spyOn(sessionApi, 'createQuote').mockResolvedValue({
      quote_id: 'saved-ml', revision: 1, catalog_version_id: 'catalog-version-1',
      ready_for_draft: true, total_rub: '600.00', lines: [],
      snapshot_sha256: 'sha', approved: false,
    });
    render(<TenantOperations
      organization={{ id: 'org-ml', name: 'ML', role: 'editor' }}
      catalog={catalog} csrf="csrf-ml" onOrganizationChange={vi.fn()}
    />);
    fillPaperFacts();
    fireEvent.change(screen.getByLabelText('Наименование позиции'), {
      target: { value: 'Бумага А4 80 г' },
    });
    fireEvent.change(screen.getByLabelText('Количество'), {
      target: { value: '2' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Подбор по характеристикам' }));
    await waitFor(() => expect(matcher).toHaveBeenCalledWith(
      'org-ml', 'catalog-version-1',
      expect.objectContaining({
        category: 'paper', description: 'Бумага А4 80 г', quantity: 2,
      }),
      'csrf-ml', undefined, 'ml',
    ));
    const select = await screen.findByLabelText('Выбранный товар');
    const rejected = [...(select as HTMLSelectElement).options].find(opt => opt.value === 'SKU-C');
    expect(rejected?.disabled).toBe(true);
    fireEvent.change(select, { target: { value: 'SKU-B' } });
    fireEvent.click(screen.getByRole('button', { name: 'Рассчитать и сохранить КП' }));
    await waitFor(() => expect(create).toHaveBeenCalledWith(
      'org-ml', 'catalog-version-1',
      [expect.objectContaining({ category: 'paper', quantity: 2 })],
      'csrf-ml', 'ml-quote-key', undefined,
      [{ line_number: 1, sku: 'SKU-B', approved_sale_quantity: null }],
    ));
  });
});

describe('A40 ML no-match and stale decision handling', () => {
  it('shows an empty ML candidate set without inventing a manager selection', async () => {
    makeMocks();
    const matcher = vi.spyOn(sessionApi, 'previewMatches').mockResolvedValue({
      catalog_version_id: catalog.version_id,
      mode: 'ml', calibrated_no_match: false,
      requires_manager_confirmation: true, candidates: [],
    });
    const create = vi.spyOn(sessionApi, 'createQuote').mockResolvedValue({
      quote_id: 'no-match-review', revision: 1,
      catalog_version_id: catalog.version_id,
      ready_for_draft: false, total_rub: null,
      snapshot_sha256: 'hash', approved: false,
      lines: [{
        line_number: 1, description: 'Несуществующий артикул',
        status: 'no_match', sku: null, product_name: null,
        requested_quantity: 1, requested_unit: 'пачка',
        sale_quantity: null, sale_unit: null, unit_price_rub: null,
        line_total_rub: null, reason_codes: ['no_compatible_sku'],
      }],
    });
    render(<TenantOperations
      organization={{ id: 'org-a40', name: 'Test', role: 'editor' }}
      catalog={catalog} csrf="csrf-a40" onOrganizationChange={vi.fn()}
    />);
    fillPaperFacts();
    fireEvent.change(screen.getByLabelText('Наименование позиции'), {
      target: { value: 'Несуществующий артикул' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Подбор по характеристикам' }));
    expect(await screen.findByText(/Не удалось подобрать товары по указанным требованиям/))
      .toBeInTheDocument();
    expect(screen.queryByLabelText('Выбранный товар'))
      .not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Рассчитать и сохранить КП' }));
    await waitFor(() => expect(create).toHaveBeenCalledOnce());
    expect(create.mock.calls[0]?.length).toBe(5);
    expect(await screen.findByText(/КП сохранено/)).toBeInTheDocument();
    expect(screen.getByText(/PDF недоступен, пока строки КП требуют проверки/))
      .toBeInTheDocument();
    expect(matcher).toHaveBeenCalledOnce();
  });

  it('invalidates selected SKU after changing the requested quantity', async () => {
    makeMocks();
    vi.spyOn(sessionApi, 'previewMatches').mockResolvedValue({
      catalog_version_id: catalog.version_id,
      mode: 'ml', calibrated_no_match: false,
      requires_manager_confirmation: true,
      candidates: [{
        sku: 'A-PAPER-001', name: 'Бумага А4', price_rub: '429.90',
        sale_unit: 'пачка', pieces_per_sale_unit: 1,
        score: 0.87, status: 'compatible', reason_codes: [],
      }],
    });
    const create = vi.spyOn(sessionApi, 'createQuote').mockResolvedValue({
      quote_id: 'new-revision', revision: 1,
      catalog_version_id: catalog.version_id,
      ready_for_draft: true, total_rub: '859.80',
      snapshot_sha256: 'hash', approved: false, lines: [],
    });
    render(<TenantOperations
      organization={{ id: 'org-a40', name: 'Test', role: 'editor' }}
      catalog={catalog} csrf="csrf-a40" onOrganizationChange={vi.fn()}
    />);
    fillPaperFacts();
    fireEvent.change(screen.getByLabelText('Наименование позиции'), {
      target: { value: 'Бумага А4 80' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Подбор по характеристикам' }));
    const skuPicker = await screen.findByLabelText('Выбранный товар');
    fireEvent.change(skuPicker, { target: { value: 'A-PAPER-001' } });
    fireEvent.change(screen.getByLabelText('Количество'), {
      target: { value: '2' },
    });
    expect(screen.queryByLabelText('Выбранный товар')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Рассчитать и сохранить КП' }));
    await waitFor(() => expect(create).toHaveBeenCalledOnce());
    expect(create.mock.calls[0]?.length).toBe(5);
  });
});

describe('A44 immutable PostgreSQL SKU decision attribution', () => {
  it('reads the server audit for the selected quote revision', async () => {
    makeMocks();
    vi.spyOn(sessionApi, 'listQuotes').mockResolvedValue({
      items: [{
        quote_id: 'quote-a44', revision: 2,
        total_rub: '600.00', ready_for_draft: true,
      }],
    });
    vi.spyOn(sessionApi, 'getQuote').mockResolvedValue({
      quote_id: 'quote-a44', revision: 2,
      catalog_version_id: 'catalog-version-1',
      ready_for_draft: true, total_rub: '600.00',
      snapshot_sha256: 'sha',
      approved: false,
      lines: [{
        line_number: 1, description: 'Бумага A4 80',
        status: 'manager_selected', sku: 'SKU-B', product_name: 'Бумага A4',
        requested_quantity: 2, requested_unit: 'пачка',
        sale_quantity: 2, sale_unit: 'пачка',
        unit_price_rub: '300.00', line_total_rub: '600.00', reason_codes: [],
      }],
    });
    vi.spyOn(sessionApi, 'quoteReview').mockRejectedValue(
      new Error('Not found'),
    );
    const journal = vi.spyOn(sessionApi, 'managerDecisions').mockResolvedValue({
      quote_id: 'quote-a44', revision: 2, items: [{
        line_number: 1, sku: 'SKU-B',
        catalog_version_id: 'catalog-version-1',
        approved_by_user_id: '12345678-0000-0000-0000-000000000000',
        recorded_at: '2026-10-22T19:00:00Z',
      }],
    });
    render(<TenantOperations
      organization={{ id: 'org-a44', name: 'Acme', role: 'editor' }}
      catalog={catalog} csrf="csrf-a44" onOrganizationChange={vi.fn()}
    />);
    fireEvent.click(await screen.findByRole('button', { name: 'Открыть' }));
    await waitFor(() => expect(journal).toHaveBeenCalledWith(
      'org-a44', 'quote-a44', 2,
    ));
    expect(await screen.findByText(/Подтвердил пользователь 12345678/))
      .toBeInTheDocument();
    expect(screen.getByText('История выбора товаров')).toBeInTheDocument();
  });
});

describe('A48 negative OpenRouter timeout and manual no-provider fallback', () => {
  it.each([
    ['provider_timeout', 504, 'Сервис ИИ не ответил вовремя.'],
    ['invalid_output', 502, 'Ответ ИИ не прошёл проверку.'],
    ['duplicate_inference_in_progress', 429, 'Повтор запроса к ИИ заблокирован.'],
  ])('preserves %s and requires explicit review of the source rows', async (code, status, message) => {
    makeMocks();
    const extract = vi.spyOn(sessionApi, 'extractRequest').mockRejectedValue(
      new SessionApiError(String(code), String(message), Number(status)),
    );
    const create = vi.spyOn(sessionApi, 'createQuote');
    render(<TenantOperations
      organization={{ id: 'org-fallback', name: 'Fallback', role: 'editor' }}
      catalog={catalog} csrf="csrf-fallback" onOrganizationChange={vi.fn()}
    />);
    fireEvent.change(screen.getByLabelText('Текст заявки (до 12 000 символов)'), {
      target: { value: 'Картридж 120XL\nБумага А4 80 г/м²' },
    });
    fireEvent.click(screen.getByLabelText(
      /Разрешаю передать текст заявки/,
    ));
    fireEvent.click(screen.getByRole('button', { name: 'Распознать заявку' }));
    await waitFor(() => expect(extract).toHaveBeenCalledTimes(1));
    expect(await screen.findByRole('alert')).toHaveTextContent(String(message));
    expect(screen.getByText(/Текст заявки разбит на строки для ручного заполнения\./))
      .toHaveAttribute('role', 'status');
    expect(screen.queryByText(/ИИ не распознавал заявку/)).not.toBeInTheDocument();
    expect(screen.getAllByLabelText('Наименование позиции')).toHaveLength(2);
    expect((screen.getAllByLabelText('Количество')[0] as HTMLInputElement).value)
      .toBe('0');
    expect((screen.getAllByLabelText('Категория')[0] as HTMLSelectElement).value)
      .toBe('unknown');
    expect(screen.getAllByLabelText('Проверил позицию по исходной заявке'))
      .toHaveLength(2);
    expect(screen.getByRole('button', { name: 'Рассчитать и сохранить КП' }))
      .toBeDisabled();
    expect(create).not.toHaveBeenCalled();
  });

  it('keeps the manual row limit visible alongside the provider failure', async () => {
    makeMocks();
    vi.spyOn(sessionApi, 'extractRequest').mockRejectedValue(
      new SessionApiError('provider_timeout', 'Сервис ИИ не ответил вовремя.', 504),
    );
    const create = vi.spyOn(sessionApi, 'createQuote');
    render(<TenantOperations organization={{ id: 'org-limit', name: 'Limit', role: 'editor' }}
      catalog={catalog} csrf="csrf-limit" onOrganizationChange={vi.fn()}/>);
    fireEvent.change(screen.getByLabelText('Текст заявки (до 12 000 символов)'), {
      target: { value: Array.from({ length: 21 }, (_, i) => 'Товар ' + i).join('\n') },
    });
    fireEvent.click(screen.getByLabelText(/Разрешаю передать текст заявки/));
    fireEvent.click(screen.getByRole('button', { name: 'Распознать заявку' }));
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Сервис ИИ не ответил вовремя.');
    expect(alert).toHaveTextContent('до 20 строк');
    expect(create).not.toHaveBeenCalled();
  });

  it('permits local preparation without consent or LLM network access', async () => {
    makeMocks();
    const extract = vi.spyOn(sessionApi, 'extractRequest');
    render(<TenantOperations
      organization={{ id: 'org-manual', name: 'Manual', role: 'editor' }}
      catalog={catalog} csrf="csrf-manual" onOrganizationChange={vi.fn()}
    />);
    fireEvent.change(screen.getByLabelText('Текст заявки (до 12 000 символов)'), {
      target: { value: 'Бумага А4 80 г/м²' },
    });
    fireEvent.click(screen.getByRole('button', {
      name: 'Разделить на позиции без ИИ',
    }));
    expect(screen.getByText(/без передачи текста во внешний ИИ/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Рассчитать и сохранить КП' }))
      .toBeDisabled();
    expect(extract).not.toHaveBeenCalled();
  });
});


describe('A60 stale quote reads cannot overwrite manual request review', () => {
  const stored = {
    quote_id: 'older-document',
    revision: 1,
    catalog_version_id: catalog.version_id,
    ready_for_draft: true,
    total_rub: '429.90',
    approved: false,
    snapshot_sha256: 'sha',
    lines: [{
      line_number: 1, description: 'Office paper',
      status: 'manager_selected',
      sku: 'E2E-PAPER-A', product_name: 'Office paper',
      requested_quantity: 1, requested_unit: 'пачка',
      sale_quantity: 1, sale_unit: 'пачка',
      unit_price_rub: '429.90', line_total_rub: '429.90', reason_codes: [],
    }],
  };

  it('does not lose pending saved-quote recovery when catalog first hydrates', async () => {
    makeMocks();
    const organization = { id: 'org-one', name: 'Acme', role: 'editor' as const };
    vi.spyOn(sessionApi, 'listQuotes').mockResolvedValue({
      items: [{ quote_id: stored.quote_id, revision: 1,
        total_rub: '429.90', ready_for_draft: true }],
    });
    const sourceLine: QuoteLineInput = {
      source_quote: 'Original vendor request', description: 'A4 office paper',
      category: 'paper', quantity: 2, unit: 'пачка',
      brand: null, paper_format: 'A4', paper_gsm: 80,
      pen_ink_color: null, pen_ink_type: null, attributes: {},
    };
    let resolveQuote!: (value: Awaited<ReturnType<typeof sessionApi.getQuote>>) => void;
    const getQuote = vi.spyOn(sessionApi, 'getQuote').mockReturnValue(
      new Promise(resolve => { resolveQuote = resolve; }),
    );
    vi.spyOn(sessionApi, 'quoteReview').mockRejectedValue(
      new SessionApiError('review_not_found', 'No audit', 404),
    );
    const view = render(<TenantOperations organization={organization}
      catalog={null} csrf="csrf-editor" onOrganizationChange={vi.fn()}/>);
    fireEvent.click(await screen.findByRole('button', { name: 'Открыть' }));
    expect(getQuote).toHaveBeenCalledTimes(1);
    // Real login loads the quote list before the active catalog. The second
    // network response must not invalidate the earlier persisted quote read.
    view.rerender(<TenantOperations organization={organization}
      catalog={catalog} csrf="csrf-editor" onOrganizationChange={vi.fn()}/>);
    await act(async () => resolveQuote({
      ...stored, source_request: { lines: [
        sourceLine,
        { ...sourceLine, description: 'More A4 paper', quantity: 3 },
      ] },
    }));
    await waitFor(() => expect(screen.getAllByLabelText('Наименование позиции'))
      .toHaveLength(2));
    const names = screen.getAllByLabelText('Наименование позиции');
    expect(names.map(node => (node as HTMLInputElement).value))
      .toEqual(['A4 office paper', 'More A4 paper']);
    expect(screen.getAllByLabelText('Проверил позицию по исходной заявке'))
      .toHaveLength(2);
    expect(screen.getAllByLabelText('Проверил позицию по исходной заявке')[0])
      .not.toBeChecked();
  });

  it('keeps both fallback rows when an earlier quote audit finishes late', async () => {
    makeMocks();
    vi.spyOn(sessionApi, 'listQuotes').mockResolvedValue({
      items: [{ quote_id: stored.quote_id, revision: 1,
        total_rub: '429.90', ready_for_draft: true }],
    });
    vi.spyOn(sessionApi, 'getQuote').mockResolvedValue(stored);
    let release!: (value: Awaited<ReturnType<typeof sessionApi.managerDecisions>>) => void;
    vi.spyOn(sessionApi, 'managerDecisions').mockReturnValue(new Promise(resolve => {
      release = resolve;
    }));
    vi.spyOn(sessionApi, 'quoteReview').mockRejectedValue(
      new SessionApiError('review_not_found', 'No review', 404),
    );
    render(<TenantOperations
      organization={{ id: 'org-one', name: 'Acme', role: 'editor' }}
      catalog={catalog} csrf="csrf-editor" onOrganizationChange={vi.fn()}
    />);
    fireEvent.click(await screen.findByRole('button', { name: 'Открыть' }));
    await screen.findByRole('heading', { name: 'КП · версия 1' });
    const textArea = screen.getByLabelText('Текст заявки (до 12 000 символов)');
    fireEvent.change(textArea, {
      target: { value: 'Первая строка\nВторая строка' },
    });
    fireEvent.click(screen.getByRole('button', {
      name: 'Разделить на позиции без ИИ',
    }));
    expect(screen.getAllByLabelText('Наименование позиции')).toHaveLength(2);
    expect(screen.getAllByLabelText('Проверил позицию по исходной заявке'))
      .toHaveLength(2);
    await act(async () => {
      release({ quote_id: stored.quote_id, revision: 1, items: [] });
    });
    await waitFor(() => {
      expect(screen.getAllByLabelText('Наименование позиции')).toHaveLength(2);
      expect(screen.queryByRole('heading', { name: 'КП · версия 1' }))
        .not.toBeInTheDocument();
    });
    expect(screen.getAllByLabelText('Наименование позиции')
      .map(input => (input as HTMLInputElement).value))
      .toEqual(['Первая строка', 'Вторая строка']);
  });

  it('ignores a quote read completing after the operator starts a new request', async () => {
    makeMocks();
    vi.spyOn(sessionApi, 'listQuotes').mockResolvedValue({
      items: [{ quote_id: stored.quote_id, revision: 1,
        total_rub: '429.90', ready_for_draft: true }],
    });
    let release!: (value: typeof stored) => void;
    vi.spyOn(sessionApi, 'getQuote').mockReturnValue(new Promise(resolve => {
      release = resolve;
    }));
    render(<TenantOperations
      organization={{ id: 'org-one', name: 'Acme', role: 'editor' }}
      catalog={catalog} csrf="csrf-editor" onOrganizationChange={vi.fn()}
    />);
    fireEvent.click(await screen.findByRole('button', { name: 'Открыть' }));
    fireEvent.change(screen.getByLabelText('Текст заявки (до 12 000 символов)'), {
      target: { value: 'Новая заявка\nВторая позиция' },
    });
    fireEvent.click(screen.getByRole('button', {
      name: 'Разделить на позиции без ИИ',
    }));
    await act(async () => {
      release(stored);
    });
    await waitFor(() => expect(screen.getAllByLabelText('Наименование позиции'))
      .toHaveLength(2));
    expect(screen.queryByRole('heading', { name: 'КП · версия 1' }))
      .not.toBeInTheDocument();
    expect(screen.getAllByLabelText('Наименование позиции')
      .map(input => (input as HTMLInputElement).value))
      .toEqual(['Новая заявка', 'Вторая позиция']);
  });
});

describe('A61 immutable quote input restoration', () => {
  it('restores every typed line but does not restore manager approvals or SKU selection', async () => {
    makeMocks();
    const quoteId = 'persisted-multi-line';
    const source_request: { lines: QuoteLineInput[] } = { lines: [{
      source_quote: 'Бумага A4 80 г/м², 2 пачки',
      description: 'Бумага A4 80 г/м²',
      category: 'paper', quantity: 2, unit: 'пачка',
      brand: 'Office',
      paper_format: 'A4', paper_gsm: 80,
      pen_ink_color: null, pen_ink_type: null,
      attributes: {},
    }, {
      source_quote: 'Ручка гелевая чёрная',
      description: 'Ручка гелевая чёрная',
      category: 'pen', quantity: 3, unit: 'штука',
      brand: null, paper_format: null, paper_gsm: null,
      pen_ink_color: 'black', pen_ink_type: 'gel',
      attributes: { tip_size_mm_x10: 5 },
    }] };
    vi.spyOn(sessionApi, 'listQuotes').mockResolvedValue({ items: [{
      quote_id: quoteId, revision: 2, total_rub: '850.00',
      ready_for_draft: true,
    }] });
    vi.spyOn(sessionApi, 'getQuote').mockResolvedValue({
      quote_id: quoteId, revision: 2, catalog_version_id: catalog.version_id,
      snapshot_sha256: 'signed', approved: false,
      ready_for_draft: true, total_rub: '850.00', source_request,
      lines: source_request.lines.map((line, index) => ({
        line_number: index + 1, description: line.description,
        status: 'manager_selected', sku: 'STORED-' + index,
        product_name: line.description,
        requested_quantity: line.quantity, requested_unit: line.unit,
        sale_quantity: line.quantity, sale_unit: line.unit,
        unit_price_rub: '100.00', line_total_rub: '300.00',
        reason_codes: [],
      })),
    });
    vi.spyOn(sessionApi, 'managerDecisions').mockResolvedValue({
      quote_id: quoteId, revision: 2, items: [],
    });
    vi.spyOn(sessionApi, 'quoteReview').mockRejectedValue(
      new SessionApiError('review_not_found', 'Missing', 404),
    );
    const revise = vi.spyOn(sessionApi, 'reviseQuote');
    render(<TenantOperations
      organization={{ id: 'org-one', name: 'Acme', role: 'editor' }}
      catalog={catalog} csrf="csrf" onOrganizationChange={vi.fn()}
    />);
    fireEvent.click(await screen.findByRole('button', { name: 'Открыть' }));
    await waitFor(() => expect(screen.getAllByLabelText('Наименование позиции'))
      .toHaveLength(2));
    const descriptions = screen.getAllByLabelText('Наименование позиции');
    expect(descriptions.map(input => (input as HTMLInputElement).value))
      .toEqual(source_request.lines.map(line => line.description));
    expect(screen.getAllByLabelText('Количество').map(input =>
      (input as HTMLInputElement).value)).toEqual(['2', '3']);
    expect(screen.getByLabelText('Плотность бумаги, г/м²')).toHaveValue(80);
    expect(screen.getByLabelText('Цвет чернил')).toHaveValue('black');
    expect(screen.getByLabelText('Тип чернил')).toHaveValue('gel');
    expect(screen.getAllByLabelText('Проверил позицию по исходной заявке'))
      .toHaveLength(2);
    expect(screen.getAllByLabelText('Проверил позицию по исходной заявке')
      .every(input => !(input as HTMLInputElement).checked)).toBe(true);
    expect(screen.queryByLabelText('Выбранный товар'))
      .not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Создать следующую версию' }))
      .toBeDisabled();
    expect(screen.getByText(/предыдущие решения не переносятся автоматически/))
      .toBeInTheDocument();
    expect(revise).not.toHaveBeenCalled();
  });

  it('fails closed for legacy revisions lacking the source input', async () => {
    makeMocks();
    vi.spyOn(sessionApi, 'listQuotes').mockResolvedValue({ items: [{
      quote_id: 'legacy', revision: 1, total_rub: '100.00',
      ready_for_draft: true,
    }] });
    vi.spyOn(sessionApi, 'getQuote').mockResolvedValue({
      quote_id: 'legacy', revision: 1,
      catalog_version_id: catalog.version_id, snapshot_sha256: 'legacy',
      approved: false, ready_for_draft: true, total_rub: '100.00',
      lines: [],
    });
    vi.spyOn(sessionApi, 'quoteReview').mockRejectedValue(
      new SessionApiError('review_not_found', 'Missing', 404),
    );
    render(<TenantOperations
      organization={{ id: 'org-one', name: 'Acme', role: 'editor' }}
      catalog={catalog} csrf="csrf" onOrganizationChange={vi.fn()}
    />);
    fireEvent.click(await screen.findByRole('button', { name: 'Открыть' }));
    await screen.findByText(/нет сохранённых исходных характеристик/);
    expect(screen.getAllByLabelText('Наименование позиции')).toHaveLength(1);
    expect(screen.getByLabelText('Наименование позиции')).toHaveValue('');
    expect(screen.queryByLabelText('Выбранный товар'))
      .not.toBeInTheDocument();
  });
});


describe('A62 conflict-safe revision editing', () => {
  it.each([
    ['stale_revision', /уже изменено другим сотрудником/],
    ['stale_catalog', /Каталог изменился/],
  ])('retains unsaved editor data and explains %s conflict', async (code, message) => {
    makeMocks();
    vi.stubGlobal('crypto', { randomUUID: () => 'a62-unique-request' });
    vi.spyOn(sessionApi, 'listQuotes').mockResolvedValue({ items: [{
      quote_id: 'a62-existing-quote', revision: 2,
      ready_for_draft: false, total_rub: null,
    }] });
    vi.spyOn(sessionApi, 'getQuote').mockResolvedValue({
      quote_id: 'a62-existing-quote', revision: 2,
      catalog_version_id: catalog.version_id, snapshot_sha256: 'original',
      ready_for_draft: false, total_rub: null, approved: false, lines: [],
      source_request: { lines: [{
        source_quote: '2 пачки бумаги A4',
        description: 'Бумага A4 для офиса',
        category: 'paper', quantity: 2, unit: 'пачка',
        brand: null, paper_format: 'A4', paper_gsm: 80,
        pen_ink_color: null, pen_ink_type: null, attributes: {},
      }] },
    });
    vi.spyOn(sessionApi, 'quoteReview').mockRejectedValue(
      new SessionApiError('review_not_found', 'Missing', 404),
    );
    const revise = vi.spyOn(sessionApi, 'reviseQuote')
      .mockRejectedValue(new SessionApiError(code, 'Conflicting revision', 409));
    render(<TenantOperations
      organization={{ id: 'org-one', name: 'Acme', role: 'editor' }}
      catalog={catalog} csrf="csrf-editor" onOrganizationChange={vi.fn()}
    />);
    fireEvent.click(await screen.findByRole('button', { name: 'Открыть' }));
    await waitFor(() => expect(screen.getByLabelText('Наименование позиции'))
      .toHaveValue('Бумага A4 для офиса'));
    fireEvent.click(screen.getByLabelText('Проверил позицию по исходной заявке'));
    const button = screen.getByRole('button', { name: 'Создать следующую версию' });
    expect(button).toBeEnabled();
    fireEvent.click(button);
    await waitFor(() => expect(revise).toHaveBeenCalledTimes(1));
    expect(await screen.findByRole('alert')).toHaveTextContent(message);
    expect(screen.getByLabelText('Наименование позиции'))
      .toHaveValue('Бумага A4 для офиса');
    expect(screen.getByRole('heading', { name: 'КП · версия 2' }))
      .toBeInTheDocument();
    expect(screen.queryByText(/КП сохранено/)).not.toBeInTheDocument();
    expect(revise.mock.calls[0]?.[2]).toBe(2);
    expect(revise.mock.calls[0]?.[3]).toBe(catalog.version_id);
    expect(revise.mock.calls[0]?.[4]).toEqual([
      expect.objectContaining({ description: 'Бумага A4 для офиса', quantity: 2 }),
    ]);
  });
});


describe('A63 published catalog invalidates stale manager decisions', () => {

  it('does not mark an untouched new form as reviewed LLM input on first catalog publication', async () => {
    makeMocks();
    vi.stubGlobal('crypto', { randomUUID: () => 'first-catalog-quote' });
    const create = vi.spyOn(sessionApi, 'createQuote').mockResolvedValue({
      quote_id: 'new-quote', revision: 1, catalog_version_id: catalog.version_id,
      ready_for_draft: false, total_rub: null, lines: [],
      snapshot_sha256: 'new', approved: false,
    });
    const props = {
      organization: { id: 'org-one', name: 'Acme', role: 'editor' as const },
      csrf: 'csrf-test', onOrganizationChange: vi.fn(),
    };
    const view = render(<TenantOperations {...props} catalog={null}/>);
    fillPaperFacts();
    view.rerender(<TenantOperations {...props} catalog={catalog}/>);
    expect(screen.queryByLabelText('Проверил позицию по исходной заявке'))
      .not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Наименование позиции'), {
      target: { value: 'Новая бумага А4' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Рассчитать и сохранить КП' }));
    await waitFor(() => expect(create).toHaveBeenCalledTimes(1));
    expect(create.mock.calls[0]?.[1]).toBe(catalog.version_id);
  });

  it('refreshes active catalog, revokes old SKU preview and requires new human review', async () => {
    makeMocks();
    vi.stubGlobal('crypto', { randomUUID: () => 'a63-catalog-conflict-key' });
    const nextCatalog = {
      version_id: 'catalog-version-2', source_sha256: 'hash-v2', rows_count: 2,
    };
    const refresh = vi.fn().mockResolvedValue(nextCatalog);
    vi.spyOn(sessionApi, 'listQuotes').mockResolvedValue({ items: [{
      quote_id: 'a63-quote', revision: 3, total_rub: '950.00',
      ready_for_draft: true,
    }] });
    vi.spyOn(sessionApi, 'getQuote').mockResolvedValue({
      quote_id: 'a63-quote', revision: 3,
      catalog_version_id: catalog.version_id,
      snapshot_sha256: 'sha', approved: false,
      ready_for_draft: true, total_rub: '950.00', lines: [],
      source_request: { lines: [{
        source_quote: 'Бумага 80 г/м²',
        description: 'Бумага А4 80',
        category: 'paper', quantity: 2, unit: 'пачка',
        brand: null, paper_format: 'A4', paper_gsm: 80,
        pen_ink_color: null, pen_ink_type: null, attributes: {},
      }] },
    });
    vi.spyOn(sessionApi, 'quoteReview').mockRejectedValue(
      new SessionApiError('review_not_found', 'Missing', 404),
    );
    const preview = vi.spyOn(sessionApi, 'previewMatches').mockResolvedValue({
      catalog_version_id: catalog.version_id, mode: 'ml',
      requires_manager_confirmation: true, calibrated_no_match: false,
      candidates: [{
        sku: 'OLD-PAPER', name: 'Old catalog price',
        price_rub: '950.00', sale_unit: 'пачка', pieces_per_sale_unit: 1,
        score: 0.96, status: 'compatible', reason_codes: [],
      }],
    });
    const revise = vi.spyOn(sessionApi, 'reviseQuote')
      .mockRejectedValue(new SessionApiError('stale_catalog', 'Old catalog', 409));
    const common = {
      organization: { id: 'org-one', name: 'Acme', role: 'editor' as const },
      csrf: 'csrf-test', onOrganizationChange: vi.fn(),
      onCatalogRefresh: refresh,
    };
    const view = render(<TenantOperations {...common} catalog={catalog}/>);
    fireEvent.click(await screen.findByRole('button', { name: 'Открыть' }));
    await waitFor(() => expect(screen.getByLabelText('Наименование позиции'))
      .toHaveValue('Бумага А4 80'));
    fireEvent.click(screen.getByRole('button', { name: 'Подбор по характеристикам' }));
    await screen.findByLabelText('Выбранный товар');
    fireEvent.change(screen.getByLabelText('Выбранный товар'), {
      target: { value: 'OLD-PAPER' },
    });
    fireEvent.click(screen.getByLabelText('Проверил позицию по исходной заявке'));
    fireEvent.click(screen.getByRole('button', { name: 'Создать следующую версию' }));
    await waitFor(() => expect(revise).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(refresh).toHaveBeenCalledTimes(1));
    expect(screen.getByLabelText('Наименование позиции')).toHaveValue('Бумага А4 80');
    expect(screen.queryByLabelText('Выбранный товар'))
      .not.toBeInTheDocument();
    expect(screen.getByLabelText('Проверил позицию по исходной заявке'))
      .not.toBeChecked();
    expect(screen.getByRole('button', { name: 'Создать следующую версию' }))
      .toBeDisabled();
    expect(screen.getByRole('button', { name: 'Подбор по характеристикам' })).toBeDisabled();
    view.rerender(<TenantOperations {...common} catalog={nextCatalog}/>);
    await waitFor(() => expect(screen.getByRole('button', { name: 'Подбор по характеристикам' }))
      .toBeEnabled());
    expect(screen.getByRole('button', { name: 'Создать следующую версию' }))
      .toBeDisabled();
    expect(screen.getByLabelText('Проверил позицию по исходной заявке'))
      .not.toBeChecked();
    fireEvent.click(screen.getByRole('button', { name: 'Подбор по характеристикам' }));
    await waitFor(() => expect(preview).toHaveBeenLastCalledWith(
      'org-one', nextCatalog.version_id, expect.objectContaining({
        description: 'Бумага А4 80',
      }), 'csrf-test', undefined, 'ml',
    ));
    expect(revise).toHaveBeenCalledTimes(1);
  });
});

describe('A64 delayed ML results and tenant context isolation', () => {
  const proposal = (versionId: string) => ({
    catalog_version_id: versionId,
    mode: 'ml' as const,
    calibrated_no_match: false as const,
    requires_manager_confirmation: true as const,
    candidates: [{
      sku: 'SKU-A64', name: 'Бумага A4', price_rub: '450.00',
      sale_unit: 'пачка' as const, pieces_per_sale_unit: 1,
      score: 0.94, status: 'compatible' as const, reason_codes: [],
    }],
  });

  it('discards pending SKU candidates from a catalog replaced in another tab', async () => {
    makeMocks();
    let release!: (value: ReturnType<typeof proposal>) => void;
    const matcher = vi.spyOn(sessionApi, 'previewMatches')
      .mockImplementationOnce(() => new Promise(resolve => { release = resolve; }))
      .mockResolvedValueOnce(proposal('catalog-version-2'));
    const props = {
      organization: { id: 'org-one', name: 'Acme', role: 'editor' as const },
      csrf: 'csrf-one', onOrganizationChange: vi.fn(),
    };
    const view = render(<TenantOperations {...props} catalog={catalog}/>);
    fillPaperFacts();
    fireEvent.change(screen.getByLabelText('Наименование позиции'), {
      target: { value: 'Бумага A4, 80 г' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Подбор по характеристикам' }));
    await waitFor(() => expect(matcher).toHaveBeenCalledTimes(1));
    view.rerender(<TenantOperations {...props} catalog={{
      ...catalog, version_id: 'catalog-version-2',
    }}/>);
    await act(async () => { release(proposal(catalog.version_id)); });
    expect(screen.queryByLabelText('Выбранный товар'))
      .not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Рассчитать и сохранить КП' }))
      .toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Подбор по характеристикам' }));
    await waitFor(() => expect(matcher).toHaveBeenCalledTimes(2));
    expect(matcher.mock.calls[1]?.[1]).toBe('catalog-version-2');
    expect(await screen.findByLabelText('Выбранный товар'))
      .toBeInTheDocument();
  });

  it('discards the ML result when manager edits the request while inference runs', async () => {
    makeMocks();
    let release!: (value: ReturnType<typeof proposal>) => void;
    vi.spyOn(sessionApi, 'previewMatches').mockImplementationOnce(
      () => new Promise(resolve => { release = resolve; }),
    );
    render(<TenantOperations
      organization={{ id: 'org-edit', name: 'Acme', role: 'editor' }}
      catalog={catalog} csrf="csrf-edit" onOrganizationChange={vi.fn()}
    />);
    fillPaperFacts();
    fireEvent.change(screen.getByLabelText('Наименование позиции'), {
      target: { value: 'Бумага A4' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Подбор по характеристикам' }));
    fireEvent.change(screen.getByLabelText('Наименование позиции'), {
      target: { value: 'Картридж XL' },
    });
    await act(async () => { release(proposal(catalog.version_id)); });
    expect(screen.getByLabelText('Наименование позиции')).toHaveValue('Картридж XL');
    expect(screen.queryByLabelText('Выбранный товар'))
      .not.toBeInTheDocument();
  });

  it('does not apply an old tenant result to a newly selected organization', async () => {
    makeMocks();
    let release!: (value: ReturnType<typeof proposal>) => void;
    const matcher = vi.spyOn(sessionApi, 'previewMatches')
      .mockImplementationOnce(() => new Promise(resolve => { release = resolve; }))
      .mockResolvedValueOnce(proposal(catalog.version_id));
    const previous = { id: 'org-left', name: 'Left', role: 'editor' as const };
    const next = { id: 'org-right', name: 'Right', role: 'editor' as const };
    const view = render(<TenantOperations organization={previous}
      catalog={catalog} csrf="csrf-left" onOrganizationChange={vi.fn()}/>);
    fillPaperFacts();
    fireEvent.change(screen.getByLabelText('Наименование позиции'), {
      target: { value: 'Бумага старая' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Подбор по характеристикам' }));
    await waitFor(() => expect(matcher).toHaveBeenCalledTimes(1));
    view.rerender(<TenantOperations organization={next}
      catalog={catalog} csrf="csrf-right" onOrganizationChange={vi.fn()}/>);
    await act(async () => { release(proposal(catalog.version_id)); });
    expect(screen.queryByLabelText('Выбранный товар'))
      .not.toBeInTheDocument();
    expect(screen.getByLabelText('Наименование позиции')).toHaveValue('');
    fireEvent.change(screen.getByLabelText('Наименование позиции'), {
      target: { value: 'Бумага новая' },
    });
    fillPaperFacts();
    fireEvent.click(screen.getByRole('button', { name: 'Подбор по характеристикам' }));
    await waitFor(() => expect(matcher).toHaveBeenCalledTimes(2));
    expect(matcher.mock.calls[1]?.[0]).toBe('org-right');
    expect(matcher.mock.calls[1]?.[3]).toBe('csrf-right');
  });

  it('blocks a preview whose server catalog ID differs from the active catalog', async () => {
    makeMocks();
    vi.spyOn(sessionApi, 'previewMatches')
      .mockResolvedValue(proposal('unexpected-catalog-version'));
    const create = vi.spyOn(sessionApi, 'createQuote');
    render(<TenantOperations
      organization={{ id: 'org-one', name: 'Acme', role: 'editor' }}
      catalog={catalog} csrf="csrf-one" onOrganizationChange={vi.fn()}
    />);
    fillPaperFacts();
    fireEvent.change(screen.getByLabelText('Наименование позиции'), {
      target: { value: 'Бумага A4' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Подбор по характеристикам' }));
    expect(await screen.findByRole('alert'))
      .toHaveTextContent('Найденные товары относятся к другому прайсу');
    expect(screen.getByRole('button', { name: 'Подбор по характеристикам' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Рассчитать и сохранить КП' }))
      .toBeDisabled();
    expect(create).not.toHaveBeenCalled();
  });

  it('does not display a category suggestion after its source text changes', async () => {
    makeMocks();
    let release!: (value: Awaited<ReturnType<typeof sessionApi.suggestCategory>>) => void;
    vi.spyOn(sessionApi, 'suggestCategory').mockImplementationOnce(
      () => new Promise(resolve => { release = resolve; }),
    );
    render(<TenantOperations
      organization={{ id: 'org-one', name: 'Acme', role: 'editor' }}
      catalog={catalog} csrf="csrf-one" onOrganizationChange={vi.fn()}
    />);
    fireEvent.change(screen.getByLabelText('Наименование позиции'), {
      target: { value: 'Коврик для мыши' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Проверить категорию' }));
    fireEvent.change(screen.getByLabelText('Наименование позиции'), {
      target: { value: 'Бумага A4' },
    });
    await act(async () => {
      release({
        original_category: 'paper', suggested_category: 'mouse_pad',
        best_category: 'mouse_pad', confidence: 0.9, margin: 0.2,
        abstained: false, requires_explicit_acceptance: true,
      });
    });
    expect(screen.queryByRole('button', { name: 'Подтвердить категорию' }))
      .not.toBeInTheDocument();
    expect(screen.getByLabelText('Наименование позиции')).toHaveValue('Бумага A4');
  });
});

describe('A64 delayed quote persistence response', () => {
  it('never shows another organization a quote completed after switching tenants', async () => {
    makeMocks();
    vi.stubGlobal('crypto', { randomUUID: () => 'a64-quote-save' });
    let release!: (value: Awaited<ReturnType<typeof sessionApi.createQuote>>) => void;
    const save = vi.spyOn(sessionApi, 'createQuote').mockImplementationOnce(
      () => new Promise(resolve => { release = resolve; }),
    );
    const view = render(<TenantOperations
      organization={{ id: 'org-a', name: 'Old', role: 'editor' }}
      catalog={catalog} csrf="csrf-a" onOrganizationChange={vi.fn()}/>);
    fillPaperFacts();
    fireEvent.change(screen.getByLabelText('Наименование позиции'), {
      target: { value: 'Бумага из старой организации' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Рассчитать и сохранить КП' }));
    await waitFor(() => expect(save).toHaveBeenCalledWith(
      'org-a', catalog.version_id,
      [expect.objectContaining({ description: 'Бумага из старой организации' })],
      'csrf-a', 'a64-quote-save',
    ));
    view.rerender(<TenantOperations
      organization={{ id: 'org-b', name: 'New', role: 'editor' }}
      catalog={catalog} csrf="csrf-b" onOrganizationChange={vi.fn()}/>);
    await act(async () => {
      release({
        quote_id: 'old-tenant-document', revision: 1,
        catalog_version_id: catalog.version_id, ready_for_draft: true,
        total_rub: '450.00', lines: [], snapshot_sha256: 'old',
        approved: false,
      });
    });
    expect(screen.queryByRole('heading', { name: /КП · версия/ })).not.toBeInTheDocument();
    expect(screen.queryByText(/КП сохранено/)).not.toBeInTheDocument();
    expect(screen.getByLabelText('Наименование позиции')).toHaveValue('');
    expect(screen.getByRole('button', { name: 'Рассчитать и сохранить КП' }))
      .toBeEnabled();
    fireEvent.click(screen.getByRole('button', { name: 'Рассчитать и сохранить КП' }));
    expect(save).toHaveBeenCalledTimes(1);
  });
});

describe('A65 historical quote review and revision-pinned PDF', () => {
  const latest = {
    quote_id: 'persisted-history', revision: 3,
    catalog_version_id: 'catalog-v3', ready_for_draft: true,
    total_rub: '2051.50', snapshot_sha256: 'new-sha', approved: false,
    lines: [{
      line_number: 1, description: 'Бумага в новом прайсе',
      sku: 'PAPER-NEW', product_name: 'Бумага', status: 'manager_selected',
      requested_quantity: 2, requested_unit: 'пачка',
      sale_quantity: 2, sale_unit: 'пачка', unit_price_rub: '1025.75',
      line_total_rub: '2051.50', reason_codes: [],
    }],
  };
  const first = {
    ...latest, revision: 1, catalog_version_id: 'catalog-v1',
    total_rub: '900.00', snapshot_sha256: 'old-sha',
    lines: [{ ...latest.lines[0]!, description: 'Бумага по старому прайсу',
      sku: 'PAPER-OLD', unit_price_rub: '450.00',
      line_total_rub: '900.00' }],
  };
  const org = { id: 'org-a65', name: 'Supplier', role: 'viewer' as const };

  it('shows an older immutable revision without changing the current quote or its PDF', async () => {
    makeMocks();
    vi.spyOn(sessionApi, 'listQuotes').mockResolvedValue({
      items: [{ quote_id: latest.quote_id, revision: 3,
        ready_for_draft: true, total_rub: '2051.50' }],
    });
    const fetchQuote = vi.spyOn(sessionApi, 'getQuote')
      .mockImplementation(async (_org, _id, revision) =>
        revision === 1 ? first : latest);
    render(<TenantOperations organization={org}
      catalog={catalog} csrf="csrf-a65" onOrganizationChange={vi.fn()}/>);
    fireEvent.click(await screen.findByRole('button', { name: 'Открыть' }));
    expect(await screen.findByRole('heading', { name: 'КП · версия 3' }))
      .toBeInTheDocument();
    const currentPdf = screen.getByRole('link', { name: 'PDF черновика' });
    expect(currentPdf).toHaveAttribute('href',
      '/api/session/organizations/org-a65/quotes/persisted-history/pdf?revision=3');
    fireEvent.click(screen.getByRole('button', { name: 'Открыть предыдущую версию' }));
    await waitFor(() => expect(fetchQuote).toHaveBeenCalledWith(
      'org-a65', latest.quote_id, 1,
    ));
    const history = await screen.findByRole('region', { name: 'Предыдущая версия КП' });
    expect(history).toHaveTextContent('Версия 1 — только просмотр');
    expect(history).toHaveTextContent('900.00');
    expect(history).toHaveTextContent('PAPER-OLD');
    expect(history).toHaveTextContent('catalog-v1');
    expect(screen.getByRole('heading', { name: 'КП · версия 3' }))
      .toBeInTheDocument();
    expect(currentPdf).toHaveAttribute('href',
      '/api/session/organizations/org-a65/quotes/persisted-history/pdf?revision=3');
    expect(screen.getByRole('link', { name: 'PDF версии 1' }))
      .toHaveAttribute('href',
        '/api/session/organizations/org-a65/quotes/persisted-history/pdf?revision=1');
    expect(screen.queryByRole('button', { name: 'Рассчитать и сохранить КП' }))
      .not.toBeInTheDocument();
  });

  it('rejects a mismatched historical response and never exposes its PDF', async () => {
    makeMocks();
    vi.spyOn(sessionApi, 'listQuotes').mockResolvedValue({
      items: [{ quote_id: latest.quote_id, revision: 3,
        ready_for_draft: true, total_rub: '2051.50' }],
    });
    vi.spyOn(sessionApi, 'getQuote').mockImplementation(async (_org, _id, revision) =>
      revision === 1 ? { ...first, quote_id: 'foreign-document' } : latest);
    render(<TenantOperations organization={org}
      catalog={catalog} csrf="csrf-a65" onOrganizationChange={vi.fn()}/>);
    fireEvent.click(await screen.findByRole('button', { name: 'Открыть' }));
    await screen.findByRole('heading', { name: 'КП · версия 3' });
    fireEvent.click(screen.getByRole('button', { name: 'Открыть предыдущую версию' }));
    expect(await screen.findByRole('alert'))
      .toHaveTextContent('Получена другая версия КП');
    expect(screen.queryByRole('region', { name: 'Предыдущая версия КП' }))
      .not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'PDF версии 1' }))
      .not.toBeInTheDocument();
  });

  it('does not allow a PDF download for an incomplete historical revision', async () => {
    makeMocks();
    vi.spyOn(sessionApi, 'listQuotes').mockResolvedValue({
      items: [{ quote_id: latest.quote_id, revision: 3,
        ready_for_draft: true, total_rub: '2051.50' }],
    });
    vi.spyOn(sessionApi, 'getQuote').mockImplementation(async (_org, _id, revision) =>
      revision === 1
        ? { ...first, ready_for_draft: false, total_rub: null,
            lines: [{ ...first.lines[0]!, sku: null, status: 'no_match',
              unit_price_rub: null, line_total_rub: null }] }
        : latest);
    render(<TenantOperations organization={org}
      catalog={catalog} csrf="csrf-a65" onOrganizationChange={vi.fn()}/>);
    fireEvent.click(await screen.findByRole('button', { name: 'Открыть' }));
    await screen.findByRole('heading', { name: 'КП · версия 3' });
    fireEvent.click(screen.getByRole('button', { name: 'Открыть предыдущую версию' }));
    const history = await screen.findByRole('region', { name: 'Предыдущая версия КП' });
    expect(history).toHaveTextContent('Для этой версии PDF недоступен');
    expect(screen.queryByRole('link', { name: 'PDF версии 1' }))
      .not.toBeInTheDocument();
  });
});


describe('request completeness in the tenant editor', () => {
  const analysis = {
    ready_for_matching: false, questions_text: 'Строка 1 — уточните плотность.',
    scope_notice: 'Проверены поля поддерживаемых категорий.',
    lines: [{ line_number: 1, source_quote: 'Бумага', description: 'Бумага',
      missing_fields: ['paper_gsm'], questions: ['Уточните плотность.'] }],
  };
  const setup = () => {
    makeMocks();
    render(<TenantOperations organization={{ id: 'org-analysis', name: 'Analysis', role: 'editor' }}
      catalog={null} csrf="csrf-analysis" onOrganizationChange={vi.fn()}/>);
    fillPaperFacts();
    fireEvent.change(screen.getByLabelText('Наименование позиции'), { target: { value: 'Бумага' } });
    fireEvent.change(screen.getByLabelText('Плотность бумаги, г/м²'), { target: { value: '' } });
  };
  it('analyzes incomplete input without a published catalog and clears it after an edit', async () => {
    const analyze = vi.spyOn(sessionApi, 'analyzeRequest').mockResolvedValue(analysis);
    setup();
    fireEvent.click(screen.getByRole('button', { name: 'Проверить полноту заявки' }));
    expect(await screen.findByLabelText('Вопросы для клиента')).toHaveValue(analysis.questions_text);
    expect(analyze).toHaveBeenCalledWith('org-analysis', [expect.objectContaining({
      paper_gsm: null, description: 'Бумага',
    })], 'csrf-analysis');
    fireEvent.change(screen.getByLabelText('Плотность бумаги, г/м²'), { target: { value: '80' } });
    expect(screen.queryByLabelText('Вопросы для клиента')).not.toBeInTheDocument();
  });
  it('discards analysis completed after the manager changes a requirement', async () => {
    let resolve!: (value: typeof analysis) => void;
    vi.spyOn(sessionApi, 'analyzeRequest').mockImplementation(() => new Promise(done => { resolve = done; }));
    setup();
    fireEvent.click(screen.getByRole('button', { name: 'Проверить полноту заявки' }));
    fireEvent.change(screen.getByLabelText('Наименование позиции'), { target: { value: 'Другой товар' } });
    await act(async () => { resolve(analysis); });
    expect(screen.queryByLabelText('Вопросы для клиента')).not.toBeInTheDocument();
  });
  it.each([null, 'paper'])('shows automatic extraction analysis after normalization, category=%s', async category => {
    makeMocks();
    vi.spyOn(sessionApi, 'extractRequest').mockResolvedValue({
      extraction_id: 'normalized-preview', source: 'groq', requires_human_review: true, approved: false,
      extracted_request: { lines: [{
        source_quote: 'Бумага A4 80', description: '  Бумага  ', category,
        quantity: 2, unit: 'пачка', brand: '  Acme  ', paper_format: ' A4 ',
        paper_gsm: 80, pen_ink_color: null, pen_ink_type: null, attributes: {},
      }] }, analysis,
    });
    const recheck = vi.spyOn(sessionApi, 'analyzeRequest');
    render(<TenantOperations organization={{ id: 'org-auto-analysis', name: 'Auto', role: 'editor' }}
      catalog={null} csrf="csrf-auto-analysis" onOrganizationChange={vi.fn()}/>);
    fireEvent.change(screen.getByLabelText('Текст заявки (до 12 000 символов)'), {
      target: { value: 'Бумага A4 80' },
    });
    fireEvent.click(screen.getByLabelText(/Разрешаю передать текст заявки/));
    fireEvent.click(screen.getByRole('button', { name: 'Распознать заявку' }));
    expect(await screen.findByLabelText('Вопросы для клиента')).toHaveValue(analysis.questions_text);
    expect(recheck).not.toHaveBeenCalled();
  });
  it('lets the manager inspect missing catalog evidence while keeping the candidate unselectable', async () => {
    makeMocks();
    vi.spyOn(sessionApi, 'previewMatches').mockResolvedValue({
      catalog_version_id: catalog.version_id, mode: 'tfidf', requires_manager_confirmation: true,
      calibrated_no_match: false, candidates: [{
        sku: 'INCOMPLETE', name: 'Бумага', price_rub: '100', sale_unit: 'пачка',
        pieces_per_sale_unit: 500, score: 0.9, status: 'needs_review', reason_codes: ['paper_gsm_unknown'],
        requirements: [{ field: 'paper_gsm', label: 'Плотность', requested: '80', actual: null,
          status: 'unknown_product' }],
      }],
    });
    render(<TenantOperations organization={{ id: 'org-matrix', name: 'Matrix', role: 'editor' }}
      catalog={catalog} csrf="csrf-matrix" onOrganizationChange={vi.fn()}/>);
    fillPaperFacts();
    fireEvent.change(screen.getByLabelText('Наименование позиции'), { target: { value: 'Бумага' } });
    fireEvent.click(screen.getByRole('button', { name: 'Подобрать товары' }));
    expect(await screen.findByText('Нет данных в прайсе')).toBeInTheDocument();
    expect(screen.getByRole('option', { name: /INCOMPLETE/ })).toBeDisabled();
    expect(screen.getByLabelText('Выбранный товар')).toHaveValue('');
  });
});

describe('whole-request matching', () => {
  const candidate = (sku: string) => ({
    sku, name: 'Бумага A4 80', price_rub: '100.00', sale_unit: 'пачка' as const,
    pieces_per_sale_unit: 500, score: 0.9, status: 'compatible' as const, reason_codes: [],
  });
  const batch = {
    catalog_version_id: catalog.version_id, mode: 'tfidf' as const,
    lines: [1, 2].map(line_number => ({
      line_number, catalog_version_id: catalog.version_id, mode: 'tfidf' as const,
      requires_manager_confirmation: true as const, calibrated_no_match: false as const,
      candidates: [candidate('SKU-' + line_number)],
    })),
  };
  const setup = () => {
    makeMocks();
    render(<TenantOperations organization={{ id: 'org-batch', name: 'Batch', role: 'editor' }}
      catalog={catalog} csrf="csrf-batch" onOrganizationChange={vi.fn()}/>);
    fireEvent.click(screen.getByRole('button', { name: /Добавить позицию/ }));
    fillPaperFacts(0); fillPaperFacts(1);
    screen.getAllByLabelText('Наименование позиции').forEach((input, index) =>
      fireEvent.change(input, { target: { value: 'Бумага A4 80, строка ' + (index + 1) } }));
  };

  it('transfers a reviewed composition into SKU controls and invalidates it on request edits', async () => {
    vi.spyOn(sessionApi, 'previewVariants').mockResolvedValue({ ...batch,
      candidate_limit_per_line: 20, variants: [{ kind: 'relevance',
        lines: [1, 2].map(line_number => ({ line_number, sku: 'SKU-' + line_number,
          name: 'Бумага A4 80', sale_quantity: 1, sale_unit: 'пачка', line_total_rub: '100.00' })),
        unresolved_line_numbers: [], total_rub: '200.00', budget_exceeded: false, selectable: true,
      }],
    });
    setup();
    fireEvent.click(screen.getByRole('button', { name: 'Сравнить варианты комплектации' }));
    const choose = await screen.findByRole('button', { name: 'Использовать этот вариант' });
    expect(choose).toBeDisabled();
    fireEvent.click(screen.getByLabelText(/Проверил состав/));
    fireEvent.click(choose);
    expect(screen.getAllByLabelText('Выбранный товар').map(el =>
      (el as HTMLSelectElement).value)).toEqual(['SKU-1', 'SKU-2']);
    fireEvent.change(screen.getAllByLabelText('Наименование позиции')[0]!, {
      target: { value: 'Уточнённая бумага' },
    });
    expect(screen.queryByRole('button', { name: 'Использовать этот вариант' })).not.toBeInTheDocument();
    expect(screen.getAllByLabelText('Выбранный товар')).toHaveLength(1);
  });

  it('previews every row in one request without silently approving an SKU', async () => {
    const preview = vi.spyOn(sessionApi, 'previewBatchMatches').mockResolvedValue(batch);
    setup();
    fireEvent.click(screen.getByRole('button', { name: 'Подобрать товары по всей заявке' }));
    await waitFor(() => expect(preview).toHaveBeenCalledWith(
      'org-batch', catalog.version_id, [
        expect.objectContaining({ description: 'Бумага A4 80, строка 1' }),
        expect.objectContaining({ description: 'Бумага A4 80, строка 2' }),
      ], 'csrf-batch', undefined,
    ));
    expect(await screen.findByText(/Подбор завершён для 2 позиций/)).toBeInTheDocument();
    expect(screen.getByRole('option', { name: /SKU-1/ })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: /SKU-2/ })).toBeInTheDocument();
    screen.getAllByLabelText('Выбранный товар').forEach(select => expect(select).toHaveValue(''));
  });

  it('discards a batch that finishes after the request was edited', async () => {
    let resolve!: (value: typeof batch) => void;
    vi.spyOn(sessionApi, 'previewBatchMatches').mockImplementation(() =>
      new Promise(done => { resolve = done; }));
    setup();
    fireEvent.click(screen.getByRole('button', { name: 'Подобрать товары по всей заявке' }));
    fireEvent.change(screen.getAllByLabelText('Наименование позиции')[0]!, {
      target: { value: 'Изменённая заявка' },
    });
    await act(async () => { resolve(batch); });
    expect(screen.queryByRole('option', { name: /SKU-1/ })).not.toBeInTheDocument();
  });

  it('rejects a response whose row numbers do not match the submitted request', async () => {
    vi.spyOn(sessionApi, 'previewBatchMatches').mockResolvedValue({
      ...batch, lines: [...batch.lines].reverse(),
    });
    setup();
    fireEvent.click(screen.getByRole('button', { name: 'Подобрать товары по всей заявке' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Ответ не соответствует заявке');
    expect(screen.queryByRole('option', { name: /SKU-1/ })).not.toBeInTheDocument();
  });

  it('blocks reuse after the server reports a different catalog', async () => {
    vi.spyOn(sessionApi, 'previewBatchMatches').mockResolvedValue({
      ...batch, catalog_version_id: 'stale-version',
    });
    setup();
    fireEvent.click(screen.getByRole('button', { name: 'Подобрать товары по всей заявке' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Каталог изменился');
    expect(screen.getByRole('button', { name: 'Подобрать товары по всей заявке' })).toBeDisabled();
  });
});

describe('LLM instructions flow into matching policy', () => {
  it('carries the extracted budget to the rules form and requires confirmation before TF-IDF', async () => {
    makeMocks();
    const raw = 'Бюджет до 30000 рублей';
    const rules = { schema_version: 1 as const, markup_percent: '0', budget_max_rub: '30000', required_brand: null, excluded_brands: [] };
    vi.spyOn(sessionApi, 'extractRequest').mockResolvedValue({
      extraction_id: 'extraction-rules', source: 'groq', approved: false, requires_human_review: true,
      extracted_request: { instruction_quotes: [raw], lines: [{
        source_quote: '2 пачки бумаги А4 80 г/м²', description: 'Бумага А4 80 г/м²',
        category: 'paper', quantity: 2, unit: 'пачка', brand: null,
        paper_format: 'A4', paper_gsm: 80, pen_ink_color: null, pen_ink_type: null, attributes: {},
      }] },
      instruction_proposal: { raw_text: raw, rules, unresolved_requirements: [], can_confirm: true },
    });
    const matching = vi.spyOn(sessionApi, 'previewMatches').mockResolvedValue({
      catalog_version_id: catalog.version_id, mode: 'tfidf', requires_manager_confirmation: true,
      calibrated_no_match: false, candidates: [],
    });
    render(<TenantOperations organization={{ id: 'org-policy', name: 'Policy', role: 'editor' }}
      catalog={catalog} csrf="csrf-policy" onOrganizationChange={vi.fn()}/>);
    fireEvent.change(screen.getByLabelText('Текст заявки (до 12 000 символов)'), {
      target: { value: '2 пачки бумаги А4 80 г/м². ' + raw },
    });
    fireEvent.click(screen.getByLabelText(/Разрешаю передать текст заявки/));
    fireEvent.click(screen.getByRole('button', { name: 'Распознать заявку' }));
    await waitFor(() => expect(screen.getByLabelText('Условия подбора и расчёта')).toHaveValue(raw));
    fireEvent.click(screen.getByRole('button', { name: 'Подобрать товары' }));
    expect(matching).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Подтвердить условия' }));
    fireEvent.click(screen.getByRole('button', { name: 'Подобрать товары' }));
    await waitFor(() => expect(matching).toHaveBeenCalledWith(
      'org-policy', catalog.version_id, expect.any(Object), 'csrf-policy', { raw_text: raw, rules }, 'tfidf',
    ));
  });
});
