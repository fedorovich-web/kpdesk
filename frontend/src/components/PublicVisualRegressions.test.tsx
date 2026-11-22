import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { pricingFaqs } from '../content/pricing';
import { IntegrationsPage } from './IntegrationsPage';
import { PricingPage } from './PricingPage';

describe('public marketing regressions', () => {
  it('reuses the homepage FAQ cards on pricing without forcing scroll', () => {
    render(<PricingPage />);
    const heading = screen.getByRole('heading', { name: 'Оплата, лимиты и продление тарифа' });
    const section = heading.closest('section');
    expect(section).toHaveClass('marketing-price-faq');
    const list = section?.querySelector('.public-faq-list');
    expect(list).not.toBeNull();
    const questions = Array.from(list!.querySelectorAll('details'));
    expect(questions).toHaveLength(pricingFaqs.length);
    expect(questions.every(item => item.classList.contains('public-faq-item'))).toBe(true);
    expect(new Set(questions.map(item => item.getAttribute('name')))).toEqual(new Set(['kpdesk-pricing-faq']));
    const summaries = questions.map(item => item.querySelector('summary')!);
    const scrollBy = vi.spyOn(window, 'scrollBy').mockImplementation(() => {});
    try {
      fireEvent.click(summaries[0]!);
      expect(questions[0]).toHaveAttribute('open');
      fireEvent.click(summaries[1]!);
      expect(questions[1]).toHaveAttribute('open');
      fireEvent.click(summaries[1]!);
      expect(questions[1]).not.toHaveAttribute('open');
      expect(scrollBy).not.toHaveBeenCalled();
    } finally {
      scrollBy.mockRestore();
    }
  });

  it('requests integrated artwork early and uses smaller responsive variants', () => {
    render(<IntegrationsPage />);
    const heading = screen.getByRole('heading', { name: 'Четыре обязательных столбца прайса' });
    const section = heading.closest('section');
    expect(section).toHaveClass('marketing-api-section');
    const grid = section?.querySelector('.marketing-api-grid');
    expect(grid).not.toBeNull();
    const diagram = grid?.querySelector(':scope > img');
    expect(diagram).toHaveAttribute('loading', 'eager');
    expect(diagram).toHaveAttribute('decoding', 'async');
    expect(diagram).toHaveAttribute('srcset', expect.stringContaining('07_hero_integrations_scheme-640w.avif'));
    expect(diagram).toHaveAttribute('srcset', expect.stringContaining('07_hero_integrations_scheme-960w.avif'));
    expect(diagram).toHaveAttribute('sizes', expect.stringContaining('50vw'));
    for (const icon of document.querySelectorAll('.marketing-integration-icon, .marketing-task-grid img')) {
      expect(icon).toHaveAttribute('loading', 'eager');
      expect(icon).toHaveAttribute('srcset', expect.stringContaining('-96w.avif'));
    }
  });
});
