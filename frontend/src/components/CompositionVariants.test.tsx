import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { sessionApi } from '../tenant-session';
import type { TenantVariantsResponse } from '../tenant-session';
import { CompositionVariants } from './CompositionVariants';

afterEach(() => vi.restoreAllMocks());
const response: TenantVariantsResponse = {
  catalog_version_id: 'catalog', mode: 'tfidf', candidate_limit_per_line: 20,
  lines: [{ line_number: 1, catalog_version_id: 'catalog', mode: 'tfidf',
    requires_manager_confirmation: true, calibrated_no_match: false,
    candidates: [{ sku: 'A', name: 'Бумага A4', price_rub: '12.30', sale_unit: 'пачка',
      pieces_per_sale_unit: 500, score: .9, status: 'compatible', reason_codes: [] }] }],
  variants: [{ kind: 'relevance', lines: [{ line_number: 1, sku: 'A', name: 'Бумага A4',
    sale_quantity: 2, sale_unit: 'пачка', line_total_rub: '24.60' }],
    unresolved_line_numbers: [], total_rub: '24.60', budget_exceeded: false, selectable: true }],
};
const props = { organizationId: 'org', catalogId: 'catalog', csrf: 'csrf', identity: 'one',
  requestedItems: [{}], enabled: true, onApply: vi.fn() };

describe('catalog composition comparison', () => {
  it('requires review, then transfers the real server selection', async () => {
    const preview = vi.spyOn(sessionApi, 'previewVariants').mockResolvedValue(response);
    const apply = vi.fn();
    render(<CompositionVariants {...props} onApply={apply}/>);
    fireEvent.click(screen.getByRole('button', { name: 'Сравнить варианты комплектации' }));
    const choose = await screen.findByRole('button', { name: 'Использовать этот вариант' });
    expect(choose).toBeDisabled();
    expect(screen.getByText('Бумага A4')).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText(/Проверил состав/));
    fireEvent.click(choose);
    expect(apply).toHaveBeenCalledWith(response, response.variants[0]);
    expect(preview).toHaveBeenCalledWith('org', 'catalog', [{}], 'csrf', undefined);
  });
  it('shows unresolved rows and prevents incomplete or over-budget choices', async () => {
    vi.spyOn(sessionApi, 'previewVariants').mockResolvedValue({ ...response,
      variants: [{ ...response.variants[0]!, selectable: false, total_rub: null,
        unresolved_line_numbers: [1] }, { ...response.variants[0]!, kind: 'lower_cost',
        selectable: false, budget_exceeded: true }] });
    render(<CompositionVariants {...props}/>);
    fireEvent.click(screen.getByRole('button', { name: 'Сравнить варианты комплектации' }));
    await screen.findByText(/Требуют уточнения позиции: 1/);
    expect(screen.getByText(/Превышен подтверждённый бюджет/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Использовать этот вариант' })).not.toBeInTheDocument();
  });
  it('discards a response after the request identity changes', async () => {
    let resolve!: (value: TenantVariantsResponse) => void;
    vi.spyOn(sessionApi, 'previewVariants').mockImplementation(() => new Promise(r => { resolve = r; }));
    const view = render(<CompositionVariants {...props}/>);
    fireEvent.click(screen.getByRole('button', { name: 'Сравнить варианты комплектации' }));
    view.rerender(<CompositionVariants {...props} identity="edited"/>);
    await act(async () => { resolve(response); });
    expect(screen.queryByText('Бумага A4')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Сравнить варианты комплектации' })).toBeEnabled();
  });
  it('rejects a candidate response for a different catalog', async () => {
    vi.spyOn(sessionApi, 'previewVariants').mockResolvedValue({ ...response, catalog_version_id: 'old' });
    render(<CompositionVariants {...props}/>);
    fireEvent.click(screen.getByRole('button', { name: 'Сравнить варианты комплектации' }));
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent(/не соответствует/));
    expect(screen.queryByRole('button', { name: 'Использовать этот вариант' })).not.toBeInTheDocument();
  });
});
