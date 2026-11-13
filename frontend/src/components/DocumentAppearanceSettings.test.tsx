import { fireEvent, render, screen } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { defaultDocumentAppearance, DocumentAppearanceSettings } from './DocumentAppearanceSettings';

it('selects a layout, corporate color and company block through explicit controls', () => {
  const changed = vi.fn();
  render(<DocumentAppearanceSettings value={{ template: 'presentation', accent_color: '#007F5F', show_company_section: true }}
    disabled={false} hasCompanyFacts onChange={changed}/>);
  fireEvent.click(screen.getByLabelText(/Краткое предложение/));
  expect(changed).toHaveBeenLastCalledWith(expect.objectContaining({ template: 'compact' }));
  fireEvent.change(screen.getByLabelText('Фирменный цвет'), { target: { value: '#123456' } });
  expect(changed).toHaveBeenLastCalledWith(expect.objectContaining({ accent_color: '#123456' }));
  fireEvent.click(screen.getByLabelText(/Добавить блок/));
  expect(changed).toHaveBeenLastCalledWith(expect.objectContaining({ show_company_section: false }));
});

it('starts new proposals with v2 and explicitly upgrades a legacy template without changing its color or density', () => {
  expect(defaultDocumentAppearance.layout_version).toBe(2);
  expect(defaultDocumentAppearance.default_logo).toBe('kpdesk_v1');
  const changed = vi.fn();
  const old = { template: 'compact' as const, accent_color: '#123456', show_company_section: false };
  render(<DocumentAppearanceSettings value={old} disabled={false} hasCompanyFacts onChange={changed}/>);
  fireEvent.click(screen.getByRole('button', { name: 'Применить новое оформление КП' }));
  expect(changed).toHaveBeenCalledWith({ ...old, layout_version: 2, default_logo: 'kpdesk_v1' });
});
