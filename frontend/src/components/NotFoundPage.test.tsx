import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { NotFoundPage } from './NotFoundPage';

describe('public 404 screen #19', () => {
  it('shows real return links and never indexes an unknown route', () => {
    document.head.innerHTML = '<meta name="description"><meta name="robots">';
    render(<NotFoundPage />);
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('404');
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Страница не найдена');
    expect(document.querySelector('.not-found-hero a[href="/"]')).toBeInTheDocument();
    for (const link of screen.getAllByRole('link', { name: 'Вопросы и ответы' })) {
      expect(link).toHaveAttribute('href', '/faq');
    }
    expect(screen.getByRole('img', { name: /Иллюстрация страницы 404/ }))
      .toHaveAttribute('src', '/assets/hero/13_hero_404.avif');
    expect(document.querySelector('meta[name="robots"]')).toHaveAttribute('content', 'noindex,nofollow');
  });
});
