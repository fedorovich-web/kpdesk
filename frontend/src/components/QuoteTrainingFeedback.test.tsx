import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { sessionApi } from '../tenant-session';
import type { TenantQuote, TrainingFeedbackBundle, TrainingFeedbackContext } from '../tenant-session';
import { QuoteTrainingFeedback } from './QuoteTrainingFeedback';
import { QuoteTrainingFeedbackHistory } from './QuoteTrainingFeedbackHistory';

const quote: TenantQuote = { quote_id: 'q', revision: 1, snapshot_sha256: 'a'.repeat(64),
  catalog_version_id: 'cat', ready_for_draft: true, total_rub: '10.00', approved: false, lines: [],
  source_request: { lines: [{ description: 'Бумага', source_quote: 'Бумага', category: 'paper',
    quantity: 1, unit: 'пачка', brand: null, paper_format: 'A4', paper_gsm: 80,
    pen_ink_color: null, pen_ink_type: null, attributes: {} }] } };
const context: TrainingFeedbackContext = { quote_id: 'q', source_revision: 1, source_snapshot_sha256: 'a'.repeat(64),
  catalog_version_id: 'cat', lines: [{ line_number: 1, description: 'Бумага', selected_sku: 'A', no_match_allowed: true,
    candidates: [{ sku: 'A', name: 'Бумага A4' }, { sku: 'B', name: 'Бумага A3' }] }] };
const bundle: TrainingFeedbackBundle = { format: 'kpdesk-manager-feedback-v1', training_data_kind: 'manager_reviewed',
  organization_id: 'org', quote_id: 'q', revision: 1, snapshot_sha256: 'a'.repeat(64), catalog_version_id: 'cat',
  training_consent: true, catalog: [], events: [] };
let create: ReturnType<typeof vi.fn>;
let click: ReturnType<typeof vi.spyOn>;
beforeEach(() => {
  create = vi.fn(() => 'blob:feedback');
  vi.stubGlobal('URL', { createObjectURL: create, revokeObjectURL: vi.fn() });
  click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
  vi.spyOn(sessionApi, 'trainingFeedbackContext').mockResolvedValue(context);
  vi.spyOn(sessionApi, 'exportTrainingFeedback').mockResolvedValue(bundle);
  vi.spyOn(sessionApi, 'saveTrainingFeedback').mockResolvedValue({ feedback_id: 'f1', quote_id: 'q', revision: 1,
    reviewer_id: 'owner', created_at: '2026-11-02T12:00:00Z', withdrawn_at: null, event_count: 2, curation_status: 'pending_review' });
  vi.spyOn(sessionApi, 'trainingFeedbackHistory').mockResolvedValue({ items: [], next_cursor: null });
  vi.spyOn(sessionApi, 'downloadSavedTrainingFeedback').mockResolvedValue(bundle);
  vi.spyOn(sessionApi, 'withdrawTrainingFeedback').mockResolvedValue({ feedback_id: 'f1', quote_id: 'q', revision: 1,
    reviewer_id: 'owner', created_at: '2026-11-02T12:00:00Z', withdrawn_at: '2026-11-02T13:00:00Z',
    event_count: 2, curation_status: 'pending_review' });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });
