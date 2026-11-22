// @vitest-environment-options {"url":"https://kpdesk.test/app"}
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { UniversalWorkspace } from './UniversalWorkspace';
import { TenantOperations } from './TenantOperations';
import { sessionApi } from '../tenant-session';

const catalog = { version_id: 'catalog-one', source_sha256: 'catalog-hash', products: [
  { sku: 'C1', name: 'Кабель', sale_unit: 'м', price_rub: '12.50', price_quantity: '100', specifications: [] },
] };
const request = { lines: [{ description: 'Кабель', source_quote: 'Кабель 1.125 м', quantity: '1.125',
  sale_unit: 'м', requirements: [{ name: 'Сечение', value: '2.5', unit: 'мм', operator: 'min', source_quote: 'от 2.5 мм' }] }] };
const quote = { quote_id: 'quote-one', revision: 1, catalog_version_id: 'catalog-one', source_sha256: 'catalog-hash',
  source_request: request, selections: [{ line_number: 1, sku: 'C1' }], total_rub: '0.14', ready_for_draft: true,
  approved: false, snapshot_sha256: 'snapshot', issuer_name: 'Компания', recipient_name: '', manager_brief: '',
  commercial_terms: '', narrative: null, lines: [{ line_number: 1, name: 'Кабель', sku: 'C1', sale_quantity: '1.125',
    sale_unit: 'м', price_rub: '12.50', price_quantity: '100', line_total_rub: '0.14', status: 'manager_selected' }] };
const props = { organizationId: 'org-one', organizationName: 'Компания', csrf: 'csrf', role: 'owner' as const };
const reply = (payload: unknown, status = 200) => new Response(JSON.stringify(payload), { status,
  headers: { 'Content-Type': 'application/json' } });
function setup(extra?: (path: string, options?: RequestInit) => Response | Promise<Response> | undefined) {
  const fetcher = vi.fn((url: RequestInfo | URL, options?: RequestInit): Promise<Response> => {
    const path = String(url).split('/universal')[1] ?? '';
    const custom = extra?.(path, options);
    if (custom) return Promise.resolve(custom);
    if (path === '/capabilities') return Promise.resolve(reply({ available: true, strategy: 'lexical_universal_v1' }));
    if (path === '/catalog/current' || path === '/catalog/import') return Promise.resolve(reply(catalog));
    if (path === '/quotes' && options?.method !== 'POST') return Promise.resolve(reply({ items: [] }));
    if (path === '/requests/extract') return Promise.resolve(reply({ request, requires_human_review: true }));
    if (path === '/matches/preview') return Promise.resolve(reply({ strategy: 'lexical_universal_v1', requires_manager_selection: true,
      lines: [{ line_number: 1, candidates: [{ sku: 'C1', name: 'Кабель', score: 1, status: 'compatible',
        checks: [{ name: 'Сечение', status: 'compatible', reason: 'Совпадает' }], reason_codes: [] }] }] }));
    if (path === '/quotes' && options?.method === 'POST') return Promise.resolve(reply(quote, 201));
    if (path.startsWith('/quotes/quote-one/feedback?')) return Promise.resolve(reply({ items: [], next_offset: null }));
    if (path.startsWith('/quotes/quote-one')) return Promise.resolve(reply(quote));
    return Promise.resolve(reply({ message: 'Неожиданный запрос ' + path }, 400));
  });
  vi.stubGlobal('fetch', fetcher);
  return fetcher;
}
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });
async function extract() {
  await screen.findByText(/Товаров в прайсе: 1/);
  fireEvent.change(screen.getByLabelText('Текст заявки'), { target: { value: 'Кабель 1.125 м' } });
  fireEvent.click(screen.getByLabelText(/Разрешаю передать заявку/));
  fireEvent.click(screen.getByRole('button', { name: 'Распознать заявку' }));
  await screen.findByDisplayValue('1.125');
}
async function select() {
  const review = screen.getByLabelText('Проверил все позиции и характеристики');
  if (!(review as HTMLInputElement).checked) fireEvent.click(review);
  fireEvent.click(screen.getByRole('button', { name: 'Подобрать товары' }));
  fireEvent.click(await screen.findByRole('radio', { name: /Выбрать C1/ }));
}

const requisiteParty = { full_name: 'ООО «Учебный поставщик»', short_name: 'Учебный поставщик',
  inn: '9900000000', kpp: '990001001', ogrn: '1269900000016', address: 'Учебный адрес', status: 'ACTIVE' };
