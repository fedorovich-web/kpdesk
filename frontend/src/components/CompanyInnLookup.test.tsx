import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { sessionApi } from '../tenant-session';
import type { PartyLookupResponse } from '../tenant-session';
import { CompanyInnLookup } from './CompanyInnLookup';

const party = { full_name: 'Общество Ромашка', short_name: 'ООО Ромашка', inn: '7707083893',
  kpp: '770701001', ogrn: '1027700132195', address: 'Москва', status: 'ACTIVE' };
const response: PartyLookupResponse = { party, source: 'dadata', fetched_at: '2026-10-28T10:00:00Z', cached: false };
const enter = (inn = party.inn) => fireEvent.change(screen.getByLabelText('ИНН компании'), { target: { value: inn } });
afterEach(() => vi.restoreAllMocks());

describe('company registry lookup', () => {
  it('only searches on explicit click and applies only on explicit review action', async () => {
    const lookup = vi.spyOn(sessionApi, 'lookupCompany').mockResolvedValue(response);
    const apply = vi.fn();
    render(<CompanyInnLookup organizationId="org" csrf="csrf" onApply={apply}/>);
    enter();
    expect(lookup).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Найти по ИНН' }));
    await screen.findByText(party.short_name);
    expect(lookup).toHaveBeenCalledWith('org', party.inn, 'csrf');
    expect(apply).not.toHaveBeenCalled();
    expect(screen.getByText(party.full_name)).toBeInTheDocument();
    expect(screen.getByText('Реквизиты и статус в реестре').closest('details')).not.toHaveAttribute('open');
    expect(screen.getByRole('link', { name: 'DaData' })).toHaveAttribute('href', 'https://dadata.ru/api/find-party/');
    fireEvent.click(screen.getByRole('button', { name: 'Добавить реквизиты' }));
    expect(apply).toHaveBeenCalledWith(party);
  });

  it('rejects non-ASCII and wrong-length INN locally', () => {
    const lookup = vi.spyOn(sessionApi, 'lookupCompany');
    render(<CompanyInnLookup organizationId="org" csrf="csrf" onApply={vi.fn()}/>);
    for (const inn of ['123', '７７０７０８３８９３', '770708389a']) {
      enter(inn);
      fireEvent.click(screen.getByRole('button', { name: 'Найти по ИНН' }));
      expect(screen.getByRole('alert')).toHaveTextContent('10 или 12 цифр');
    }
    expect(lookup).not.toHaveBeenCalled();
  });

  it('supports a twelve-digit sole proprietor INN and reports the retrieval date and cache', async () => {
    const lookup = vi.spyOn(sessionApi, 'lookupCompany').mockResolvedValue({ ...response, cached: true,
      party: { ...party, inn: '500100732259', kpp: null, ogrn: null } });
    render(<CompanyInnLookup organizationId="org" csrf="csrf" onApply={vi.fn()}/>);
    enter('500100732259'); fireEvent.click(screen.getByRole('button', { name: 'Найти по ИНН' }));
    await screen.findByText(party.short_name);
    expect(lookup).toHaveBeenCalledWith('org', '500100732259', 'csrf');
    expect(screen.getByText('КПП').nextElementSibling).toHaveTextContent('Не указано');
    expect(document.querySelector('time')).toHaveAttribute('datetime', response.fetched_at);
    expect(screen.getByText(/из кеша/)).toBeInTheDocument();
  });

  it('keeps lookup disabled without a current CSRF token', () => {
    const lookup = vi.spyOn(sessionApi, 'lookupCompany');
    render(<CompanyInnLookup organizationId="org" csrf={null} onApply={vi.fn()}/>);
    expect(screen.getByLabelText('ИНН компании')).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Найти по ИНН' })).toBeDisabled();
    expect(lookup).not.toHaveBeenCalled();
  });

  it('discards a late result after editing INN and clears an existing preview', async () => {
    let resolve!: (value: PartyLookupResponse) => void;
    vi.spyOn(sessionApi, 'lookupCompany').mockImplementationOnce(() => new Promise(done => { resolve = done; }))
      .mockResolvedValue(response);
    render(<CompanyInnLookup organizationId="org" csrf="csrf" onApply={vi.fn()}/>);
    enter(); fireEvent.click(screen.getByRole('button', { name: 'Найти по ИНН' }));
    enter('500100732259');
    await act(async () => resolve(response));
    expect(screen.queryByRole('button', { name: 'Добавить реквизиты' })).not.toBeInTheDocument();
    enter(); fireEvent.click(screen.getByRole('button', { name: 'Найти по ИНН' }));
    await screen.findByText(party.short_name);
    enter('500100732259');
    expect(screen.queryByText(party.short_name)).not.toBeInTheDocument();
  });

  it.each(['organization', 'csrf', 'disabled'] as const)('discards a late result after %s changes', async change => {
    let resolve!: (value: PartyLookupResponse) => void;
    vi.spyOn(sessionApi, 'lookupCompany').mockImplementation(() => new Promise(done => { resolve = done; }));
    const apply = vi.fn();
    const view = render(<CompanyInnLookup organizationId="org" csrf="csrf" onApply={apply}/>);
    enter(); fireEvent.click(screen.getByRole('button', { name: 'Найти по ИНН' }));
    view.rerender(<CompanyInnLookup organizationId={change === 'organization' ? 'new' : 'org'}
      csrf={change === 'csrf' ? 'new' : 'csrf'} disabled={change === 'disabled'} onApply={apply}/>);
    await act(async () => resolve(response));
    expect(screen.queryByRole('button', { name: 'Добавить реквизиты' })).not.toBeInTheDocument();
    expect(apply).not.toHaveBeenCalled();
  });

  it('does not apply pending results after unmount', async () => {
    let resolve!: (value: PartyLookupResponse) => void;
    vi.spyOn(sessionApi, 'lookupCompany').mockImplementation(() => new Promise(done => { resolve = done; }));
    const apply = vi.fn();
    const view = render(<CompanyInnLookup organizationId="org" csrf="csrf" onApply={apply}/>);
    enter(); fireEvent.click(screen.getByRole('button', { name: 'Найти по ИНН' }));
    view.unmount();
    await act(async () => resolve(response));
    expect(apply).not.toHaveBeenCalled();
  });

  it('reports not found, provider errors and a non-active registry status', async () => {
    vi.spyOn(sessionApi, 'lookupCompany').mockResolvedValueOnce({ ...response, party: null })
      .mockRejectedValueOnce(new Error('Поиск реквизитов не настроен.'))
      .mockResolvedValueOnce({ ...response, party: { ...party, status: 'LIQUIDATED' } });
    render(<CompanyInnLookup organizationId="org" csrf="csrf" onApply={vi.fn()}/>);
    enter(); fireEvent.click(screen.getByRole('button', { name: 'Найти по ИНН' }));
    await screen.findByText('Компания по этому ИНН не найдена.');
    fireEvent.click(screen.getByRole('button', { name: 'Найти по ИНН' }));
    await screen.findByText('Поиск реквизитов не настроен.');
    fireEvent.click(screen.getByRole('button', { name: 'Найти по ИНН' }));
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('LIQUIDATED'));
  });
});
