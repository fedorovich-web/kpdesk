import { fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { WorkspaceNavigation } from './WorkspaceNavigation';

afterEach(() => { document.querySelector('#workspace-sources')?.remove(); });
describe('workspace task navigation', () => {
  it('orders the primary workflow and exposes real destinations', () => {
    render(<WorkspaceNavigation canWrite/>);
    const links = screen.getAllByRole('link');
    expect(links.slice(0, 4).map(link => link.getAttribute('href'))).toEqual([
      '#workspace-catalog', '#workspace-request', '#workspace-quote-editor', '#workspace-quotes',
    ]);
    expect(screen.getByRole('link', { name: 'Прайс' })).toHaveAttribute('aria-current', 'location');
  });
  it('opens a collapsed destination when navigating to additional tools', () => {
    const target = document.createElement('details');
    target.id = 'workspace-sources'; document.body.append(target);
    render(<WorkspaceNavigation canWrite/>);
    fireEvent.click(screen.getByRole('link', { name: 'Прайсы и проверка данных' }));
    expect(target.open).toBe(true);
  });
  it('keeps write-only destinations out of viewer navigation', () => {
    render(<WorkspaceNavigation canWrite={false}/>);
    expect(screen.queryByRole('link', { name: 'Товары для КП' })).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Прайсы и проверка данных' })).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Коммерческие предложения' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Тариф и оплата' })).toHaveAttribute('href', '#workspace-billing');
  });
});