const requisiteProfile = { revision: 1, sha256: 'profile-snapshot', data: { schema_version: 1 as const,
  display_name: requisiteParty.short_name, about: '', advantages: '', contacts: 'sales@example.invalid',
  default_terms: '', generation_style: '', issuer_details: requisiteParty } };

describe('universal company requisites', () => {
  it('explicitly applies saved sender details and saves them with the quote', async () => {
    const fetcher = setup();
    vi.spyOn(sessionApi, 'companyProfile').mockResolvedValue({ profile: requisiteProfile });
    render(<UniversalWorkspace {...props}/>); await extract();
    fireEvent.click(screen.getByRole('button', { name: 'Использовать реквизиты компании' }));
    await screen.findByDisplayValue(requisiteParty.short_name);
    await select(); fireEvent.click(screen.getByRole('button', { name: 'Сохранить КП' }));
    await screen.findByText('Черновик КП · версия 1');
    const sent = fetcher.mock.calls.find(([url, options]) => String(url).endsWith('/universal/quotes') && options?.method === 'POST');
    expect(JSON.parse(String(sent?.[1]?.body))).toMatchObject({ issuer_details: requisiteParty,
      issuer_contacts: 'sales@example.invalid', issuer_name: requisiteParty.short_name });
  });

  it('applies DaData recipient only after review and clears it when the name changes', async () => {
    const fetcher = setup();
    vi.spyOn(sessionApi, 'lookupCompany').mockResolvedValue({ party: requisiteParty, source: 'dadata',
      fetched_at: '2026-11-13T10:00:00Z', cached: false });
    render(<UniversalWorkspace {...props}/>); await extract();
    const panel = screen.getByText('Реквизиты заказчика').closest('details')!;
    fireEvent.click(within(panel).getByText('Реквизиты заказчика'));
    fireEvent.change(within(panel).getByLabelText('ИНН компании'), { target: { value: requisiteParty.inn } });
    fireEvent.click(within(panel).getByRole('button', { name: 'Найти по ИНН' }));
    await within(panel).findByRole('button', { name: 'Добавить реквизиты' });
    expect(screen.getByLabelText('Получатель')).toHaveValue('');
    fireEvent.click(within(panel).getByRole('button', { name: 'Добавить реквизиты' }));
    expect(screen.getByLabelText('Получатель')).toHaveValue(requisiteParty.short_name);
    fireEvent.change(screen.getByLabelText('Получатель'), { target: { value: 'Другой получатель' } });
    await select(); fireEvent.click(screen.getByRole('button', { name: 'Сохранить КП' }));
    await screen.findByText('Черновик КП · версия 1');
    const sent = fetcher.mock.calls.find(([url, options]) => String(url).endsWith('/universal/quotes') && options?.method === 'POST');
    expect(JSON.parse(String(sent?.[1]?.body))).not.toHaveProperty('recipient_details');
  });

  it('does not apply a delayed sender profile after organization changes', async () => {
    setup();
    let resolve!: (value: { profile: typeof requisiteProfile }) => void;
    vi.spyOn(sessionApi, 'companyProfile').mockReturnValue(new Promise(done => { resolve = done; }));
    const view = render(<UniversalWorkspace {...props}/>); await extract();
    fireEvent.click(screen.getByRole('button', { name: 'Использовать реквизиты компании' }));
    view.rerender(<UniversalWorkspace {...props} organizationId="other-org" organizationName="Другая компания"/>);
    await act(async () => resolve({ profile: requisiteProfile }));
    expect(screen.queryByDisplayValue(requisiteParty.short_name)).not.toBeInTheDocument();
  });

  it('keeps saved requisites when adding a reviewed narrative revision', async () => {
    const saved = { ...quote, issuer_details: requisiteParty, issuer_contacts: 'sales@example.invalid',
      recipient_name: requisiteParty.short_name, recipient_details: requisiteParty };
    const narrative = { headline: 'Учебный кабель', opening: 'Добрый день', solution: 'Предлагаем кабель', closing: 'Спасибо' };
    const fetcher = setup((path, options) => path === '/quotes' && options?.method !== 'POST'
      ? reply({ items: [saved] }) : path.startsWith('/quotes/quote-one?') ? reply(saved)
        : path.endsWith('/presentation') ? reply({ narrative, quote_id: quote.quote_id, source_revision: 1,
          source_snapshot_sha256: quote.snapshot_sha256, requires_human_review: true })
          : path.endsWith('/revisions') ? reply({ ...saved, revision: 2, narrative }, 201) : undefined);
    render(<UniversalWorkspace {...props}/>);
    fireEvent.click(await screen.findByRole('button', { name: /Открыть КП quote-one/ }));
    await screen.findByRole('heading', { name: 'Черновик КП · версия 1' });
    fireEvent.click(screen.getByLabelText(/Разрешаю передать факты этого КП/));
    fireEvent.click(screen.getByRole('button', { name: 'Сгенерировать текст КП' }));
    await screen.findByDisplayValue('Учебный кабель');
    fireEvent.click(screen.getByLabelText('Проверил текст предложения и все факты'));
    fireEvent.click(screen.getByRole('button', { name: 'Сохранить новую версию с текстом' }));
    await screen.findByRole('heading', { name: 'Черновик КП · версия 2' });
    const sent = fetcher.mock.calls.find(([url]) => String(url).endsWith('/revisions'));
    expect(JSON.parse(String(sent?.[1]?.body))).toMatchObject({ issuer_details: requisiteParty,
      recipient_details: requisiteParty, issuer_contacts: 'sales@example.invalid' });
  });
});