const mount = () => render(<QuoteTrainingFeedback organizationId="org" quote={quote} csrf="csrf" canManage/>);
async function load() {
  fireEvent.click(screen.getByText('Оценить подбор товаров'));
  fireEvent.click(screen.getByRole('button', { name: 'Загрузить товары для оценки' }));
  await screen.findByLabelText('Подходящий товар · строка 1');
}
async function ready() {
  await load();
  fireEvent.change(screen.getByLabelText('Подходящий товар · строка 1'), { target: { value: 'A' } });
  fireEvent.click(screen.getByLabelText('Не подходит: Бумага A3 (B)'));
  fireEvent.click(screen.getByLabelText(/Я проверил оценки/));
}
describe('explicit training feedback', () => {
  it('does not restore a history response invalidated by a newer save', async () => {
    let resolve!: (value: Awaited<ReturnType<typeof sessionApi.trainingFeedbackHistory>>) => void;
    vi.mocked(sessionApi.trainingFeedbackHistory).mockImplementation(() => new Promise(done => { resolve=done; }));
    const view=render(<QuoteTrainingFeedbackHistory organizationId="org" quoteId="q" csrf="csrf" scope="org/q" version={0}/>);
    fireEvent.click(screen.getByRole('button',{name:'Загрузить историю оценок'}));
    view.rerender(<QuoteTrainingFeedbackHistory organizationId="org" quoteId="q" csrf="csrf" scope="org/q" version={1}/>);
    await act(async () => resolve({items:[{feedback_id:'old',quote_id:'q',revision:1,reviewer_id:'owner',
      created_at:'2026-11-02T12:00:00Z',withdrawn_at:null,event_count:2,curation_status:'pending_review'}],next_cursor:null}));
    expect(screen.queryByText(/Сохранено для подготовки обучающих данных/)).not.toBeInTheDocument();
  });
  it('saves only explicit labels and reuses its key after an uncertain network result', async () => {
    vi.mocked(sessionApi.saveTrainingFeedback).mockRejectedValueOnce(new Error('Connection lost'));
    mount(); await ready();
    fireEvent.click(screen.getByRole('button', { name: 'Сохранить оценки в организации' }));
    await screen.findByRole('alert');
    fireEvent.click(screen.getByRole('button', { name: 'Сохранить оценки в организации' }));
    await screen.findByText(/Оценки сохранены/);
    const calls=vi.mocked(sessionApi.saveTrainingFeedback).mock.calls;
    expect(calls).toHaveLength(2);
    expect(calls[0]![6]).toBe(calls[1]![6]);
    expect(calls[0]![6]).toBeTruthy();
    expect(screen.getByLabelText(/Я проверил оценки/)).not.toBeChecked();
  });
  it('loads saved history and withdraws consent without deleting the audit', async () => {
    const summary = { feedback_id: 'f1', quote_id: 'q', revision: 1, reviewer_id: 'owner',
      created_at: '2026-11-02T12:00:00Z', withdrawn_at: null, event_count: 2, curation_status: 'pending_review' as const };
    vi.mocked(sessionApi.trainingFeedbackHistory).mockResolvedValue({ items: [summary], next_cursor: null });
    mount(); fireEvent.click(screen.getByText('Оценить подбор товаров'));
    fireEvent.click(screen.getByRole('button', { name: 'Загрузить историю оценок' }));
    await screen.findByText(/Сохранено для подготовки обучающих данных/);
    fireEvent.click(screen.getByRole('button', { name: /Отозвать согласие/ }));
    await screen.findByText(/Согласие отозвано/);
    expect(sessionApi.withdrawTrainingFeedback).toHaveBeenCalledWith('org','q','f1','csrf');
    expect(screen.queryByRole('button', { name: /Скачать сохранённые оценки/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Отозвать согласие/ })).not.toBeInTheDocument();
  });
  it('discards saved history from an old organization response', async () => {
    let resolve!: (value: Awaited<ReturnType<typeof sessionApi.trainingFeedbackHistory>>) => void;
    vi.mocked(sessionApi.trainingFeedbackHistory).mockImplementation(() => new Promise(done => { resolve=done; }));
    const view=mount(); fireEvent.click(screen.getByText('Оценить подбор товаров'));
    fireEvent.click(screen.getByRole('button', { name: 'Загрузить историю оценок' }));
    view.rerender(<QuoteTrainingFeedback organizationId="other" quote={quote} csrf="csrf" canManage/>);
    await act(async () => resolve({ items: [{ feedback_id:'secret',quote_id:'q',revision:1,reviewer_id:'owner',
      created_at:'2026-11-02T12:00:00Z',withdrawn_at:null,event_count:2,curation_status:'pending_review' }],next_cursor:null }));
    expect(screen.queryByText(/Сохранено для подготовки обучающих данных/)).not.toBeInTheDocument();
  });
  it('does not infer any decisions from the saved manager selection', async () => {
    mount(); await load();
    expect(screen.getByLabelText('Подходящий товар · строка 1')).toHaveValue('');
    expect(screen.getByRole('button', { name: 'Скачать оценки для обучения' })).toBeDisabled();
    expect(sessionApi.exportTrainingFeedback).not.toHaveBeenCalled();
    expect(screen.getByLabelText('Не подходит: Бумага A3 (B)')).not.toBeChecked();
  });
  it('shows actual property mismatches for a candidate before labeling it', async () => {
    vi.mocked(sessionApi.trainingFeedbackContext).mockResolvedValue({ ...context, lines: [{ ...context.lines[0]!,
      candidates: [{ sku: 'A', name: 'Бумага A4' }, { sku: 'B', name: 'Бумага A3', requirements: [
        { field: 'paper_format', label: 'Формат бумаги', requested: 'A4', actual: 'A3', status: 'mismatch' },
      ] }],
    }] });
    mount(); await load();
    fireEvent.click(screen.getByText('Сравнить характеристики: Бумага A3'));
    expect(screen.getByText('A3')).toBeVisible();
    expect(screen.getByLabelText('Не подходит: Бумага A3 (B)')).not.toBeChecked();
  });
  it('exports only explicit positive/negative decisions with consent and exact snapshot', async () => {
    mount(); await ready();
    fireEvent.click(screen.getByRole('button', { name: 'Скачать оценки для обучения' }));
    await screen.findByText(/Файл с оценками скачан/);
    expect(sessionApi.exportTrainingFeedback).toHaveBeenCalledWith('org', quote,
      [{ line_number: 1, relevant_sku: 'A', rejected_skus: ['B'], no_match: false }], true, true, 'csrf');
    expect(create).toHaveBeenCalledOnce(); expect(click).toHaveBeenCalledOnce();
    expect(screen.getByLabelText(/Я проверил оценки/)).not.toBeChecked();
  });
  it('requires rejecting every catalogue item for no-match', async () => {
    mount(); await load();
    fireEvent.change(screen.getByLabelText('Подходящий товар · строка 1'), { target: { value: '__no_match__' } });
    fireEvent.click(screen.getByLabelText('Не подходит: Бумага A3 (B)'));
    expect(screen.getByLabelText(/Я проверил оценки/)).toBeDisabled();
    fireEvent.click(screen.getByLabelText('Не подходит: Бумага A4 (A)'));
    expect(screen.getByLabelText(/Я проверил оценки/)).toBeEnabled();
  });
  it('cannot download after organization scope changed during export', async () => {
    let resolve!: (value: TrainingFeedbackBundle) => void;
    vi.mocked(sessionApi.exportTrainingFeedback).mockImplementation(() => new Promise(done => { resolve = done; }));
    const view = mount(); await ready();
    fireEvent.click(screen.getByRole('button', { name: 'Скачать оценки для обучения' }));
    view.rerender(<QuoteTrainingFeedback organizationId="other" quote={quote} csrf="csrf" canManage/>);
    await act(async () => { resolve(bundle); });
    expect(create).not.toHaveBeenCalled(); expect(click).not.toHaveBeenCalled();
    expect(screen.queryByLabelText('Подходящий товар · строка 1')).not.toBeInTheDocument();
  });
  it('rejects a foreign response and allows retry without losing evaluations', async () => {
    vi.mocked(sessionApi.exportTrainingFeedback).mockResolvedValue({ ...bundle, organization_id: 'foreign' });
    mount(); await ready();
    fireEvent.click(screen.getByRole('button', { name: 'Скачать оценки для обучения' }));
    await screen.findByRole('alert');
    expect(create).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.getByRole('button', { name: 'Скачать оценки для обучения' })).toBeEnabled());
  });
  it('does not expose export controls to editors/viewers', () => {
    render(<QuoteTrainingFeedback organizationId="org" quote={quote} csrf="csrf" canManage={false}/>);
    expect(screen.queryByText('Оценить подбор товаров')).not.toBeInTheDocument();
  });
});
