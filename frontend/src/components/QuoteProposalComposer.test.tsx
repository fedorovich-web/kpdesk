import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { sessionApi } from '../tenant-session';
import type { CommercialDocument, TenantQuote } from '../tenant-session';
import { QuoteProposalComposer } from './QuoteProposalComposer';

const NativeURL = URL;
beforeEach(() => {
  vi.stubGlobal('URL', class extends NativeURL {
    static createObjectURL = vi.fn(() => 'blob:proposal-preview');
    static revokeObjectURL = vi.fn();
  });
  vi.spyOn(sessionApi, 'previewDocumentPdf').mockResolvedValue(new Blob(['%PDF-1.7\npreview'], { type: 'application/pdf' }));
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

const showPdf = async () => {
  fireEvent.click(screen.getByRole('button', { name: 'Посмотреть КП перед утверждением' }));
  expect(await screen.findByTitle('Предпросмотр коммерческого предложения')).toHaveAttribute('src', 'blob:proposal-preview#view=FitH&navpanes=0');
  expect(screen.getByLabelText(/Я проверил текст, состав/)).toBeEnabled();
};
const generateText = async () => {
  fireEvent.click(screen.getByLabelText(/Разрешаю передать задание/));
  fireEvent.click(screen.getByRole('button', { name: 'Сгенерировать текст КП' }));
  await screen.findByLabelText('Вступление');
};

const quote: TenantQuote = {
  quote_id: 'quote-1', revision: 1, catalog_version_id: 'catalog-1',
  ready_for_draft: true, total_rub: '1000.00', snapshot_sha256: 'a'.repeat(64),
  approved: false, lines: [],
};
const narrative = {
  headline: 'Оснащение офиса', opening: 'Уважаемые коллеги! Предлагаем решение для вашего офиса.',
  solution: 'В предложение включены товары, подобранные по заявке.',
  closing: 'Готовы обсудить состав заказа.',
};
const preview = {
  quote_id: quote.quote_id, source_revision: quote.revision,
  source_snapshot_sha256: quote.snapshot_sha256,
  narrative, model: 'configured-model', requires_human_review: true,
};
const props = () => ({
  organizationId: 'org-1', issuerName: 'Компания', quote, csrf: 'csrf', canWrite: true,
  requestBrief: 'Подготовь деловое предложение для оснащения офиса', onApproved: vi.fn(),
});

it('keeps unavailable saved profiles explicit and permits deliberate legacy-context generation', async () => {
  vi.spyOn(sessionApi, 'companyProfile').mockRejectedValue(new Error('Настройки недоступны'));
  const generate = vi.spyOn(sessionApi, 'generatePresentation').mockResolvedValue(preview);
  render(<QuoteProposalComposer {...props()} useCompanyProfile/>);
  expect(await screen.findByRole('alert')).toHaveTextContent('Настройки недоступны');
  fireEvent.click(screen.getByLabelText(/Разрешаю передать задание/));
  expect(screen.getByRole('button', { name: 'Сгенерировать текст КП' })).toBeDisabled();
  fireEvent.click(screen.getByRole('button', { name: 'Продолжить без сохранённых сведений' }));
  expect(screen.getByLabelText(/Разрешаю передать задание/)).not.toBeChecked();
  fireEvent.click(screen.getByLabelText(/Разрешаю передать задание/));
  await waitFor(() => expect(screen.getByRole('button', { name: 'Сгенерировать текст КП' })).toBeEnabled());
  fireEvent.click(screen.getByRole('button', { name: 'Сгенерировать текст КП' }));
  await waitFor(() => expect(generate).toHaveBeenCalledWith('org-1', quote, expect.any(Object), 'csrf', true));
});

describe('automatic commercial proposal composition', () => {
  it('adds KPDesk default branding to a new proposal made from a legacy template without changing that template', async () => {
    vi.spyOn(sessionApi, 'generatePresentation').mockResolvedValue(preview);
    const seed = { appearance: { template: 'compact' as const, accent_color: '#123456',
      show_company_section: false, layout_version: 1 as const }, commercial_terms: 'Условия шаблона' };
    render(<QuoteProposalComposer {...props()} templateSeed={seed}/>);
    expect(screen.getByAltText('Логотип KPDesk по умолчанию')).toHaveAttribute('src', '/assets/kpdesk-default-v1.png');
    await generateText();
    await showPdf();
    expect(vi.mocked(sessionApi.previewDocumentPdf).mock.calls[0]?.[2].appearance).toEqual({
      ...seed.appearance, layout_version: 2, default_logo: 'kpdesk_v1',
    });
    expect(seed.appearance.layout_version).toBe(1);
    expect(seed.appearance).not.toHaveProperty('default_logo');
  });
  it('changes appearance without another AI call and requires renewed final review', async () => {
    const generate = vi.spyOn(sessionApi, 'generatePresentation').mockResolvedValue(preview);
    const approve = vi.spyOn(sessionApi, 'approveDocument').mockRejectedValue(new Error('stop after request'));
    render(<QuoteProposalComposer {...props()}/>);
    fireEvent.click(screen.getByLabelText(/Разрешаю передать задание/));
    fireEvent.click(screen.getByRole('button', { name: 'Сгенерировать текст КП' }));
    await screen.findByLabelText('Вступление');
    await showPdf();
    fireEvent.click(screen.getByLabelText(/Я проверил текст, состав/));
    fireEvent.click(screen.getByLabelText(/Краткое предложение/));
    expect(screen.getByRole('button', { name: 'Утвердить КП и подготовить PDF' })).toBeDisabled();
    expect(screen.getByLabelText('Вступление')).toHaveValue(narrative.opening);
    expect(screen.queryByTitle('Предпросмотр коммерческого предложения')).not.toBeInTheDocument();
    expect(screen.getByLabelText(/Я проверил текст, состав/)).toBeDisabled();
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:proposal-preview');
    expect(generate).toHaveBeenCalledOnce();
    await showPdf();
    fireEvent.click(screen.getByLabelText(/Я проверил текст, состав/));
    fireEvent.click(screen.getByRole('button', { name: 'Утвердить КП и подготовить PDF' }));
    await waitFor(() => expect(approve).toHaveBeenCalledWith('org-1', quote,
      expect.objectContaining({ appearance: expect.objectContaining({ template: 'compact' }) }), 'csrf', expect.any(String)));
  });
  it('includes checked server variants in generation and approval without manager-authored prose', async () => {
    const alternative = { catalog_version_id: quote.catalog_version_id, lines: [], total_rub: '800.00',
      selection: { kind: 'lower_cost' as const, selections: [{ line_number: 1, sku: 'ALT', approved_sale_quantity: null }] } };
    vi.spyOn(sessionApi, 'previewDocumentAlternatives').mockResolvedValue({
      quote_id: quote.quote_id, source_revision: 1, source_snapshot_sha256: quote.snapshot_sha256,
      candidate_limit_per_line: 20, alternatives: [alternative],
    });
    const generate = vi.spyOn(sessionApi, 'generatePresentation').mockResolvedValue({ ...preview, alternatives: [alternative] });
    const approve = vi.spyOn(sessionApi, 'approveDocument').mockRejectedValue(new Error('test stops after request'));
    render(<QuoteProposalComposer {...props()}/>);
    fireEvent.click(screen.getByRole('button', { name: 'Подобрать другие варианты для клиента' }));
    fireEvent.click(await screen.findByLabelText(/Добавить этот вариант/));
    expect(screen.getByLabelText(/Разрешаю передать задание/)).not.toBeChecked();
    fireEvent.click(screen.getByLabelText(/Разрешаю передать задание/));
    fireEvent.click(screen.getByRole('button', { name: 'Сгенерировать текст КП' }));
    await screen.findByLabelText('Вступление');
    expect(generate).toHaveBeenCalledWith('org-1', quote, expect.any(Object), 'csrf', true, undefined, [alternative.selection]);
    expect(screen.getByRole('group', { name: 'Текст предложения от ИИ' })).toBeVisible();
    expect(screen.getByRole('group', { name: 'Варианты для утверждения' })).toBeVisible();
    await showPdf();
    fireEvent.click(screen.getByLabelText(/Я проверил текст, состав/));
    fireEvent.click(screen.getByRole('button', { name: 'Утвердить КП и подготовить PDF' }));
    await waitFor(() => expect(approve).toHaveBeenCalledWith('org-1', quote,
      expect.objectContaining({ alternatives: [alternative] }), 'csrf', expect.any(String)));
  });
  it('generates the complete text from the task, then requires review before approval', async () => {
    const generate = vi.spyOn(sessionApi, 'generatePresentation').mockResolvedValue(preview);
    const approve = vi.spyOn(sessionApi, 'approveDocument').mockImplementation(async (
      _org, _quote, document: CommercialDocument,
    ) => ({ ...quote, revision: 2, approved: true, document, approval: {
      user_id: 'manager', approved_at: '2026-10-28T10:00:00Z', source_revision: 1,
      source_snapshot_sha256: quote.snapshot_sha256,
    } }));
    const context = props();
    render(<QuoteProposalComposer {...context}/>);
    const button = screen.getByRole('button', { name: 'Сгенерировать текст КП' });
    expect(button).toBeDisabled();
    const consent = screen.getByLabelText(/Разрешаю передать задание/);
    expect(consent).toHaveAccessibleName(/OpenRouter/);
    expect(consent).toHaveAccessibleName(/NVIDIA Nemotron/);
    expect(consent).toHaveAccessibleName(/данные выбранных товаров/);
    fireEvent.click(consent);
    fireEvent.click(button);
    expect(await screen.findByLabelText('Вступление')).toHaveValue(narrative.opening);
    expect(screen.getByLabelText('Описание товаров')).toHaveValue(narrative.solution);
    expect(generate).toHaveBeenCalledWith('org-1', quote, expect.objectContaining({
      manager_brief: context.requestBrief, issuer_name: 'Компания',
    }), 'csrf', true);
    const confirm = screen.getByRole('button', { name: 'Утвердить КП и подготовить PDF' });
    expect(confirm).toBeDisabled();
    expect(screen.getByLabelText(/Я проверил текст, состав/)).toBeDisabled();
    expect(approve).not.toHaveBeenCalled();
    await showPdf();
    fireEvent.click(screen.getByLabelText(/Я проверил текст, состав/));
    fireEvent.click(confirm);
    await waitFor(() => expect(context.onApproved).toHaveBeenCalledWith(expect.objectContaining({
      approved: true, revision: 2,
    })));
    expect(approve).toHaveBeenCalledWith('org-1', quote, expect.objectContaining({ narrative }), 'csrf', expect.any(String));
    expect(approve.mock.calls[0]?.[2]).toEqual(vi.mocked(sessionApi.previewDocumentPdf).mock.calls[0]?.[2]);
  });

  it('invalidates generated copy when the manager changes the business task', async () => {
    vi.spyOn(sessionApi, 'generatePresentation').mockResolvedValue(preview);
    render(<QuoteProposalComposer {...props()}/>);
    fireEvent.click(screen.getByLabelText(/Разрешаю передать задание/));
    fireEvent.click(screen.getByRole('button', { name: 'Сгенерировать текст КП' }));
    await screen.findByLabelText('Вступление');
    fireEvent.change(screen.getByLabelText('Задача клиента и пожелания к тексту'), { target: { value: 'Другая задача' } });
    expect(screen.queryByLabelText('Вступление')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Утвердить КП и подготовить PDF' })).not.toBeInTheDocument();
  });

  it('does not replace a failed provider call with a template or a ready PDF', async () => {
    vi.spyOn(sessionApi, 'generatePresentation').mockRejectedValue(new Error('OpenRouter недоступен'));
    render(<QuoteProposalComposer {...props()}/>);
    fireEvent.click(screen.getByLabelText(/Разрешаю передать задание/));
    fireEvent.click(screen.getByRole('button', { name: 'Сгенерировать текст КП' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('OpenRouter недоступен');
    expect(screen.queryByLabelText('Вступление')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Утвердить КП и подготовить PDF' })).not.toBeInTheDocument();
  });

  it('invalidates the PDF and renewed review after a narrative edit without regenerating text', async () => {
    const generate = vi.spyOn(sessionApi, 'generatePresentation').mockResolvedValue(preview);
    const approve = vi.spyOn(sessionApi, 'approveDocument');
    const view = render(<QuoteProposalComposer {...props()}/>);
    await generateText(); await showPdf();
    fireEvent.click(screen.getByLabelText(/Я проверил текст, состав/));
    expect(screen.getByRole('button', { name: 'Утвердить КП и подготовить PDF' })).toBeEnabled();
    fireEvent.change(screen.getByLabelText('Вступление'), { target: { value: 'Проверенное вступление после правки.' } });
    expect(screen.queryByTitle('Предпросмотр коммерческого предложения')).not.toBeInTheDocument();
    expect(screen.getByLabelText(/Я проверил текст, состав/)).not.toBeChecked();
    expect(screen.getByLabelText(/Я проверил текст, состав/)).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Утвердить КП и подготовить PDF' }));
    expect(approve).not.toHaveBeenCalled();
    await showPdf();
    expect(sessionApi.previewDocumentPdf).toHaveBeenLastCalledWith('org-1', quote,
      expect.objectContaining({ narrative: expect.objectContaining({ opening: 'Проверенное вступление после правки.' }) }), 'csrf');
    expect(generate).toHaveBeenCalledOnce();
    view.unmount();
    expect(URL.revokeObjectURL).toHaveBeenCalledTimes(2);
  });

  it.each(['organization', 'revision', 'csrf', 'permission', 'unmount'] as const)(
    'discards an in-flight PDF after %s changes', async change => {
      vi.spyOn(sessionApi, 'generatePresentation').mockResolvedValue(preview);
      let resolve!: (value: Blob) => void;
      vi.mocked(sessionApi.previewDocumentPdf).mockImplementationOnce(() => new Promise(done => { resolve = done; }));
      const context = props();
      const view = render(<QuoteProposalComposer {...context}/>);
      await generateText();
      fireEvent.click(screen.getByRole('button', { name: 'Посмотреть КП перед утверждением' }));
      expect(screen.getByLabelText(/Я проверил текст, состав/)).toBeDisabled();
      if (change === 'unmount') view.unmount();
      else view.rerender(<QuoteProposalComposer {...context}
        organizationId={change === 'organization' ? 'org-2' : context.organizationId}
        quote={change === 'revision' ? { ...quote, revision: 2 } : quote}
        csrf={change === 'csrf' ? 'new-csrf' : context.csrf}
        canWrite={change !== 'permission'}/>);
      await act(async () => resolve(new Blob(['%PDF-1.7\nold'], { type: 'application/pdf' })));
      expect(screen.queryByTitle('Предпросмотр коммерческого предложения')).not.toBeInTheDocument();
      expect(URL.createObjectURL).not.toHaveBeenCalled();
      expect(context.onApproved).not.toHaveBeenCalled();
    },
  );

  it.each(['text/html', 'empty', 'error'])('keeps approval blocked for an invalid PDF response: %s', async kind => {
    vi.spyOn(sessionApi, 'generatePresentation').mockResolvedValue(preview);
    if (kind === 'error') vi.mocked(sessionApi.previewDocumentPdf).mockRejectedValueOnce(new Error('PDF временно недоступен'));
    else vi.mocked(sessionApi.previewDocumentPdf).mockResolvedValueOnce(new Blob(kind === 'empty' ? [] : ['invalid'],
      { type: kind === 'empty' ? 'application/pdf' : kind }));
    const approve = vi.spyOn(sessionApi, 'approveDocument');
    render(<QuoteProposalComposer {...props()}/>);
    await generateText();
    fireEvent.click(screen.getByRole('button', { name: 'Посмотреть КП перед утверждением' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(kind === 'error'
      ? 'PDF временно недоступен' : 'Не удалось получить PDF для просмотра');
    expect(screen.getByLabelText(/Я проверил текст, состав/)).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Утвердить КП и подготовить PDF' })).toBeDisabled();
    expect(approve).not.toHaveBeenCalled();
    expect(URL.createObjectURL).not.toHaveBeenCalled();
  });

  it('applies recipient registry details explicitly, excludes tax fields from generation and persists them in the approved document', async () => {
    const party = { full_name: 'Общество Клиент', short_name: 'ООО Клиент', inn: '7707083893',
      kpp: '770701001', ogrn: '1027700132195', address: 'Москва, адрес заказчика', status: 'ACTIVE' };
    vi.spyOn(sessionApi, 'lookupCompany').mockResolvedValue({ party, source: 'dadata', fetched_at: '2026-10-28T10:00:00Z', cached: false });
    const generate = vi.spyOn(sessionApi, 'generatePresentation').mockResolvedValue(preview);
    const context = props();
    const approve = vi.spyOn(sessionApi, 'approveDocument').mockImplementation(async (
      _org, _quote, document: CommercialDocument,
    ) => ({ ...quote, revision: 2, approved: true, document, approval: { user_id: 'manager',
      approved_at: '2026-10-28T10:00:00Z', source_revision: 1, source_snapshot_sha256: quote.snapshot_sha256 } }));
    render(<QuoteProposalComposer {...context}/>);
    fireEvent.click(screen.getByText('Заполнить заказчика по ИНН'));
    fireEvent.change(screen.getByLabelText('ИНН компании'), { target: { value: party.inn } });
    fireEvent.click(screen.getByRole('button', { name: 'Найти по ИНН' }));
    await screen.findByRole('button', { name: 'Добавить реквизиты' });
    expect(screen.getByLabelText('Для кого предложение')).toHaveValue('');
    expect(generate).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Добавить реквизиты' }));
    expect(screen.getByLabelText('Для кого предложение')).toHaveValue(party.short_name);
    await generateText();
    expect(generate).toHaveBeenCalledWith('org-1', quote, expect.objectContaining({ recipient_name: party.short_name }), 'csrf', true);
    for (const field of [party.inn, party.kpp, party.ogrn, party.address, 'recipient_details'])
      expect(JSON.stringify(generate.mock.calls[0])).not.toContain(field);
    await showPdf();
    expect(sessionApi.previewDocumentPdf).toHaveBeenCalledWith('org-1', quote,
      expect.objectContaining({ recipient_details: party }), 'csrf');
    fireEvent.click(screen.getByLabelText(/Я проверил текст, состав/));
    fireEvent.click(screen.getByRole('button', { name: 'Утвердить КП и подготовить PDF' }));
    await waitFor(() => expect(context.onApproved).toHaveBeenCalledWith(expect.objectContaining({
      document: expect.objectContaining({ recipient_details: party }), approved: true,
    })));
    expect(approve).toHaveBeenCalledWith('org-1', quote, expect.objectContaining({ recipient_details: party }), 'csrf', expect.any(String));
  });
});


it('exposes saved document alternatives as a named group', () => {
  const alternative = { catalog_version_id: quote.catalog_version_id, lines: [], total_rub: '800.00',
    selection: { kind: 'lower_cost' as const, selections: [{ line_number: 1, sku: 'ALT', approved_sale_quantity: null }] } };
  const document: CommercialDocument = { issuer_name: 'Компания', recipient_name: '', manager_brief: '',
    commercial_terms: '', issued_on: '2026-11-12', narrative, alternatives: [alternative] };
  render(<QuoteProposalComposer {...props()} quote={{ ...quote, approved: true, document }}/>);
  expect(screen.getByRole('group', { name: 'Сохранённые варианты КП' })).toBeVisible();
});
