import { afterEach, describe, expect, it } from 'vitest';
import { fireEvent, screen } from '@testing-library/react';
import { enhancePublicHome } from './public-lite';

afterEach(() => {
  document.body.replaceChildren();
  document.body.style.overflow = '';
});

function fixture() {
  document.body.innerHTML = `<div id="root" data-ssr-route="/">
    <header class="public-header">
      <a class="public-logo" href="/"><img src="/assets/logo.avif" alt="KPDesk"/></a>
      <nav class="public-nav">
        <a href="/features">Возможности</a>
        <a href="/pricing">Тарифы</a>
      </nav>
      <button type="button" class="mobile-menu-toggle" aria-label="Открыть меню"
        aria-controls="_R_2_" aria-expanded="false">Меню</button>
    </header>
    <div class="public-faq-list">
      <details><summary>Первый вопрос</summary><p>Первый ответ</p></details>
      <details><summary>Второй вопрос</summary><p>Второй ответ</p></details>
    </div></div>`;
  enhancePublicHome();
}

describe('prerendered homepage progressive enhancement', () => {
  it('makes FAQ a native exclusive group without React or forced scrolling', () => {
    fixture();
    const details = document.querySelectorAll<HTMLDetailsElement>('.public-faq-list details');
    expect(details).toHaveLength(2);
    expect(details[0]).toHaveAttribute('name', 'kpdesk-home-faq');
    expect(details[1]).toHaveAttribute('name', 'kpdesk-home-faq');
    const summary = details[0]!.querySelector('summary')!;
    fireEvent.click(summary);
    expect(details[0]).toHaveAttribute('open');
    fireEvent.click(summary);
    expect(details[0]).not.toHaveAttribute('open');
  });

  it('preserves the menu semantics, locks scrolling and closes on Escape', () => {
    fixture();
    const trigger = screen.getByRole('button', { name: 'Открыть меню' });
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
    fireEvent.click(trigger);
    const dialog = screen.getByRole('dialog', { name: 'Меню навигации' });
    expect(dialog).toBeInTheDocument();
    expect(trigger).toHaveAttribute('aria-expanded', 'true');
    expect(document.body.style.overflow).toBe('hidden');
    expect(dialog).toHaveAttribute('id', '_R_2_');
    expect(screen.getByRole('navigation', { name: 'Мобильное меню' }))
      .toHaveTextContent('Возможности');
    expect(screen.getByRole('link', { name: 'Создать КП' })).toHaveAttribute('href', '/app');
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(trigger).toHaveFocus();
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
    expect(document.body.style.overflow).toBe('');
    expect(document.querySelector('.mobile-drawer-layer')).toHaveAttribute('aria-hidden', 'true');
  });

  it('traps keyboard focus and closes using its visible controls', () => {
    fixture();
    fireEvent.click(screen.getByRole('button', { name: 'Открыть меню' }));
    const close = screen.getByRole('button', { name: 'Закрыть меню' });
    const last = screen.getByRole('link', { name: 'support@kpdesk.ru' });
    last.focus();
    fireEvent.keyDown(document, { key: 'Tab' });
    expect(close).toHaveFocus();
    fireEvent.keyDown(document, { key: 'Tab', shiftKey: true });
    expect(last).toHaveFocus();
    fireEvent.click(screen.getByRole('button', { name: 'Закрыть меню вне панели' }));
    expect(screen.getByRole('button', { name: 'Открыть меню' }))
      .toHaveAttribute('aria-expanded', 'false');
  });
});
