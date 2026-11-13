import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { catalogFileError, ImportCatalogPage } from './ImportCatalogPage';

describe('real catalog import #12', () => {
  it('offers only the accepted synthetic CSV and browser steps in public demo', () => {
    render(<ImportCatalogPage isDemo file={new File(['sheet'], 'price.xlsx')}
      onFileChange={vi.fn()} onSubmit={vi.fn()} busy={false} onBack={vi.fn()}/>);
    expect(screen.getByRole('link', { name: /Скачать учебный прайс CSV/ }))
      .toHaveAttribute('href', '/templates/kpdesk-portfolio-synthetic-v1.csv');
    expect(screen.getByLabelText('Файл прайс-листа')).toHaveAttribute('accept', '.csv');
    expect(screen.getByRole('alert')).toHaveTextContent('только учебный CSV');
    expect(screen.queryByText('Проверка товаров и цен')).not.toBeInTheDocument();
    expect(screen.queryByText(/Максимум 10 МиБ/)).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /шаблон Excel/ })).not.toBeInTheDocument();
    expect(screen.getByText(/до 1 МиБ/)).toBeInTheDocument();
  });
  it('rejects unsupported, empty and oversized files before transport', () => {
    expect(catalogFileError(new File(['test'], 'bad.xls'))).toMatch(/CSV и XLSX/);
    expect(catalogFileError(new File([], 'empty.csv'))).toMatch(/пустой/);
    expect(catalogFileError(new File([new Uint8Array(10 * 1024 * 1024 + 1)], 'huge.csv'))).toMatch(/10 МиБ/);
    expect(catalogFileError(new File(['sku;name'], 'valid.csv'))).toBeNull();
  });

  it('lists exact backend requirements, a real template, and a gated import action', () => {
    const onSubmit = vi.fn();
    render(<ImportCatalogPage file={null} onFileChange={vi.fn()} onSubmit={onSubmit}
      busy={false} onBack={vi.fn()}/>);
    expect(screen.getByRole('heading', { name: 'Импорт прайс-листа' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Проверить и загрузить/ })).toBeDisabled();
    expect(screen.getByRole('link', { name: /Скачать шаблон Excel/ }))
      .toHaveAttribute('href', '/templates/kpdesk-catalog-v2-template.xlsx');
    expect(screen.getByRole('link', { name: /Скачать шаблон CSV/ }))
      .toHaveAttribute('href', '/templates/kpdesk-catalog-v2-columns.csv');
    expect(screen.getByRole('link', { name: /Скачать JSON-шаблон/ }))
      .toHaveAttribute('href', '/templates/kpdesk-catalog-v2-template.json');
    expect(screen.getByText(/листом Products/)).toBeInTheDocument();
    expect(screen.getByText(/Другие прайсы и выгрузки 1С/)).toBeInTheDocument();
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it('responds to actual file selections and blocks a server mutation while invalid', () => {
    const onFileChange = vi.fn();
    render(<ImportCatalogPage file={new File(['data'], 'bad.xls')} onFileChange={onFileChange}
      onSubmit={vi.fn()} busy={false} onBack={vi.fn()}/>);
    expect(screen.getByRole('alert')).toHaveTextContent('Поддерживаются только CSV и XLSX.');
    expect(screen.getByRole('button', { name: /Проверить и загрузить/ })).toBeDisabled();
    fireEvent.change(screen.getByLabelText('Файл прайс-листа'), {
      target: { files: [new File(['header'], 'price.csv')] },
    });
    expect(onFileChange).toHaveBeenCalledOnce();
  });
});
