import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, afterEach } from 'vitest';
import { MobileNavigation } from './MobileNavigation';

const links = [
  { href: '/features', label: 'Возможности' },
  { href: '/pricing', label: 'Тарифы' },
] as const;
afterEach(() => { document.body.style.overflow = ''; });

describe('right-hand mobile navigation', () => {
  it('opens an accessible modal drawer, prevents page scrolling and closes on overlay', () => {
    render(<MobileNavigation links={links} label="Открыть меню" />);
    const trigger = screen.getByRole('button', { name: 'Открыть меню' });
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
    fireEvent.click(trigger);
    expect(trigger).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByRole('dialog', { name: 'Меню навигации' })).toBeInTheDocument();
    expect(document.body.style.overflow).toBe('hidden');
    fireEvent.click(screen.getByRole('button', { name: 'Закрыть меню вне панели' }));
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
    expect(document.body.style.overflow).toBe('');
  });
  it('contains real links, restores focus on Escape, and closes on navigation', () => {
    render(<MobileNavigation links={links} onNavigate={() => {}} />);
    const trigger = screen.getByRole('button', { name: 'Открыть меню' });
    fireEvent.click(trigger);
    expect(screen.getByRole('navigation', { name: 'Мобильное меню' })).toBeInTheDocument();
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(trigger).toHaveFocus();
    fireEvent.click(trigger);
    fireEvent.click(screen.getByRole('link', { name: 'Возможности' }));
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
  });
  it('traps tab navigation within the open drawer', () => {
    render(<MobileNavigation links={links} />);
    fireEvent.click(screen.getByRole('button', { name: 'Открыть меню' }));
    const close = screen.getByRole('button', { name: 'Закрыть меню' });
    const end = screen.getByRole('link', { name: 'support@kpdesk.ru' });
    end.focus();
    fireEvent.keyDown(document, { key: 'Tab' });
    expect(close).toHaveFocus();
    close.focus();
    fireEvent.keyDown(document, { key: 'Tab', shiftKey: true });
    expect(end).toHaveFocus();
  });
});
