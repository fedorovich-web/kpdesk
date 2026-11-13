import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { sessionApi, SessionApiError } from '../tenant-session';
import type { TenantQuote } from '../tenant-session';
import { TenantOperations } from './TenantOperations';

const catalog = { version_id: 'catalog-save', source_sha256: 'synthetic', rows_count: 1 };
const line = { source_quote: 'Сохранённая заявка на бумагу A4', description: 'Бумага A4',
  category: 'paper', quantity: 2, unit: 'пачка', paper_format: 'A4', paper_gsm: 80,
  brand: null, pen_ink_color: null, pen_ink_type: null, attributes: {} };
const saved = (id = 'saved-quote', revision = 1): TenantQuote => ({
  quote_id: id, revision, catalog_version_id: catalog.version_id,
  snapshot_sha256: 'a'.repeat(64), approved: false, ready_for_draft: false,
  total_rub: null, lines: [], source_request: { lines: [line] },
});
const props = { organization: { id: 'org-save', name: 'Учебная компания', role: 'editor' as const },
  catalog, csrf: 'synthetic-csrf', onOrganizationChange: vi.fn() };
const failure = () => new SessionApiError('network', 'Ответ потерян', 0);
const submit = () => fireEvent.click(screen.getByRole('button', {
  name: /Рассчитать и сохранить КП|Создать следующую версию/,
}));

function fill() {
  fireEvent.change(screen.getByLabelText('Наименование позиции'), { target: { value: 'Бумага A4' } });
  fireEvent.change(screen.getByLabelText('Категория'), { target: { value: 'paper' } });
  fireEvent.change(screen.getByLabelText('Количество'), { target: { value: '2' } });
  fireEvent.change(screen.getByLabelText('Единица'), { target: { value: 'пачка' } });
  fireEvent.change(screen.getByLabelText('Формат'), { target: { value: 'A4' } });
  fireEvent.change(screen.getByLabelText('Плотность бумаги, г/м²'), { target: { value: '80' } });
}

beforeEach(() => {
  vi.spyOn(sessionApi, 'invitations').mockResolvedValue([]);
  vi.spyOn(sessionApi, 'members').mockResolvedValue([]);
  vi.spyOn(sessionApi, 'listQuotes').mockResolvedValue({ items: [] });
  vi.spyOn(sessionApi, 'quoteReview').mockRejectedValue(new SessionApiError('not_found', '', 404));
});
afterEach(() => vi.restoreAllMocks());