describe('universal workspace', () => {
  it('keeps rejected AI source and allows explicit manual recovery with decimal commas', async () => {
    const source = 'Кабель 2,5 м, диаметр не менее 16,5 мм';
    const fetcher = setup(path => path === '/requests/extract' ? reply({ code: 'invalid_output',
      message: 'Ответ ИИ не прошёл проверку.' }, 502) : undefined);
    render(<UniversalWorkspace {...props}/>);
    await screen.findByText(/Товаров в прайсе: 1/);
    fireEvent.change(screen.getByLabelText('Текст заявки'), { target: { value: source } });
    fireEvent.click(screen.getByLabelText(/Разрешаю передать заявку/));
    fireEvent.click(screen.getByRole('button', { name: 'Распознать заявку' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(/добавьте позиции вручную/i);
    expect(screen.getByLabelText('Текст заявки')).toHaveValue(source);
    expect(screen.queryByLabelText('Количество позиции 1')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Добавить позицию вручную' }));
    fireEvent.change(screen.getByLabelText('Описание позиции 1'), { target: { value: 'Кабель' } });
    fireEvent.change(screen.getByLabelText('Цитата из заявки позиции 1'), { target: { value: source } });
    fireEvent.change(screen.getByLabelText('Количество позиции 1'), { target: { value: '2,5' } });
    fireEvent.change(screen.getByLabelText('Единица позиции 1'), { target: { value: 'м' } });
    fireEvent.click(screen.getByRole('button', { name: 'Добавить характеристику позиции 1' }));
    fireEvent.change(screen.getByLabelText('Название характеристики 1 позиции 1'), { target: { value: 'Диаметр' } });
    fireEvent.change(screen.getByLabelText('Условие характеристики 1 позиции 1'), { target: { value: 'min' } });
    fireEvent.change(screen.getByLabelText('Значение характеристики 1 позиции 1'), { target: { value: '16,5' } });
    fireEvent.change(screen.getByLabelText('Единица характеристики 1 позиции 1'), { target: { value: 'мм' } });
    fireEvent.change(screen.getByLabelText('Цитата характеристики 1 позиции 1'), { target: { value: 'диаметр не менее 16,5 мм' } });
    expect(screen.getByRole('button', { name: 'Подобрать товары' })).toBeDisabled();
    fireEvent.click(screen.getByLabelText('Проверил все позиции и характеристики'));
    expect(screen.getByRole('button', { name: 'Подобрать товары' })).toBeEnabled();
    fireEvent.click(screen.getByRole('button', { name: 'Подобрать товары' }));
    await screen.findByRole('radio', { name: /Выбрать C1/ });
    const sent = fetcher.mock.calls.find(([url]) => String(url).endsWith('/matches/preview'));
    const line = JSON.parse(String(sent?.[1]?.body)).request.lines[0];
    expect(line).toMatchObject({ quantity: '2.5', sale_unit: 'м', source_quote: source,
      requirements: [{ value: 16.5, operator: 'min', unit: 'мм' }] });
    expect(screen.getByLabelText('Количество позиции 1')).toHaveValue('2,5');
    expect(fetcher.mock.calls.filter(([url]) => String(url).endsWith('/requests/extract'))).toHaveLength(1);
  });
  it('sends a comma quantity as an exact decimal string without numeric coercion', async () => {
    const fetcher = setup(); render(<UniversalWorkspace {...props}/>); await extract();
    fireEvent.change(screen.getByLabelText('Количество позиции 1'), { target: { value: '999999999999,999' } });
    fireEvent.click(screen.getByLabelText('Проверил все позиции и характеристики'));
    fireEvent.click(screen.getByRole('button', { name: 'Подобрать товары' }));
    await screen.findByRole('radio', { name: /Выбрать C1/ });
    const sent = fetcher.mock.calls.find(([url]) => String(url).endsWith('/matches/preview'));
    expect(JSON.parse(String(sent?.[1]?.body)).request.lines[0].quantity).toBe('999999999999.999');
  });
  it.each(['1.0000000000000001', '1,0000000000000001', '1000000000', '16,5,0'])(
    'refuses an invalid or overprecise numeric requirement before Number can round %s', async value => {
      const fetcher = setup(); render(<UniversalWorkspace {...props}/>); await extract();
      fireEvent.change(screen.getByLabelText('Значение характеристики 1 позиции 1'), { target: { value } });
      expect(screen.getByLabelText('Проверил все позиции и характеристики')).toBeDisabled();
      expect(screen.getByRole('button', { name: 'Подобрать товары' })).toBeDisabled();
      expect(fetcher.mock.calls.some(([url]) => String(url).endsWith('/matches/preview'))).toBe(false);
    });
  it.each(['0', '-2', '1,0001', '1000000000000', '2,5,0'])(
    'refuses an invalid exact quantity %s while allowing unknown quantities for review', async value => {
      setup(); render(<UniversalWorkspace {...props}/>); await extract();
      fireEvent.change(screen.getByLabelText('Количество позиции 1'), { target: { value } });
      expect(screen.getByLabelText('Проверил все позиции и характеристики')).toBeDisabled();
      fireEvent.change(screen.getByLabelText('Количество позиции 1'), { target: { value: '' } });
      expect(screen.getByLabelText('Проверил все позиции и характеристики')).toBeEnabled();
    });
  it.each([
    ['999999999,999999', 999999999.999999], ['0,000001', 0.000001],
    ['+00001,5000000', 1.5], ['-0,5', -0.5],
  ])('retains valid bounded numeric characteristic %s', async (value, expected) => {
    const fetcher = setup(); render(<UniversalWorkspace {...props}/>); await extract();
    fireEvent.change(screen.getByLabelText('Значение характеристики 1 позиции 1'), { target: { value } });
    fireEvent.click(screen.getByLabelText('Проверил все позиции и характеристики'));
    fireEvent.click(screen.getByRole('button', { name: 'Подобрать товары' }));
    await screen.findByRole('radio', { name: /Выбрать C1/ });
    const sent = fetcher.mock.calls.find(([url]) => String(url).endsWith('/matches/preview'));
    expect(JSON.parse(String(sent?.[1]?.body)).request.lines[0].requirements[0].value).toBe(expected);
  });
  it('uses ordinary customer wording throughout request, selection and saved quote', async () => {
    setup(); render(<UniversalWorkspace {...props}/>);
    await extract(); await select();
    fireEvent.click(screen.getByRole('button', { name: 'Сохранить КП' }));
    await screen.findByText('Итого: 0.14 ₽');
    const labels = [...document.querySelectorAll('[aria-label]')].map(node => node.getAttribute('aria-label')).join(' ');
    expect(document.body.textContent + labels).not.toMatch(/универсальн/i);
    expect(screen.getByRole('region', { name: 'Сохранённый черновик' })).toBeInTheDocument();
  });
  it('does not contact universal endpoints until the workspace disclosure opens', async () => {
    const fetcher = setup(path => path === '/capabilities' ? reply({ available: false }) : undefined);
    vi.spyOn(sessionApi, 'invitations').mockResolvedValue([]);
    vi.spyOn(sessionApi, 'listQuotes').mockResolvedValue({ items: [] });
    render(<TenantOperations organization={{ id: props.organizationId, name: props.organizationName, role: 'viewer' }}
      csrf={props.csrf} catalog={null} onOrganizationChange={vi.fn()} compactNavigation/>);
    await waitFor(() => expect(sessionApi.listQuotes).toHaveBeenCalled());
    expect(fetcher.mock.calls.filter(([url]) => String(url).includes('/universal/'))).toHaveLength(0);
    const disclosure = screen.getByText('Прайс, заявка и КП').closest('details')!;
    disclosure.open = true; fireEvent(disclosure, new Event('toggle'));
    expect(await screen.findByText(/Раздел подготовки КП пока недоступен/)).toBeInTheDocument();
    expect(fetcher.mock.calls.filter(([url]) => String(url).includes('/universal/'))).toHaveLength(1);
    expect(screen.getByRole('heading', { name: 'Коммерческие предложения' })).toBeInTheDocument();
  });
  it('keeps the unavailable schema isolated and does not fetch catalogs or history', async () => {
    const fetcher = setup(path => path === '/capabilities' ? reply({ available: false, reason: 'missing_schema' }) : undefined);
    render(<UniversalWorkspace {...props}/>);
    expect(await screen.findByText(/Раздел подготовки КП пока недоступен/)).toBeInTheDocument();
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('button', { name: 'Подобрать товары' })).not.toBeInTheDocument();
  });
  it('requires explicit external consent, retains decimal strings and requires manager selection', async () => {
    const fetcher = setup(); render(<UniversalWorkspace {...props}/>);
    await screen.findByText(/Товаров в прайсе: 1/);
    expect(screen.getByRole('button', { name: 'Распознать заявку' })).toBeDisabled();
    expect(screen.getByLabelText(/Разрешаю передать заявку/)).not.toBeChecked();
    await extract();
    expect(screen.getByLabelText('Условие характеристики 1 позиции 1')).toHaveValue('min');
    expect(screen.getByRole('button', { name: 'Сохранить КП' })).toBeDisabled();
    await select();
    fireEvent.click(screen.getByRole('button', { name: 'Сохранить КП' }));
    await screen.findByText('Итого: 0.14 ₽');
    const sent = fetcher.mock.calls.find(([url, options]) => String(url).endsWith('/universal/quotes') && options?.method === 'POST');
    const body = JSON.parse(String(sent?.[1]?.body));
    expect(body.request.lines[0].quantity).toBe('1.125');
    expect(body.selections).toEqual([{ line_number: 1, sku: 'C1' }]);
    expect(body.request.lines[0].requirements[0].operator).toBe('min');
    expect(sent?.[1]?.headers).toMatchObject({ 'X-CSRF-Token': 'csrf', 'Idempotency-Key': expect.any(String) });
    expect(screen.getByRole('link', { name: 'Скачать PDF черновика' })).toHaveAttribute('href', expect.stringContaining('revision=1'));
  });
  it('invalidates selections and preview when source, request or quote context changes', async () => {
    setup(); render(<UniversalWorkspace {...props}/>); await extract(); await select();
    expect(screen.getByRole('button', { name: 'Сохранить КП' })).toBeEnabled();
    fireEvent.change(screen.getByLabelText('Условия оплаты и поставки'), { target: { value: 'Без доставки' } });
    expect(screen.queryByRole('radio', { name: /Выбрать C1/ })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Сохранить КП' })).toBeDisabled();
    await select();
    fireEvent.change(screen.getByLabelText('Количество позиции 1'), { target: { value: '2.125' } });
    expect(screen.getByLabelText('Проверил все позиции и характеристики')).not.toBeChecked();
    expect(screen.queryByRole('radio', { name: /Выбрать C1/ })).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Текст заявки'), { target: { value: 'Другой запрос' } });
    expect(screen.queryByDisplayValue('2.125')).not.toBeInTheDocument();
  });
  it('discards pending extraction after an organization switch', async () => {
    let resolve!: (response: Response) => void;
    setup(path => path === '/requests/extract' ? new Promise(r => { resolve = r; }) : undefined);
    const view = render(<UniversalWorkspace {...props}/>);
    await screen.findByText(/Товаров в прайсе: 1/);
    fireEvent.change(screen.getByLabelText('Текст заявки'), { target: { value: 'Кабель 1.125 м' } });
    fireEvent.click(screen.getByLabelText(/Разрешаю передать заявку/));
    fireEvent.click(screen.getByRole('button', { name: 'Распознать заявку' }));
    view.rerender(<UniversalWorkspace {...props} organizationId="org-two"/>);
    await act(async () => { resolve(reply({ request, requires_human_review: true })); });
    expect(screen.getByLabelText('Текст заявки')).toHaveValue('');
    expect(screen.queryByDisplayValue('1.125')).not.toBeInTheDocument();
  });
  it('imports canonical files as multipart and clears a stale selection after replacement', async () => {
    const fetcher = setup(); render(<UniversalWorkspace {...props}/>); await extract(); await select();
    fireEvent.change(screen.getByLabelText('Прайс CSV или XLSX'), { target: { files: [new File(['sku;name;price_rub;sale_unit'], 'price.csv')] } });
    fireEvent.click(screen.getByRole('button', { name: 'Импортировать прайс' }));
    await waitFor(() => expect(fetcher.mock.calls.some(([url]) => String(url).endsWith('/catalog/import'))).toBe(true));
    const sent = fetcher.mock.calls.find(([url]) => String(url).endsWith('/catalog/import'));
    expect(sent?.[1]?.body).toBeInstanceOf(FormData);
    expect((sent?.[1]?.body as FormData).get('file')).toBeInstanceOf(File);
    expect(screen.queryByRole('radio', { name: /Выбрать C1/ })).not.toBeInTheDocument();
  });
  it('reads saved history without enabling edits for a viewer', async () => {
    setup(path => path === '/quotes' ? reply({ items: [quote] }) : undefined);
    render(<UniversalWorkspace {...props} role="viewer"/>);
    fireEvent.click(await screen.findByRole('button', { name: /Открыть КП quote-one/ }));
    expect(await screen.findByText('Итого: 0.14 ₽')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Импортировать прайс' })).not.toBeInTheDocument();
    expect(within(screen.getByRole('region', { name: 'Сохранённый черновик' })).getByRole('link')).toBeInTheDocument();
  });
  it('discards a pending preview after an editable requirement changes', async () => {
    let resolve!: (response: Response) => void;
    setup(path => path === '/matches/preview' ? new Promise(r => { resolve = r; }) : undefined);
    render(<UniversalWorkspace {...props}/>); await extract();
    fireEvent.click(screen.getByLabelText('Проверил все позиции и характеристики'));
    fireEvent.click(screen.getByRole('button', { name: 'Подобрать товары' }));
    fireEvent.change(screen.getByLabelText('Значение характеристики 1 позиции 1'), { target: { value: '5' } });
    await act(async () => resolve(reply({ strategy: 'lexical_universal_v1', requires_manager_selection: true,
      lines: [{ line_number: 1, candidates: [{ sku: 'OLD', name: 'Устаревший товар', checks: [], status: 'compatible', score: 10, reason_codes: [] }] }] })));
    expect(screen.queryByText(/Устаревший товар/)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Сохранить КП' })).toBeDisabled();
  });
  it('requires separate consent and review before saving presentation as a bound new revision', async () => {
    const narrative = { headline: 'Кабель для проекта', opening: 'Добрый день', solution: 'Предлагаем кабель', closing: 'Спасибо' };
    const fetcher = setup(path => path.endsWith('/presentation') ? reply({ narrative, quote_id: quote.quote_id,
      source_revision: 1, source_snapshot_sha256: quote.snapshot_sha256, requires_human_review: true })
      : path.endsWith('/revisions') ? reply({ ...quote, revision: 2, narrative }, 201) : undefined);
    render(<UniversalWorkspace {...props}/>); await extract(); await select();
    fireEvent.click(screen.getByRole('button', { name: 'Сохранить КП' }));
    const generate = await screen.findByRole('button', { name: 'Сгенерировать текст КП' });
    expect(generate).toBeDisabled();
    fireEvent.click(screen.getByLabelText(/Разрешаю передать факты этого КП/)); fireEvent.click(generate);
    await screen.findByDisplayValue('Кабель для проекта');
    const saveText = screen.getByRole('button', { name: 'Сохранить новую версию с текстом' });
    expect(saveText).toBeDisabled();
    fireEvent.click(screen.getByLabelText('Проверил текст предложения и все факты'));
    fireEvent.change(screen.getByLabelText('Заголовок'), { target: { value: 'Проверенный заголовок' } });
    expect(saveText).toBeDisabled();
    fireEvent.click(screen.getByLabelText('Проверил текст предложения и все факты')); fireEvent.click(saveText);
    await screen.findByRole('heading', { name: 'Черновик КП · версия 2' });
    const sent = fetcher.mock.calls.find(([url]) => String(url).endsWith('/revisions'));
    expect(JSON.parse(String(sent?.[1]?.body))).toMatchObject({ expected_revision: 1,
      source_snapshot_sha256: 'snapshot', reviewed: true, narrative: { headline: 'Проверенный заголовок' },
      request: quote.source_request, selections: quote.selections });
    expect(screen.getByText(/Не утверждено/)).toBeInTheDocument();
  });
  it('rejects a presentation response bound to a different snapshot', async () => {
    setup(path => path.endsWith('/presentation') ? reply({ narrative: { headline: 'Чужой текст', opening: 'A', solution: 'B', closing: 'C' },
      quote_id: quote.quote_id, source_revision: 1, source_snapshot_sha256: 'wrong', requires_human_review: true }) : undefined);
    render(<UniversalWorkspace {...props}/>); await extract(); await select();
    fireEvent.click(screen.getByRole('button', { name: 'Сохранить КП' }));
    await screen.findByRole('button', { name: 'Сгенерировать текст КП' });
    fireEvent.click(screen.getByLabelText(/Разрешаю передать факты этого КП/));
    fireEvent.click(screen.getByRole('button', { name: 'Сгенерировать текст КП' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(/не соответствует/);
    expect(screen.queryByDisplayValue('Чужой текст')).not.toBeInTheDocument();
  });
  it('requires explicit ratings, reviewed and training consent before storing feedback', async () => {
    let resolveHistory!: (response: Response) => void;
    const history = new Promise<Response>(resolve => { resolveHistory = resolve; });
    const fetcher = setup(path => {
      if (path.endsWith('/feedback?offset=0')) return history;
      if (path.endsWith('/feedback')) return reply({ id: 'feedback-one', curation_status: 'pending_review' }, 201);
      return undefined;
    });
    render(<UniversalWorkspace {...props}/>); await extract(); await select();
    fireEvent.click(screen.getByRole('button', { name: 'Сохранить КП' }));
    const submit = await screen.findByRole('button', { name: 'Сохранить оценку подбора' });
    const rating = screen.getByLabelText('Оценка товара позиции 1');
    expect(submit).toBeDisabled();
    expect(rating).toBeDisabled();
    expect(screen.getByLabelText(/Разрешаю использовать эти оценки/)).not.toBeChecked();
    await act(async () => resolveHistory(reply({ items: [], next_offset: null })));
    expect(rating).toBeEnabled();
    fireEvent.change(rating, { target: { value: 'positive' } });
    fireEvent.click(screen.getByLabelText('Проверил оценку подбора'));
    expect(submit).toBeDisabled();
    fireEvent.click(screen.getByLabelText(/Разрешаю использовать эти оценки/)); fireEvent.click(submit);
    await screen.findByText(/Оценка сохранена для проверки/);
    const sent = fetcher.mock.calls.find(([url]) => String(url).endsWith('/feedback'));
    expect(JSON.parse(String(sent?.[1]?.body))).toEqual({ expected_revision: 1, source_snapshot_sha256: 'snapshot', reviewed: true,
      training_consent: true, decisions: [{ line_number: 1, relevant_sku: 'C1', negative_skus: [] }] });
    fireEvent.click(screen.getByRole('button', { name: 'Отозвать эту оценку' }));
    await waitFor(() => expect(fetcher.mock.calls.some(([url, options]) => String(url).endsWith('/feedback/feedback-one') && options?.method === 'DELETE')).toBe(true));
  });
  it('collects every export page before downloading feedback without silently truncating', async () => {
    const create = vi.fn((_blob: Blob) => 'blob:feedback'); const revoke = vi.fn();
    vi.stubGlobal('URL', class extends URL { static createObjectURL = create; static revokeObjectURL = revoke; });
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
    const fetcher = setup(path => path === '/feedback/export?offset=0'
      ? reply({ format: 'kpdesk-universal-feedback-v1', items: [{ id: 'first' }], next_offset: 100 })
      : path === '/feedback/export?offset=100'
        ? reply({ format: 'kpdesk-universal-feedback-v1', items: [{ id: 'second' }], next_offset: null }) : undefined);
    render(<UniversalWorkspace {...props}/>); await extract(); await select();
    fireEvent.click(screen.getByRole('button', { name: 'Сохранить КП' }));
    const download = await screen.findByRole('button', { name: 'Скачать оценки подбора' });
    await waitFor(() => expect(download).toBeEnabled()); fireEvent.click(download);
    await waitFor(() => expect(create).toHaveBeenCalledTimes(1));
    expect(fetcher.mock.calls.some(([url]) => String(url).endsWith('/feedback/export?offset=100'))).toBe(true);
    const content = await new Promise<string>(resolve => {
      const reader = new FileReader(); reader.onload = () => resolve(String(reader.result)); reader.readAsText(create.mock.calls[0]![0] as Blob);
    });
    expect(JSON.parse(content).items).toEqual([{ id: 'first' }, { id: 'second' }]);
    expect(JSON.parse(content).training_activated).toBe(false);
    expect(revoke).toHaveBeenCalledWith('blob:feedback');
  });
  it('preserves explicit numeric equality and opaque text digits through manual requirement edits', async () => {
    const fetcher = setup(); render(<UniversalWorkspace {...props}/>);
    await screen.findByText(/Товаров в прайсе: 1/);
    fireEvent.click(screen.getByRole('button', { name: 'Добавить позицию вручную' }));
    fireEvent.change(screen.getByLabelText('Описание позиции 1'), { target: { value: 'Насос' } });
    fireEvent.change(screen.getByLabelText('Цитата из заявки позиции 1'), { target: { value: 'Насос, напор 50 м, код 00123' } });
    fireEvent.click(screen.getByRole('button', { name: 'Добавить характеристику позиции 1' }));
    fireEvent.change(screen.getByLabelText('Название характеристики 1 позиции 1'), { target: { value: 'Напор' } });
    fireEvent.change(screen.getByLabelText('Значение характеристики 1 позиции 1'), { target: { value: '50' } });
    fireEvent.change(screen.getByLabelText('Тип значения характеристики 1 позиции 1'), { target: { value: 'number' } });
    fireEvent.change(screen.getByLabelText('Цитата характеристики 1 позиции 1'), { target: { value: 'напор 50 м' } });
    fireEvent.click(screen.getByRole('button', { name: 'Добавить характеристику позиции 1' }));
    fireEvent.change(screen.getByLabelText('Название характеристики 2 позиции 1'), { target: { value: 'Код' } });
    fireEvent.change(screen.getByLabelText('Значение характеристики 2 позиции 1'), { target: { value: '00123' } });
    fireEvent.change(screen.getByLabelText('Цитата характеристики 2 позиции 1'), { target: { value: 'код 00123' } });
    fireEvent.click(screen.getByLabelText('Проверил все позиции и характеристики'));
    fireEvent.click(screen.getByRole('button', { name: 'Подобрать товары' }));
    await screen.findByRole('radio', { name: /Выбрать C1/ });
    const sent = fetcher.mock.calls.find(([url]) => String(url).endsWith('/matches/preview'));
    const requirements = JSON.parse(String(sent?.[1]?.body)).request.lines[0].requirements;
    expect(requirements[0]).toMatchObject({ name: 'Напор', value: 50, operator: 'eq' });
    expect(requirements[1]).toMatchObject({ name: 'Код', value: '00123', operator: 'eq' });
    expect(requirements[0]).not.toHaveProperty('value_kind');
  });
  it('loads saved feedback after reopening the workspace and allows its withdrawal', async () => {
    const fetcher = setup(path => path === '/quotes' ? reply({ items: [quote] })
      : path === '/quotes/quote-one/feedback?offset=0' ? reply({ items: [{ id: 'earlier-feedback', revision: 1, curation_status: 'pending_review' }], next_offset: null })
        : path === '/feedback/earlier-feedback' ? new Response(null, { status: 204 }) : undefined);
    const first = render(<UniversalWorkspace {...props}/>);
    fireEvent.click(await screen.findByRole('button', { name: /Открыть КП quote-one/ }));
    expect(await screen.findByRole('button', { name: /Отозвать оценку earlier-feedback/ })).toBeInTheDocument();
    first.unmount();
    render(<UniversalWorkspace {...props}/>);
    fireEvent.click(await screen.findByRole('button', { name: /Открыть КП quote-one/ }));
    fireEvent.click(await screen.findByRole('button', { name: /Отозвать оценку earlier-feedback/ }));
    await waitFor(() => expect(fetcher.mock.calls.some(([url, options]) => String(url).endsWith('/feedback/earlier-feedback') && options?.method === 'DELETE')).toBe(true));
    expect(await screen.findByText('Оценка отозвана.')).toBeInTheDocument();
  });
  it('explains failed candidate checks without exposing internal reason codes', async () => {
    setup(path => path === '/matches/preview' ? reply({ strategy: 'lexical_universal_v1', requires_manager_selection: true,
      lines: [{ line_number: 1, candidates: [{ sku: 'C1', name: 'Кабель', score: 50, status: 'needs_review', reason_codes: [],
        checks: [{ name: 'model', status: 'incompatible', reason: 'model_mismatch' },
          { name: 'Сечение', status: 'needs_review', reason: 'spec_missing' },
          { name: 'quantity', status: 'incompatible', reason: 'unit_mismatch' },
          { name: 'Покрытие', status: 'needs_review', reason: 'future_internal_check_v2' }] }] }] }) : undefined);
    render(<UniversalWorkspace {...props}/>); await extract(); await select();
    expect(screen.getByText(/Модель:.*Модель не совпадает/)).toBeInTheDocument();
    expect(screen.getByText(/Сечение:.*Характеристика отсутствует в прайсе/)).toBeInTheDocument();
    expect(screen.getByText(/Количество:.*Единицы измерения не совпадают/)).toBeInTheDocument();
    expect(screen.getByText(/Покрытие:.*Нужна проверка менеджером/)).toBeInTheDocument();
    expect(screen.queryByText(/model_mismatch|spec_missing|unit_mismatch|future_internal_check_v2/)).not.toBeInTheDocument();
  });
});
