import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { pricingFaqs } from '../content/pricing';
import { IntegrationsPage } from './IntegrationsPage';
import { PricingPage } from './PricingPage';
import { HowItWorksPage } from './HowItWorksPage';

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
    const diagram = grid?.querySelector(':scope > .marketing-api-diagram > img');
    expect(diagram).toHaveAttribute('loading', 'eager');
    expect(diagram).toHaveAttribute('decoding', 'async');
    expect(diagram).toHaveAttribute('srcset', expect.stringContaining('07_hero_integrations_scheme-640w.avif'));
    expect(diagram).toHaveAttribute('srcset', expect.stringContaining('07_hero_integrations_scheme-960w.avif'));
    expect(diagram).toHaveAttribute('sizes', expect.stringContaining('410px'));
    for (const icon of document.querySelectorAll('.marketing-integration-icon, .marketing-task-grid img')) {
      expect(icon).toHaveAttribute('loading', 'eager');
      if (icon.getAttribute('src')?.endsWith('.avif')) {
        expect(icon).toHaveAttribute('srcset', expect.stringContaining('-96w.avif'));
      }
    }
    const brands = Array.from(document.querySelectorAll<HTMLImageElement>('.marketing-integration-icon[src^="/assets/brands/"]'));
    expect(brands.map(icon => icon.getAttribute('src'))).toEqual([
      '/assets/brands/1c.svg', '/assets/brands/bitrix24.svg', '/assets/brands/amocrm-display.svg',
      '/assets/brands/microsoft-excel-display.svg', '/assets/brands/google-drive.svg',
    ]);
  });

  it('renders process samples as readable text with only small illustration icons', () => {
    const { container } = render(<HowItWorksPage />);
    const previews = [...container.querySelectorAll('.marketing-step-art')];
    expect(previews).toHaveLength(5);
    expect(previews[0]).toHaveTextContent('Прайс.xlsx');
    expect(previews[4]).toHaveTextContent('Готовое предложение');
    for (const preview of previews) {
      expect(preview.querySelector('dl')).not.toBeNull();
      expect(preview.querySelector('img')).toHaveAttribute('width', '24');
    }
  });
});
