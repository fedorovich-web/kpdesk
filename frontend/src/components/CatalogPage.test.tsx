import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { CatalogPage } from './CatalogPage';
import type { CatalogProduct } from '../domain';

const product = (index: number): CatalogProduct => ({
  sku: 'SKU-' + String(index).padStart(3, '0'),
  name: 'Товар ' + String(index).padStart(3, '0'),
  category: index % 2 === 0 ? 'paper' : 'pen',
  price_rub: String(index * 10) + '.00',
  sale_unit: 'штука',
  pieces_per_sale_unit: 1,
  brand: 'Тестовая марка',
  attributes: {},
});

describe('real catalog listing #11', () => {
  it('shows empty state instead of synthetic products', () => {
    const importAction = vi.fn();
    render(<CatalogPage catalog={null} products={[]} query=""
      onQueryChange={vi.fn()} onImport={importAction}/>);
    expect(screen.getByRole('heading', { name: 'Прайс-лист' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Прайс-лист пуст' })).toBeInTheDocument();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
    fireEvent.click(screen.getAllByRole('button', { name: /Загрузить прайс/ })[0]!);
    expect(importAction).toHaveBeenCalledOnce();
  });

  it('does not show fictional zero counters while backend data cannot be verified', () => {
    render(<CatalogPage catalog={null} products={[]} query="" onQueryChange={vi.fn()}
      onImport={vi.fn()} loadError="HTTP 500"/>);
    expect(screen.getByRole('heading', { name: 'Не удалось открыть прайс' })).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Прайс-лист пуст' })).not.toBeInTheDocument();
    expect(screen.queryByText('Всего товаров')).not.toBeInTheDocument();
  });

  it('sorts only supplied API rows, without inventing availability or statuses', () => {
    render(<CatalogPage catalog={{ id: 'sample', rows_count: 3, source_sha256: 'test' }}
      products={[product(3), product(1), product(2)]} query="" onQueryChange={vi.fn()} onImport={vi.fn()}/>);
    const table = screen.getByRole('table');
    expect(within(table).getAllByRole('row')).toHaveLength(4);
    fireEvent.click(screen.getByRole('button', { name: 'Сортировать по цене' }));
    const cells = within(table).getAllByRole('row').slice(1).map(row =>
      within(row).getAllByRole('cell')[1]?.textContent);
    expect(cells).toEqual(['SKU-001', 'SKU-002', 'SKU-003']);
    fireEvent.click(screen.getByRole('button', { name: 'Сортировать по цене' }));
    const desc = within(table).getAllByRole('row').slice(1).map(row =>
      within(row).getAllByRole('cell')[1]?.textContent);
    expect(desc).toEqual(['SKU-003', 'SKU-002', 'SKU-001']);
    expect(screen.queryByText(/В наличии|Наличие на складе/)).not.toBeInTheDocument();
  });

  it('paginates 21 API items and resets after selecting a category', () => {
    render(<CatalogPage catalog={null} products={Array.from({ length: 21 }, (_, i) => product(i + 1))}
      query="" onQueryChange={vi.fn()} onImport={vi.fn()}/>);
    expect(screen.getByText('Показано 1–8 из 21')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Следующая страница' }));
    expect(screen.getByText('Показано 9–16 из 21')).toBeInTheDocument();
    fireEvent.change(screen.getByRole('combobox', { name: 'Категория' }), { target: { value: 'paper' } });
    expect(screen.getByText('Показано 1–8 из 10')).toBeInTheDocument();
  });
});