describe('saving and reopening quotes as one manager workflow', () => {
  it('retries an uncertain create with the same key, but a deliberate new quote gets a fresh key', async () => {
    const create = vi.spyOn(sessionApi, 'createQuote').mockRejectedValueOnce(failure())
      .mockResolvedValueOnce(saved()).mockResolvedValueOnce(saved('intentional-new'));
    render(<TenantOperations {...props}/>);
    fill(); submit();
    await screen.findByText('Ответ потерян');
    submit();
    await screen.findByRole('heading', { name: 'КП · версия 1' });
    expect(create.mock.calls[1]?.[4]).toBe(create.mock.calls[0]?.[4]);
    fireEvent.click(screen.getByRole('button', { name: 'Создать новое КП' }));
    fill(); submit();
    await waitFor(() => expect(create).toHaveBeenCalledTimes(3));
    expect(create.mock.calls[2]?.[4]).not.toBe(create.mock.calls[0]?.[4]);
  });

  it('preserves unresolved keys for both payloads when the manager edits and then restores a quantity', async () => {
    const create = vi.spyOn(sessionApi, 'createQuote').mockRejectedValue(failure());
    render(<TenantOperations {...props}/>);
    fill(); submit(); await screen.findByText('Ответ потерян');
    fireEvent.change(screen.getByLabelText('Количество'), { target: { value: '3' } });
    submit(); await screen.findByText('Ответ потерян');
    fireEvent.change(screen.getByLabelText('Количество'), { target: { value: '2' } });
    submit(); await screen.findByText('Ответ потерян');
    expect(create).toHaveBeenCalledTimes(3);
    expect(create.mock.calls[1]?.[4]).not.toBe(create.mock.calls[0]?.[4]);
    expect(create.mock.calls[2]?.[4]).toBe(create.mock.calls[0]?.[4]);
  });

  it('retries a revision with its original key and expected revision after an uncertain result', async () => {
    vi.mocked(sessionApi.listQuotes).mockResolvedValue({ items: [{ ...saved(), total_rub: null }] });
    vi.spyOn(sessionApi, 'getQuote').mockResolvedValue(saved());
    const revise = vi.spyOn(sessionApi, 'reviseQuote').mockRejectedValueOnce(failure())
      .mockResolvedValueOnce(saved('saved-quote', 2));
    render(<TenantOperations {...props}/>);
    fireEvent.click(await screen.findByRole('button', { name: 'Открыть' }));
    fireEvent.click(await screen.findByLabelText('Проверил позицию по исходной заявке'));
    submit(); await screen.findByText('Ответ потерян');
    submit(); await screen.findByRole('heading', { name: 'КП · версия 2' });
    expect(revise.mock.calls[1]?.[6]).toBe(revise.mock.calls[0]?.[6]);
    expect(revise.mock.calls[1]?.slice(0, 5)).toEqual(revise.mock.calls[0]?.slice(0, 5));
  });

  it('does not replace another opened quote with a late successful save', async () => {
    const other = saved('other-quote', 7);
    vi.mocked(sessionApi.listQuotes).mockResolvedValue({ items: [other] });
    vi.spyOn(sessionApi, 'getQuote').mockResolvedValue(other);
    let finish!: (quote: TenantQuote) => void;
    vi.spyOn(sessionApi, 'createQuote').mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    render(<TenantOperations {...props}/>);
    fill(); submit();
    fireEvent.click(await screen.findByRole('button', { name: 'Открыть' }));
    await screen.findByRole('heading', { name: 'КП · версия 7' });
    await act(async () => finish(saved()));
    expect(screen.getByRole('heading', { name: 'КП · версия 7' })).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'КП · версия 1' })).not.toBeInTheDocument();
  });

  it('does not report an old save failure against another opened quote', async () => {
    const other = saved('other-quote', 7);
    vi.mocked(sessionApi.listQuotes).mockResolvedValue({ items: [other] });
    vi.spyOn(sessionApi, 'getQuote').mockResolvedValue(other);
    let reject!: (error: Error) => void;
    vi.spyOn(sessionApi, 'createQuote').mockImplementationOnce(() => new Promise((_, fail) => { reject = fail; }));
    render(<TenantOperations {...props}/>);
    fill(); submit();
    fireEvent.click(await screen.findByRole('button', { name: 'Открыть' }));
    await screen.findByRole('heading', { name: 'КП · версия 7' });
    await act(async () => reject(new Error('Ошибка старого сохранения')));
    expect(screen.queryByText('Ошибка старого сохранения')).not.toBeInTheDocument();
  });

  it('keeps newer editor changes when the submitted composition finishes saving', async () => {
    let finish!: (quote: TenantQuote) => void;
    vi.spyOn(sessionApi, 'createQuote').mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    render(<TenantOperations {...props}/>);
    fill(); submit();
    fireEvent.change(screen.getByLabelText('Количество'), { target: { value: '9' } });
    await act(async () => finish(saved()));
    expect(screen.getByLabelText('Количество')).toHaveValue(9);
    expect(screen.queryByRole('heading', { name: 'КП · версия 1' })).not.toBeInTheDocument();
    expect(screen.getByText(/В редакторе остались более новые правки/)).toBeInTheDocument();
  });

  it.each(['wrong-revision', 'wrong-catalog', 'array'] as const)(
    'keeps the retry identity and draft after a malformed save response: %s', async kind => {
      const invalid = kind === 'wrong-revision' ? saved('saved-quote', 4)
        : kind === 'wrong-catalog' ? { ...saved(), catalog_version_id: 'foreign-catalog' } : [];
      const create = vi.spyOn(sessionApi, 'createQuote').mockResolvedValueOnce(invalid as TenantQuote)
        .mockResolvedValueOnce(saved());
      render(<TenantOperations {...props}/>);
      fill(); submit();
      await screen.findByText(/Не удалось подтвердить сохранение товаров/);
      expect(screen.queryByRole('heading', { name: /КП · версия/ })).not.toBeInTheDocument();
      submit(); await screen.findByRole('heading', { name: 'КП · версия 1' });
      expect(create.mock.calls[1]?.[4]).toBe(create.mock.calls[0]?.[4]);
    },
  );

  it.each([true, false])('reopens only the saved request context, with source present=%s', async hasSource => {
    const stored = { ...saved(), ready_for_draft: true, total_rub: '798.00',
      source_request: hasSource ? { lines: [line] } : null };
    vi.mocked(sessionApi.listQuotes).mockResolvedValue({ items: [stored] });
    vi.spyOn(sessionApi, 'getQuote').mockResolvedValue(stored);
    const generate = vi.spyOn(sessionApi, 'generatePresentation').mockResolvedValue({
      quote_id: stored.quote_id, source_revision: 1, source_snapshot_sha256: stored.snapshot_sha256,
      model: 'nvidia/nemotron-3-ultra-550b-a55b:free',
      requires_human_review: true, narrative: { headline: 'Учебное предложение', opening: 'Рассмотрите состав.',
        solution: 'Бумага для офиса.', closing: 'Подтвердите состав.' },
    });
    render(<TenantOperations {...props}/>);
    fireEvent.change(screen.getByLabelText('Текст заявки (до 12 000 символов)'), {
      target: { value: 'Посторонняя заявка на лазерный картридж' },
    });
    fireEvent.click(await screen.findByRole('button', { name: 'Открыть' }));
    await screen.findByLabelText('Задача клиента и пожелания к тексту');
    expect(screen.getByLabelText('Текст заявки (до 12 000 символов)'))
      .toHaveValue(hasSource ? line.source_quote : '');
    expect(screen.getByLabelText('Задача клиента и пожелания к тексту')).not.toHaveValue(expect.stringContaining('Посторонняя'));
    if (hasSource) expect(screen.getByLabelText('Задача клиента и пожелания к тексту')).toHaveValue(line.source_quote);
    fireEvent.click(screen.getByLabelText(/Разрешаю передать задание/));
    fireEvent.click(screen.getByRole('button', { name: 'Сгенерировать текст КП' }));
    await waitFor(() => expect(generate).toHaveBeenCalledOnce());
    expect(JSON.stringify(generate.mock.calls)).not.toContain('Посторонняя');
  });
});
