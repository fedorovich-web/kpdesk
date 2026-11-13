import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { sessionApi } from '../tenant-session';
import type { DocumentAlternativePreview, TenantQuote } from '../tenant-session';
import { QuoteDocumentAlternatives } from './QuoteDocumentAlternatives';

afterEach(() => vi.restoreAllMocks());
const quote = { quote_id: 'q', revision: 1, snapshot_sha256: 'a'.repeat(64), catalog_version_id: 'c' } as TenantQuote;
const preview: DocumentAlternativePreview = { quote_id: 'q', source_revision: 1,
  source_snapshot_sha256: quote.snapshot_sha256, candidate_limit_per_line: 20, alternatives: [{
    catalog_version_id: 'c', selection: { kind: 'lower_cost', selections: [
      { line_number: 1, sku: 'SECOND', approved_sale_quantity: null },
    ] }, total_rub: '60.00', lines: [{ line_number: 1, sku: 'SECOND', product_name: 'Бумага A4',
      sale_quantity: 2, sale_unit: 'пачка', line_total_rub: '60.00' } as TenantQuote['lines'][number]],
  }] };
const props = { organizationId: 'org', quote, csrf: 'csrf', disabled: false, onChange: vi.fn() };

it('loads server compositions without selecting them or calling AI, then explicitly includes a choice', async () => {
  const load = vi.spyOn(sessionApi, 'previewDocumentAlternatives').mockResolvedValue(preview);
  const changed = vi.fn();
  render(<QuoteDocumentAlternatives {...props} onChange={changed}/>);
  fireEvent.click(screen.getByRole('button', { name: 'Подобрать другие варианты для клиента' }));
  expect(await screen.findByText('Бумага A4')).toBeInTheDocument();
  expect(changed).not.toHaveBeenCalled();
  fireEvent.click(screen.getByLabelText(/Добавить этот вариант/));
  expect(changed).toHaveBeenCalledWith(preview.alternatives);
  expect(load).toHaveBeenCalledWith('org', quote, 'csrf');
});

it('ignores a late response after the saved quote changes', async () => {
  let resolve!: (value: DocumentAlternativePreview) => void;
  vi.spyOn(sessionApi, 'previewDocumentAlternatives').mockImplementation(() => new Promise(r => { resolve = r; }));
  const view = render(<QuoteDocumentAlternatives {...props}/>);
  fireEvent.click(screen.getByRole('button', { name: 'Подобрать другие варианты для клиента' }));
  view.rerender(<QuoteDocumentAlternatives {...props} quote={{ ...quote, revision: 2 }}/>);
  await act(async () => resolve(preview));
  expect(screen.queryByText('Бумага A4')).not.toBeInTheDocument();
  await waitFor(() => expect(screen.getByRole('button', { name: 'Подобрать другие варианты для клиента' })).toBeEnabled());
});
