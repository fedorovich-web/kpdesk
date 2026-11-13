import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { sessionApi } from '../tenant-session';
import type { CompanyLogo, PartyLookupResponse } from '../tenant-session';
import { CompanyProfileSettings } from './CompanyProfileSettings';

const registryParty = { full_name: 'Общество Ромашка', short_name: 'ООО Ромашка', inn: '7707083893',
  kpp: '770701001', ogrn: '1027700132195', address: 'Москва', status: 'ACTIVE' };
const logo: CompanyLogo = { mime_type: 'image/png', width: 40, height: 20, sha256: 'd'.repeat(64),
  data_base64: 'iVBORw0KGgo=' };

afterEach(() => vi.restoreAllMocks());
describe('one-time company facts', () => {
  it('prepares and previews a logo without saving the profile until renewed review', async () => {
    vi.spyOn(sessionApi, 'companyProfile').mockResolvedValue({ profile: null });
    const prepare = vi.spyOn(sessionApi, 'prepareCompanyLogo').mockResolvedValue({ logo });
    const save = vi.spyOn(sessionApi, 'saveCompanyProfile').mockImplementation(() => new Promise(() => {}));
    render(<CompanyProfileSettings organizationId="org" organizationName="Компания" csrf="csrf" canManage onSaved={vi.fn()}/>);
    await screen.findByLabelText('Логотип компании');
    expect(screen.getByAltText('Логотип KPDesk по умолчанию')).toHaveAttribute('src', '/assets/kpdesk-default-v1.png');
    fireEvent.change(screen.getByLabelText('Контакты для КП'), { target: { value: 'sales@example.invalid' } });
    fireEvent.click(screen.getByLabelText(/Подтверждаю достоверность/));
    const file = new File(['synthetic'], 'company.png', { type: 'image/png' });
    fireEvent.change(screen.getByLabelText('Логотип компании'), { target: { files: [file] } });
    expect(await screen.findByAltText('Логотип компании')).toHaveAttribute('src', `data:image/png;base64,${logo.data_base64}`);
    expect(screen.queryByAltText('Логотип KPDesk по умолчанию')).not.toBeInTheDocument();
    expect(prepare).toHaveBeenCalledWith('org', file, 'csrf');
    expect(save).not.toHaveBeenCalled();
    expect(screen.getByLabelText(/Подтверждаю достоверность/)).not.toBeChecked();
    fireEvent.click(screen.getByLabelText(/Подтверждаю достоверность/));
    fireEvent.click(screen.getByRole('button', { name: 'Сохранить настройки компании' }));
    await waitFor(() => expect(save).toHaveBeenCalledWith('org', expect.objectContaining({ logo,
      contacts: 'sales@example.invalid' }), 0, 'csrf'));
  });
  it('ignores a prepared logo after the selected organization changes', async () => {
    vi.spyOn(sessionApi, 'companyProfile').mockResolvedValue({ profile: null });
    let resolve!: (value: { logo: CompanyLogo }) => void;
    vi.spyOn(sessionApi, 'prepareCompanyLogo').mockImplementation(() => new Promise(done => { resolve = done; }));
    const view = render(<CompanyProfileSettings organizationId="old" organizationName="Первая" csrf="csrf" canManage onSaved={vi.fn()}/>);
    await screen.findByLabelText('Логотип компании');
    fireEvent.change(screen.getByLabelText('Логотип компании'), { target: { files: [new File(['image'], 'image.png')] } });
    view.rerender(<CompanyProfileSettings organizationId="new" organizationName="Вторая" csrf="csrf-new" canManage onSaved={vi.fn()}/>);
    await screen.findByLabelText('Логотип компании');
    await act(async () => resolve({ logo }));
    expect(screen.queryByAltText('Логотип компании')).not.toBeInTheDocument();
    expect(screen.getByLabelText('Название отправителя')).toHaveValue('Вторая');
  });
  it('retains a saved logo on upload error and requires review when removing it', async () => {
    vi.spyOn(sessionApi, 'companyProfile').mockResolvedValue({ profile: { revision: 2, sha256: 'a'.repeat(64),
      data: { schema_version: 1, display_name: 'Компания', about: '', advantages: '', contacts: '',
        default_terms: '', generation_style: '', logo } } });
    vi.spyOn(sessionApi, 'prepareCompanyLogo').mockRejectedValue(new Error('Неверный файл'));
    render(<CompanyProfileSettings organizationId="org" organizationName="Компания" csrf="csrf" canManage onSaved={vi.fn()}/>);
    await screen.findByAltText('Логотип компании');
    fireEvent.click(screen.getByLabelText(/Подтверждаю достоверность/));
    fireEvent.change(screen.getByLabelText('Логотип компании'), { target: { files: [new File(['bad'], 'bad.svg')] } });
    expect(await screen.findByRole('alert')).toHaveTextContent('Черновик не изменён');
    expect(screen.getByAltText('Логотип компании')).toBeInTheDocument();
    expect(screen.getByLabelText(/Подтверждаю достоверность/)).toBeChecked();
    fireEvent.click(screen.getByRole('button', { name: 'Убрать логотип' }));
    expect(screen.queryByAltText('Логотип компании')).not.toBeInTheDocument();
    expect(screen.getByAltText('Логотип KPDesk по умолчанию')).toBeInTheDocument();
    expect(screen.getByLabelText(/Подтверждаю достоверность/)).not.toBeChecked();
  });
  it('requires owner attestation and saves the exact version without an AI call', async () => {
    vi.spyOn(sessionApi, 'companyProfile').mockResolvedValue({ profile: null });
    const save = vi.spyOn(sessionApi, 'saveCompanyProfile').mockResolvedValue({ profile: {
      revision: 1, sha256: 'a'.repeat(64), data: { schema_version: 1, display_name: 'Компания',
        about: 'Офисные товары', advantages: '', contacts: '', default_terms: '', generation_style: '' },
    } });
    const changed = vi.fn();
    render(<CompanyProfileSettings organizationId="org" organizationName="Компания" csrf="csrf"
      canManage onSaved={changed}/>);
    await screen.findByLabelText('Описание компании');
    fireEvent.change(screen.getByLabelText('Описание компании'), { target: { value: 'Офисные товары' } });
    expect(screen.getByRole('button', { name: 'Сохранить настройки компании' })).toBeDisabled();
    fireEvent.click(screen.getByLabelText(/Подтверждаю достоверность/));
    fireEvent.click(screen.getByRole('button', { name: 'Сохранить настройки компании' }));
    await waitFor(() => expect(save).toHaveBeenCalledWith('org', expect.objectContaining({
      about: 'Офисные товары', display_name: 'Компания',
    }), 0, 'csrf'));
    expect(changed).toHaveBeenCalledOnce();
  });
  it('does not offer editing for an ordinary organization member', async () => {
    vi.spyOn(sessionApi, 'companyProfile').mockResolvedValue({ profile: null });
    render(<CompanyProfileSettings organizationId="org" organizationName="Компания" csrf="csrf"
      canManage={false} onSaved={vi.fn()}/>);
    expect(await screen.findByText(/Руководитель ещё не сохранил/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Сохранить настройки компании' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Найти по ИНН' })).not.toBeInTheDocument();
  });
  it('preserves a dirty draft and clears pending save after session scope changes', async () => {
    vi.spyOn(sessionApi, 'companyProfile').mockResolvedValue({ profile: null });
    vi.spyOn(sessionApi, 'saveCompanyProfile').mockImplementation(() => new Promise(() => {}));
    const view = render(<CompanyProfileSettings organizationId="org" organizationName="Компания" csrf="old"
      canManage onSaved={vi.fn()}/>);
    await screen.findByLabelText('Описание компании');
    fireEvent.change(screen.getByLabelText('Описание компании'), { target: { value: 'Несохранённая правка' } });
    fireEvent.click(screen.getByLabelText(/Подтверждаю достоверность/));
    fireEvent.click(screen.getByRole('button', { name: 'Сохранить настройки компании' }));
    view.rerender(<CompanyProfileSettings organizationId="org" organizationName="Компания" csrf="new"
      canManage onSaved={vi.fn()}/>);
    await waitFor(() => expect(screen.getByRole('button', { name: 'Загрузить сохранённую версию' })).toBeEnabled());
    expect(screen.getByLabelText('Описание компании')).toHaveValue('Несохранённая правка');
  });
  it('applies reviewed registry details only explicitly, preserves contacts and resets attestation', async () => {
    vi.spyOn(sessionApi, 'companyProfile').mockResolvedValue({ profile: null });
    vi.spyOn(sessionApi, 'lookupCompany').mockResolvedValue({ party: registryParty, source: 'dadata',
      fetched_at: '2026-10-28T10:00:00Z', cached: false });
    const save = vi.spyOn(sessionApi, 'saveCompanyProfile');
    render(<CompanyProfileSettings organizationId="org" organizationName="Компания" csrf="csrf"
      canManage onSaved={vi.fn()}/>);
    await screen.findByLabelText('Контакты для КП');
    fireEvent.change(screen.getByLabelText('Контакты для КП'), { target: { value: '+7 999 123-45-67\nhello@example.ru' } });
    fireEvent.change(screen.getByLabelText('Описание компании'), { target: { value: 'Наше описание' } });
    fireEvent.click(screen.getByLabelText(/Подтверждаю достоверность/));
    fireEvent.change(screen.getByLabelText('ИНН компании'), { target: { value: registryParty.inn } });
    fireEvent.click(screen.getByRole('button', { name: 'Найти по ИНН' }));
    await screen.findByRole('button', { name: 'Добавить реквизиты' });
    expect(screen.getByLabelText('Название отправителя')).toHaveValue('Компания');
    expect(screen.getByLabelText(/Подтверждаю достоверность/)).toBeChecked();
    fireEvent.click(screen.getByRole('button', { name: 'Добавить реквизиты' }));
    expect(screen.getByLabelText('Название отправителя')).toHaveValue(registryParty.short_name);
    expect(screen.getByLabelText('Контакты для КП')).toHaveValue('+7 999 123-45-67\nhello@example.ru');
    expect(screen.getByLabelText('Реквизиты отправителя')).toHaveTextContent(registryParty.inn);
    expect(screen.getByLabelText('Описание компании')).toHaveValue('Наше описание');
    expect(screen.getByLabelText(/Подтверждаю достоверность/)).not.toBeChecked();
    expect(screen.getByText(/Загрузка сохранённой версии заменит несохранённые правки/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Добавить реквизиты' }));
    expect(screen.getByLabelText('Контакты для КП')).toHaveValue('+7 999 123-45-67\nhello@example.ru');
    expect(save).not.toHaveBeenCalled();
  });
  it('saves structured legal details separately even when contacts use the full limit', async () => {
    vi.spyOn(sessionApi, 'companyProfile').mockResolvedValue({ profile: null });
    vi.spyOn(sessionApi, 'lookupCompany').mockResolvedValue({ party: registryParty, source: 'dadata',
      fetched_at: '2026-10-28T10:00:00Z', cached: false });
    const save = vi.spyOn(sessionApi, 'saveCompanyProfile').mockImplementation(() => new Promise(() => {}));
    render(<CompanyProfileSettings organizationId="org" organizationName="Компания" csrf="csrf"
      canManage onSaved={vi.fn()}/>);
    await screen.findByLabelText('Контакты для КП');
    fireEvent.change(screen.getByLabelText('Контакты для КП'), { target: { value: 'a'.repeat(600) } });
    fireEvent.click(screen.getByLabelText(/Подтверждаю достоверность/));
    fireEvent.change(screen.getByLabelText('ИНН компании'), { target: { value: registryParty.inn } });
    fireEvent.click(screen.getByRole('button', { name: 'Найти по ИНН' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Добавить реквизиты' }));
    expect(screen.getByLabelText('Контакты для КП')).toHaveValue('a'.repeat(600));
    expect(screen.getByLabelText('Название отправителя')).toHaveValue(registryParty.short_name);
    expect(screen.getByLabelText(/Подтверждаю достоверность/)).not.toBeChecked();
    fireEvent.click(screen.getByLabelText(/Подтверждаю достоверность/));
    fireEvent.click(screen.getByRole('button', { name: 'Сохранить настройки компании' }));
    await waitFor(() => expect(save).toHaveBeenCalledWith('org', expect.objectContaining({
      contacts: 'a'.repeat(600), issuer_details: registryParty,
    }), 0, 'csrf'));
  });
  it('keeps contact edits made during lookup and uses the current draft on Apply', async () => {
    vi.spyOn(sessionApi, 'companyProfile').mockResolvedValue({ profile: null });
    let resolve!: (value: PartyLookupResponse) => void;
    vi.spyOn(sessionApi, 'lookupCompany').mockImplementation(() => new Promise(done => { resolve = done; }));
    render(<CompanyProfileSettings organizationId="org" organizationName="Компания" csrf="csrf"
      canManage onSaved={vi.fn()}/>);
    await screen.findByLabelText('Контакты для КП');
    fireEvent.change(screen.getByLabelText('ИНН компании'), { target: { value: registryParty.inn } });
    fireEvent.click(screen.getByRole('button', { name: 'Найти по ИНН' }));
    fireEvent.change(screen.getByLabelText('Контакты для КП'), { target: { value: 'Новый телефон: +7 999 123-45-67' } });
    await act(async () => resolve({ party: registryParty, source: 'dadata', fetched_at: '2026-10-28T10:00:00Z', cached: false }));
    expect(screen.getByLabelText('Контакты для КП')).toHaveValue('Новый телефон: +7 999 123-45-67');
    fireEvent.click(screen.getByRole('button', { name: 'Добавить реквизиты' }));
    expect(screen.getByLabelText('Контакты для КП')).toHaveValue('Новый телефон: +7 999 123-45-67');
    expect(screen.getByLabelText('Реквизиты отправителя')).toHaveTextContent(registryParty.inn);
  });
  it('loads saved sender details and requires renewed review when removing them', async () => {
    vi.spyOn(sessionApi, 'companyProfile').mockResolvedValue({ profile: {
      revision: 3, sha256: 'b'.repeat(64), data: { schema_version: 1, display_name: 'Компания',
        about: '', advantages: '', contacts: 'sales@example.invalid', default_terms: '',
        generation_style: '', issuer_details: registryParty },
    } });
    const save = vi.spyOn(sessionApi, 'saveCompanyProfile').mockImplementation(() => new Promise(() => {}));
    render(<CompanyProfileSettings organizationId="org" organizationName="Компания" csrf="csrf"
      canManage onSaved={vi.fn()}/>);
    expect(await screen.findByLabelText('Реквизиты отправителя')).toHaveTextContent(registryParty.inn);
    fireEvent.click(screen.getByLabelText(/Подтверждаю достоверность/));
    fireEvent.click(screen.getByRole('button', { name: 'Убрать реквизиты отправителя' }));
    expect(screen.queryByLabelText('Реквизиты отправителя')).not.toBeInTheDocument();
    expect(screen.getByLabelText(/Подтверждаю достоверность/)).not.toBeChecked();
    fireEvent.click(screen.getByLabelText(/Подтверждаю достоверность/));
    fireEvent.click(screen.getByRole('button', { name: 'Сохранить настройки компании' }));
    await waitFor(() => expect(save).toHaveBeenCalledWith('org', expect.objectContaining({
      contacts: 'sales@example.invalid', issuer_details: undefined,
    }), 3, 'csrf'));
  });
});
